"""Progress reporting that does not depend on the installer's own window.

Installers like the ones gitfurl targets read their payload from big files next to the
setup program. While the installer runs, gitfurl looks (read-only, through /proc) at how far
into those files the running processes have read, and how much has been written into the
prefix. That gives a percentage, a rate and a rough ETA without scraping the installer.

This is a heuristic: it assumes the large files beside the installer are its payload. An
installer that copies or reads them differently can make the numbers wrong, and the ETA is
only an estimate. Nothing here reads file contents.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from gitfurl.detect import clean

MIN_PAYLOAD_BYTES = 1 << 20
# Wine's own background processes: not interesting to show as "what the installer is doing".
WINE_INFRA = {"wineserver", "services.exe", "winedevice.exe", "explorer.exe", "plugplay.exe",
              "rpcss.exe", "svchost.exe", "conhost.exe", "wineboot.exe", "winedbg.exe", "winedbg",
              "start.exe"}


# Wine starts its debugger when a Windows program crashes (the "Program Error" dialog). Its
# /proc name is "winedbg" with Wine 11.0 (seen 2026-11-24) and "winedbg.exe" elsewhere.
CRASH_HANDLER = "winedbg.exe"
CRASH_HANDLER_NAMES = frozenset([CRASH_HANDLER, "winedbg"])
# CPU use below this (percent of one core) counts as quiet: Wine's own background processes
# use about 1-2% while nothing else happens (seen with Wine 11.0 on 2026-11-24).
QUIET_CPU_PCT = 3.0


@dataclass(frozen=True)
class Activity:
    count: int = 0  # processes running in the prefix
    names: tuple = ()  # program names other than Wine's own background processes
    ticks: int = 0  # CPU ticks (user plus system), summed
    pids: tuple = ()  # every process counted
    crash_pids: tuple = ()  # processes named winedbg.exe (Wine's crash handler)

    @property
    def crash_handler(self) -> bool:
        return bool(self.crash_pids)


def prefix_activity(prefix: Path, proc_root: str = "/proc") -> tuple[int, list[str], int]:
    """Processes of this user that run in the prefix: (count, program names, CPU ticks).
    See prefix_processes()."""
    a = prefix_processes(prefix, proc_root)
    return a.count, list(a.names), a.ticks


def prefix_processes(prefix: Path, proc_root: str = "/proc") -> Activity:
    """Processes of this user that run in the prefix.

    A process counts when its environment says WINEPREFIX=<prefix>. Only that one variable
    is looked at; nothing else from the environment is kept. CPU ticks are user plus system
    time since the process started, summed.
    """
    want = f"WINEPREFIX={prefix}".encode()
    count, ticks, names, found, crash = 0, 0, [], [], []
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return Activity()
    for pid in pids:
        try:
            with open(os.path.join(proc_root, pid, "environ"), "rb") as fh:
                if want not in fh.read().split(b"\0"):
                    continue
            with open(os.path.join(proc_root, pid, "stat"), encoding="ascii", errors="replace") as fh:
                stat = fh.read()
        except OSError:
            continue
        # "pid (comm) state ...": comm may contain spaces and parentheses, so split at the last ")".
        head, _, rest = stat.rpartition(")")
        fields = rest.split()
        try:
            ticks += int(fields[11]) + int(fields[12])  # utime, stime
        except (IndexError, ValueError):
            continue
        name = head.partition("(")[2]
        count += 1
        found.append(int(pid))
        if name in CRASH_HANDLER_NAMES:
            crash.append(int(pid))
        if name not in WINE_INFRA and name not in names:
            names.append(name)
    return Activity(count, tuple(sorted(names)), ticks, tuple(sorted(found)),
                    tuple(sorted(crash)))


def payload_files(installer: Path) -> list[Path]:
    """Large regular files in the installer's folder, other than the installer itself."""
    out = []
    try:
        for e in os.scandir(installer.parent):
            p = Path(e.path)
            if p != installer and e.is_file(follow_symlinks=False) \
                    and e.stat(follow_symlinks=False).st_size >= MIN_PAYLOAD_BYTES:
                out.append(p)
    except OSError:
        pass
    return sorted(out)


def read_positions(files: list[Path], proc_root: str = "/proc") -> dict[str, int]:
    """Highest read offset any process of this user holds on each file (path -> bytes)."""
    wanted = {str(f) for f in files}
    best: dict[str, int] = {}
    try:
        pids = [p for p in os.listdir(proc_root) if p.isdigit()]
    except OSError:
        return best
    for pid in pids:
        fd_dir = os.path.join(proc_root, pid, "fd")
        try:
            fds = os.listdir(fd_dir)
        except OSError:
            continue
        for fd in fds:
            try:
                target = os.readlink(os.path.join(fd_dir, fd))
            except OSError:
                continue
            if target not in wanted:
                continue
            try:
                with open(os.path.join(proc_root, pid, "fdinfo", fd), encoding="ascii") as fh:
                    for line in fh:
                        if line.startswith("pos:"):
                            pos = int(line.split()[1])
                            best[target] = max(best.get(target, 0), pos)
                            break
            except (OSError, ValueError):
                continue
    return best


def tree_size(root: Path) -> int:
    total = 0
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total


@dataclass
class Sample:
    elapsed_s: float
    read_bytes: int
    total_bytes: int
    written_bytes: int  # growth of the prefix since the run started
    rate_bps: float  # smoothed payload read rate
    procs: int = 0  # processes running in the prefix
    cpu_pct: float | None = None  # their CPU use since the last sample (100 = one core)
    helpers: tuple = ()  # program names other than Wine's own background processes
    pids: tuple = ()  # the processes running in the prefix
    crash_pids: tuple = ()  # Wine's crash handler (winedbg.exe) processes in the prefix

    @property
    def percent(self) -> float | None:
        return None if not self.total_bytes else min(100.0, 100.0 * self.read_bytes / self.total_bytes)

    @property
    def eta_s(self) -> float | None:
        remaining = self.total_bytes - self.read_bytes
        if self.total_bytes <= 0 or self.rate_bps <= 0 or self.read_bytes < 0.01 * self.total_bytes:
            return None  # too early for a useful estimate
        return remaining / self.rate_bps

    def line(self) -> str:
        def gb(n):
            return f"{n / 1e9:.2f} GB"

        parts = [f"elapsed {_dur(self.elapsed_s)}"]
        if self.total_bytes:
            parts.append(f"payload read {gb(self.read_bytes)} of {gb(self.total_bytes)} "
                         f"({self.percent:.1f}%)")
            if self.rate_bps > 0:
                parts.append(f"{self.rate_bps / 1e6:.0f} MB/s")
            eta = self.eta_s
            parts.append(f"about {_dur(eta)} left (estimate)" if eta is not None else "no ETA yet")
        else:
            parts.append("no payload files found beside the installer")
        parts.append(f"written to prefix {gb(self.written_bytes)}")
        if self.procs:
            busy = f"{self.procs} process(es) running"
            if self.cpu_pct is not None:
                busy += f", CPU {self.cpu_pct:.0f}%"
            if self.helpers:
                # names come from /proc and are chosen by the installer: printable only
                busy += " (" + ", ".join(clean(str(h), 40) for h in self.helpers[:4]) + ")"
            parts.append(busy)
            if self.cpu_pct is not None and self.cpu_pct < QUIET_CPU_PCT and self.rate_bps <= 0:
                parts.append("quiet: it may be waiting, or finishing; slow phases are normal")
        return ", ".join(parts)


def _dur(s: float) -> str:
    s = int(s)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}h{m:02d}m" if h else f"{m}m{sec:02d}s"


STOP_WAIT_S = 5.0  # longest ProgressMonitor.stop() waits for a running on_sample


class ProgressMonitor:
    """Samples progress on a background thread and hands each Sample to on_sample."""

    def __init__(self, installer: Path, prefix: Path, on_sample, interval: float = 10.0,
                 proc_root: str = "/proc"):
        self.files = payload_files(installer)
        self.total = sum(f.stat().st_size for f in self.files)
        self.prefix, self.on_sample, self.interval, self.proc_root = prefix, on_sample, interval, proc_root
        self._stop = threading.Event()
        # Held while sampling and while on_sample runs, so stop() never samples at the same
        # time as the thread, and no on_sample call starts after stop() has returned.
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._t0 = 0.0
        self._base = 0
        self._last: tuple[float, int] | None = None
        self._rate = 0.0
        self._cpu_last: tuple[float, int] | None = None
        self.last: Sample | None = None

    def sample(self) -> Sample:
        now = time.monotonic()
        read = sum(read_positions(self.files, self.proc_root).values())
        if self._last is not None and now > self._last[0]:
            inst = max(0.0, (read - self._last[1]) / (now - self._last[0]))
            self._rate = inst if self._rate == 0 else 0.7 * self._rate + 0.3 * inst
        self._last = (now, read)
        act = prefix_processes(self.prefix, self.proc_root)
        cpu = None
        if self._cpu_last is not None and now > self._cpu_last[0]:
            hz = os.sysconf("SC_CLK_TCK") if hasattr(os, "sysconf") else 100
            cpu = max(0.0, 100.0 * (act.ticks - self._cpu_last[1]) / hz
                      / (now - self._cpu_last[0]))
        self._cpu_last = (now, act.ticks)
        s = Sample(now - self._t0, read, self.total, max(0, tree_size(self.prefix) - self._base),
                   self._rate, act.count, cpu, act.names, act.pids, act.crash_pids)
        self.last = s
        return s

    def start(self) -> None:
        self._t0 = time.monotonic()
        self._base = tree_size(self.prefix)
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            with self._lock:
                if self._stop.is_set():
                    return
                try:
                    self.on_sample(self.sample())
                except Exception:  # noqa: BLE001, S110 - progress must never break the install
                    pass

    def stop(self, wait_s: float = STOP_WAIT_S) -> Sample:
        """Stop sampling and return a final sample.

        Once the lock is taken the thread is either waiting or about to see the stop flag, so
        no on_sample call starts after this returns and sample() never runs twice at once.
        A Ctrl-C while this waits for the lock, the final sample or the thread is held back
        and raised only after the thread has ended (or `wait_s` seconds have passed), so the
        thread is never left inside on_sample because of it. If on_sample does not return
        within `wait_s` seconds (for example a write to a stalled terminal), this stops
        waiting and the thread may still be inside it.
        """
        self._stop.set()
        held: list[KeyboardInterrupt] = []
        deadline = time.monotonic() + wait_s

        def left() -> float:
            return max(0.0, deadline - time.monotonic())

        got = _uninterrupted(lambda: self._lock.acquire(timeout=left()), held)
        try:
            final = _uninterrupted(self.sample, held)
        finally:
            if got:
                self._lock.release()
        if self._thread:
            _uninterrupted(lambda: self._thread.join(timeout=left()), held)
        if held:
            raise held[0]
        return final



def _uninterrupted(fn, held: list):
    """Call fn until it returns without a KeyboardInterrupt; keep each one in `held`."""
    while True:
        try:
            return fn()
        except KeyboardInterrupt as exc:
            held.append(exc)


# Heuristic thresholds for "observed activity" (see ActivityObserver). Not proof of anything.
OBSERVE_MIN_WRITTEN = 1 << 20  # bytes the prefix must have grown by
OBSERVE_MIN_READ = 1 << 20  # bytes into the payload files a process must have read


class ActivityObserver:
    """Remembers the largest bytes-written and bytes-read seen across a run's samples.

    `observed` is a heuristic: the prefix grew by at least `min_written` AND a process read at
    least `min_read` bytes into the payload files. It sees only what the samples caught, so a
    short burst between two samples is missed. It is evidence of activity, not proof that
    anything was extracted or installed.
    """

    def __init__(self, min_written: int = OBSERVE_MIN_WRITTEN, min_read: int = OBSERVE_MIN_READ):
        self.min_written, self.min_read = min_written, min_read
        self.max_written = 0
        self.max_read = 0
        self.samples = 0

    def update(self, sample: Sample) -> None:
        self.samples += 1
        self.max_written = max(self.max_written, sample.written_bytes)
        self.max_read = max(self.max_read, sample.read_bytes)

    @property
    def measured(self) -> bool:
        return self.samples > 0

    @property
    def observed(self) -> bool:
        return self.max_written >= self.min_written and self.max_read >= self.min_read


class StallDetector:
    """Advisory only: says that nothing moved for `after_s` seconds, once per quiet period
    (after the run moves again, a new quiet period is reported again). It never acts.

    Activity is any of: the payload read position grew, the prefix's processes used at least
    QUIET_CPU_PCT of one core since the last sample, or the run's log file grew. The clock is injected for tests.
    """

    def __init__(self, after_s: float, log_path: Path | None = None, clock=time.monotonic):
        if after_s <= 0:
            raise ValueError("the stall time must be above 0 seconds")
        self.after_s, self.log_path, self.clock = after_s, log_path, clock
        self.fired = False
        self._log_size = 0
        self._log_size = self._size()
        self._last_move = clock()
        self._read = 0

    def _size(self) -> int:
        try:
            return os.path.getsize(self.log_path) if self.log_path else 0
        except OSError:
            return self._log_size

    def check(self, sample: Sample) -> float | None:
        """Feed one sample. Returns the idle seconds when a quiet period first passes the
        limit, else None."""
        now = self.clock()
        size = self._size()
        if sample.read_bytes > self._read or size > self._log_size \
                or (sample.cpu_pct is not None and sample.cpu_pct >= QUIET_CPU_PCT):
            self._last_move = now
            self.fired = False  # moving again: the next quiet period is reported too
        self._read = max(self._read, sample.read_bytes)
        self._log_size = max(self._log_size, size)
        idle = now - self._last_move
        if not self.fired and idle >= self.after_s:
            self.fired = True
            return idle
        return None
