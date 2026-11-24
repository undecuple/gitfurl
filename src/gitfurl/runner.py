"""Execution planning. M1 only builds and previews a plan; it never runs anything."""

from __future__ import annotations

import os
import shlex
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from gitfurl.prefixes import child_env as build_child_env
from gitfurl.prefixes import prefix_path
from gitfurl.preflight import memory_available
from gitfurl.procgroup import SigintGuard as _SigintGuard
from gitfurl.procgroup import wineserver_for
from gitfurl.progress import (
    CRASH_HANDLER_NAMES,
    QUIET_CPU_PCT,
    ActivityObserver,
    ProgressMonitor,
    StallDetector,
    _dur,
    prefix_processes,
)


@dataclass(frozen=True)
class InstallPlan:
    argv: list[str]
    prefix: Path
    cwd: Path
    env_overrides: dict[str, str] = field(default_factory=dict)
    prerequisites: list[str] = field(default_factory=list)
    runtime: str = "wine"
    portable: bool = False  # run inside bwrap (see gitfurl.portable); off by default
    scan: object = None  # a gitfurl.scan.ScanPlan: ClamAV scan before the run; off by default
    # Run inside a memory-limited systemd user scope (see gitfurl.memlimit). Off in this
    # library default; the CLI and the window turn it on unless the user turns it off.
    memory_limit: bool = False
    # A gitfurl.unattended.UnattendedPlan: run an Inno Setup installer without its wizard
    # (its switches are already in argv). Off by default.
    unattended: object = None
    # Keep Wine's Mono and Gecko install dialogs when the prefix is created (wine runtime).
    # Off by default: wineboot gets prefixes.QUIET_WINEBOOT_ENV.
    wine_addons: bool = False

    @property
    def installer(self) -> Path:
        """The installer file: argv[1] for both runtimes (switches, if any, follow it)."""
        return Path(self.argv[1])

    def preview(self) -> str:
        """Readable preview only. The real call would use argv, never a shell string."""
        return shlex.join(self.argv)


def validate_installer(path_str: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise ValueError(f"installer not found: {path}")
    if not path.is_file():
        raise ValueError(f"installer is not a regular file: {path}")
    return path.resolve()


RUNTIMES = ("wine", "proton")


def validate_proton_path(value: str | None) -> Path:
    """A Proton folder you chose (it must contain the 'proton' script). Never downloaded."""
    if not value:
        raise ValueError("the proton runtime needs a Proton folder: pass --proton-path or set "
                         "PROTONPATH (gitfurl does not download one)")
    path = Path(value).expanduser()
    if not (path / "proton").is_file():
        raise ValueError(f"not a Proton folder (no 'proton' file inside): {path}")
    return path.resolve()


def runtime_env(runtime: str, proton_path: str | None) -> dict[str, str]:
    """Environment overrides a runtime needs besides WINEPREFIX."""
    if runtime == "wine":
        return {}
    if runtime != "proton":
        raise ValueError(f"unknown runtime: {runtime}")
    return {"PROTONPATH": str(validate_proton_path(proton_path)), "GAMEID": "umu-0"}


def build_plan(installer: Path, prefix_name: str, env: dict[str, str] | None = None,
               runtime: str = "wine", proton_path: str | None = None,
               portable: bool = False, scan=None,
               memory_limit: bool = False, unattended=None,
               wine_addons: bool = False) -> InstallPlan:
    prefix = prefix_path(prefix_name, env)
    extra = runtime_env(runtime, proton_path)
    if portable:
        from gitfurl.portable import MENU_OVERRIDE

        extra = {**extra, **MENU_OVERRIDE}
    if runtime == "proton":
        needs = [
            "umu-run available on PATH",
            ("the Steam Linux Runtime, which umu-run downloads on its first run unless it is "
             "already present (a network download; gitfurl does not start it for you)"),
        ]
    else:
        needs = ["wine available on PATH (run 'gitfurl doctor')"]
    if portable:
        needs.append("bwrap (bubblewrap) available on PATH (portable install)")
    if scan is not None:
        needs.append("clamscan on PATH and its signature database (ClamAV scan first; gitfurl "
                     "never updates the database)")
    if memory_limit:
        needs.append("systemd-run and a user systemd manager with the memory controller "
                     "(memory limit; turn it off with --no-memory-limit)")
    return InstallPlan(
        argv=["umu-run" if runtime == "proton" else "wine", str(installer),
              *(unattended.args() if unattended is not None else [])],
        prefix=prefix,
        cwd=installer.parent,
        env_overrides={"WINEPREFIX": str(prefix), **extra},
        prerequisites=[*needs, f"dedicated prefix at {prefix}, created with approval"],
        runtime=runtime,
        portable=portable,
        scan=scan,
        memory_limit=memory_limit,
        unattended=unattended,
        wine_addons=wine_addons,
    )


CANCELLED_STATUS = 130  # the shell convention for a run ended by an interrupt
STOP_GRACE_S = 10.0

CANCEL_REMAINS = (
    "Stopped by you. Nothing was deleted. The prefix may hold partly installed files and "
    "registry changes, and files may have been written elsewhere on this machine. Any process "
    "that left the run's own process group was not touched. Read the log, and the changes "
    "report if you turned it on."
)


class RunControl:
    """Stops the run gitfurl started, and only that run.

    The installer is started in its own process group, so a stop signals that group and
    nothing else: never a process found by name, never another Wine session. For the wine
    runtime, after the group has ended, the Wine server of this run's own prefix is asked to
    exit (`wineserver -k` with that WINEPREFIX), which also ends anything else running in
    that dedicated prefix. Complete process-tree termination is not guaranteed: a program
    that moved itself into another session is out of reach of a group signal.

    A process-group id can be reused once the group is empty, so the group leader is kept
    unreaped until any stop has finished, every signal is sent under the lock, and nothing
    is signalled after the run has ended (a stop then does nothing).

    Starting the leader and recording its group happen under the same lock as a stop
    request, so a stop either sees the group or prevents the start; it cannot fall between.
    A run counts as cancelled only if the stop took effect: before the start, or by
    signalling a group that still had a live member.
    """

    def __init__(self, grace_s: float = STOP_GRACE_S):
        self.grace_s = grace_s
        self.cancelled = False
        self.notes: list[str] = []
        self._pgid: int | None = None
        self._ended = False  # the leader has exited: no new stop may signal anything
        self._finished = False
        self._stopping = False
        self._stop_done = threading.Event()
        self._lock = threading.Lock()
        self._server_argv: list[str] | None = None
        self._server_env: dict[str, str] | None = None
        self._server_configured = False

    @property
    def started(self) -> bool:
        """The run's process was created."""
        return self._pgid is not None

    def configure_server(self, argv: list[str] | None, env: dict[str, str] | None) -> None:
        """Wine runtime only. argv None means no wineserver was found."""
        self._server_argv, self._server_env = argv, env
        self._server_configured = True

    def start(self, popen_fn):
        """Start the leader unless a stop was already requested. Returns it, or None.

        The check, the start and the recording of the group are one step under the lock.
        """
        with self._lock:
            if self.cancelled:
                return None
            proc = popen_fn()
            self._pgid = proc.pid  # the leader is its own group leader (start_new_session)
            return proc

    def mark_exited(self) -> None:
        """The leader has exited (it is not yet reaped). A stop requested from now on does nothing."""
        with self._lock:
            self._ended = True

    def _signal(self, sig) -> bool:
        """Signal the run's group, only while the run is active. False if it was refused."""
        with self._lock:
            if self._finished or self._pgid is None:
                return False
            try:
                os.killpg(self._pgid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            return True

    def _group_alive(self) -> bool:
        """True while a live (not zombie) process is in the run's group. Read-only.

        The leader stays unreaped until finish(), so the group id cannot be reused while this
        is called; a zombie leader alone does not count as alive.
        """
        with self._lock:
            if self._finished or self._pgid is None:
                return False
            pgid = self._pgid
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                    fields = fh.read().rpartition(")")[2].split()
                if int(fields[2]) == pgid and fields[0] != "Z":
                    return True
            except (OSError, ValueError, IndexError):
                continue
        return False

    def _wait_group(self, seconds: float, guard=None) -> None:
        """Wait for the group to end. A Ctrl-C held back by the guard ends the wait early."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline and not (guard and guard.hits) and self._group_alive():
            time.sleep(0.1)

    def request_stop(self) -> list[str]:
        """Stop the run: TERM to its group, KILL after the grace period. Safe to call twice.

        It always runs to completion: Ctrl-C is held back while it works (a second one skips
        the grace period and forces the stop), KILL is sent even if something goes wrong in
        between, and a waiting finish() is always released.
        """
        mine = False
        guard = _SigintGuard()
        try:
            guard.__enter__()
            with self._lock:
                if self._stopping or self._ended or self._finished:
                    return list(self.notes)
                self._stopping = mine = True
                started = self._pgid is not None
                if not started:
                    self.cancelled = True  # start() will refuse
            terminated = False
            try:
                if started and self._group_alive() and self._signal(signal.SIGTERM):
                    terminated = True
                    self.cancelled = True
                    self._wait_group(self.grace_s, guard)
            finally:
                if terminated and self._group_alive() and self._signal(signal.SIGKILL):
                    self.notes.append(
                        "stop was forced by a second Ctrl-C: remaining processes were "
                        "force-killed" if guard.hits else
                        f"some processes did not stop within {self.grace_s:g} s and "
                        "were force-killed")
                    self._wait_group(3)
                    if self._group_alive():
                        self.notes.append(
                            "could not confirm that every process of the run ended")
            if self.cancelled and started:
                self._ask_server_to_exit()
        finally:
            if mine:
                self._stop_done.set()
            guard.release()
        return list(self.notes)

    def _ask_server_to_exit(self) -> None:
        if not self._server_configured:
            return  # not the wine runtime
        if not self._server_argv:
            self.notes.append("wineserver was not found, so this prefix's Wine server was not "
                              "asked to exit; anything it still runs may be left over")
            return
        try:
            done = subprocess.run(self._server_argv, env=self._server_env, timeout=30,
                                  check=False, stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL)
            if done.returncode == 0:
                self.notes.append("asked this prefix's Wine server to exit (wineserver -k)")
            else:
                self.notes.append(f"wineserver -k for this prefix exited with status "
                                  f"{done.returncode}; its server may still be running")
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.notes.append(f"could not stop this prefix's Wine server: {exc}")

    def finish(self) -> None:
        """Called by the run once the leader has exited (not yet reaped): waits for a stop in
        progress, then closes the run so nothing can be signalled any more."""
        self.mark_exited()
        with self._lock:
            stopping = self._stopping
        if stopping:
            self._stop_done.wait()
        with self._lock:
            self._finished = True


def _run_in_group(argv, cwd, env, stdout, control: RunControl):
    """Start argv in its own process group and wait. Ctrl-C stops it gracefully."""
    def popen():
        return subprocess.Popen(argv, cwd=cwd, env=env, stdout=stdout, stderr=subprocess.STDOUT,
                                start_new_session=True)

    guard = _SigintGuard()
    guard.__enter__()  # a Ctrl-C from here until the wait loop is held, then replayed
    try:
        proc = control.start(popen)
        if proc is None:  # stopped before it started
            return subprocess.CompletedProcess(argv, CANCELLED_STATUS)
        try:
            guard.release()  # from here a Ctrl-C raises, and the handler below covers it
            if guard.hits:
                control.request_stop()
            while True:
                try:  # wait without reaping, so the group id stays ours until finish()
                    os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOWAIT)
                    break
                except KeyboardInterrupt:
                    control.request_stop()
                except ChildProcessError:
                    break
            control.mark_exited()
        except BaseException:
            control.request_stop()  # never leave the leader running unmanaged
            raise
        finally:
            control.finish()
            proc.wait()  # reap
        return proc
    finally:
        guard.release()


@dataclass
class RunOptions:
    """Optional extras for a run. Everything here is off by default."""

    on_progress: object = None  # callable(Sample), enables the progress monitor
    progress_interval: float = 10.0
    detailed_log: bool = False  # also write a JSON-lines event log
    wine_debug: str | None = None  # WINEDEBUG channels, e.g. "err+all"
    control: RunControl | None = None  # lets the caller stop the run (see RunControl)
    stall_after_s: float | None = None  # advisory "possibly stalled" after this much idle time
    on_note: object = None  # callable(str): one-line advisory notes (stall, crash handler)
    window_titles: object = None  # callable(pids, programs, env) -> list|None; tests inject
    window_list: object = None  # callable(pids, programs, env, pid_of=) -> list|None; tests
    clock: object = time.monotonic  # injected in tests
    proc_root: str = "/proc"  # injected in tests
    meminfo: str = "/proc/meminfo"  # injected in tests
    allow_scan_hit: bool = False  # run even if the ClamAV scan matched a signature
    scan_result: object = None  # a ScanResult already obtained (the app's second confirmation)
    scan_summary: str | None = None  # set by run_with_prefix; written to the log
    cgroup_root: str = "/sys/fs/cgroup"  # injected in tests (memory limit)
    memory_interval: float = 5.0  # seconds between memory limit adjustments


_WINEDEBUG_OK = None


def validate_wine_debug(value: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z0-9+,_\-]{1,200}", value):
        raise ValueError("invalid --wine-debug value: use letters, digits and + , - _ only")
    return value


def logs_root(env: dict[str, str] | None = None) -> Path:
    import os

    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/state")
    return Path(base) / "gitfurl" / "logs"


@dataclass
class ExecResult:
    """What a run reported, as separate facts. Unpacks as (status, log_path) for old callers.

    started: the installer process was created (False if a stop came before the start).
    exit_status: the process's own exit code (None if it never started). `status` is what
      gitfurl reports: 130 for a cancelled run, otherwise the same as exit_status.
    extraction_observed: a heuristic only. The prefix grew and payload files were read, as
      the progress monitor saw. It is observed activity, not proof of anything. It is False
      when nothing was seen, and also when it was not measured (see observation_measured).
    observation_measured: the progress monitor ran and took at least one sample.
    """

    status: int
    log_path: Path
    started: bool = True
    extraction_observed: bool = False
    observation_measured: bool = False
    exit_status: int | None = None
    memory: object = None  # gitfurl.memlimit.MemoryReport when the memory limit was on
    crash_seen_s: float | None = None  # seconds into the run when Wine's crash handler was seen
    unattended: object = None  # gitfurl.unattended.UnattendedReport for an unattended run

    def as_tuple(self):
        return (self.status, self.log_path)

    def __iter__(self):
        return iter(self.as_tuple())

    def __getitem__(self, i):
        return self.as_tuple()[i]

    def __len__(self):
        return len(self.as_tuple())


@dataclass
class TrackedResult(ExecResult):
    """ExecResult from run_tracked: unpacks as (status, log_path, ChangeReport or None)."""

    changes: object = None

    def as_tuple(self):
        return (self.status, self.log_path, self.changes)


def describe_result(result) -> str | None:
    """Three plain lines for the end text. None if `result` is not an ExecResult."""
    if not isinstance(result, ExecResult):
        return None
    if not result.started:
        return "Started: no (stopped before the installer was created).\nExit status: none."
    if not result.observation_measured:
        seen = "not measured (the progress monitor was off)"
    elif result.extraction_observed:
        seen = ("yes. The prefix grew and the installer's files were read while it ran. This is "
                "a heuristic and does not show that anything was extracted or installed")
    else:
        seen = "no (not seen; a short burst between samples would be missed)"
    text = (f"Started: yes.\nObserved activity (heuristic): {seen}.\n"
            f"Exit status: {result.exit_status}.")
    if result.crash_seen_s is not None:
        text += (f"\nCrash: Wine's crash handler (winedbg) was seen {_dur(result.crash_seen_s)} "
                 "into the run (checked at each progress sample), so a Windows program in the "
                 "prefix crashed. See the log.")
    if result.unattended is not None:
        text += "\n" + "\n".join(result.unattended.lines())
    if result.memory is not None:
        text += "\n" + "\n".join(result.memory.lines())
    return text


def execute(plan: InstallPlan, wine: str, env: dict[str, str] | None = None, run=None,
            options: RunOptions | None = None):
    """Run the installer from an approved plan. Returns an ExecResult (unpacks as status, log).

    Output goes to a local log file only. The environment values are never written to the log.
    With options, it can also report progress and write a JSON-lines event log next to it.
    """
    import json
    from datetime import datetime

    options = options or RunOptions()
    control = options.control or RunControl()
    if not plan.prefix.is_dir():
        raise FileNotFoundError(f"prefix does not exist: {plan.prefix} (run 'gitfurl prefix create')")
    log_dir = logs_root(env)
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{plan.prefix.name}-{stamp}.log"
    argv = [wine, *plan.argv[1:]]
    base_env = os.environ if env is None else env
    overrides = dict(plan.env_overrides)
    if options.wine_debug:
        overrides["WINEDEBUG"] = validate_wine_debug(options.wine_debug)
    if plan.portable:  # no silent fallback: refuse before anything is created or run
        from dataclasses import replace

        from gitfurl.portable import build_portable_argv, find_bwrap

        bwrap = find_bwrap(base_env)
        # The sandbox starts from an empty environment; only the allowlist and these
        # overrides are set inside it (see gitfurl.portable.build_portable_argv).
        argv = build_portable_argv(replace(plan, argv=argv), bwrap or "", plan.installer,
                                   dict(base_env), overrides)
    scope = None
    if plan.memory_limit:  # no silent fallback: refuse before anything is created or run
        from gitfurl.memlimit import prepare

        scope = prepare(plan.prefix.name, base_env, cgroup_root=options.cgroup_root,
                        meminfo=options.meminfo, interval=options.memory_interval)
        argv = scope.wrap(argv)  # outermost: it wraps bwrap or umu-run as well
    child_env = build_child_env(os.environ if env is None else env, overrides)
    events = None
    if options.detailed_log:
        efd = os.open(log_dir / f"{plan.prefix.name}-{stamp}.events.jsonl",
                      os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        events = os.fdopen(efd, "w")

    def event(kind, **data):
        if events:
            events.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"),
                                     "event": kind, **data}) + "\n")
            events.flush()

    monitor = None
    observer = None
    stall = None
    if options.stall_after_s is not None:
        stall = StallDetector(options.stall_after_s, log_path, options.clock)  # raises if <= 0
    # A crash handler already running in the prefix before this run is not this run's crash.
    crash = {"seen_s": None, "before": frozenset()}
    titles_of = options.window_titles
    if titles_of is None:
        from gitfurl.windows import window_titles as titles_of

    def new_crash(sample) -> bool:
        return bool(set(sample.crash_pids) - crash["before"])

    def windows_now(sample) -> list[str] | None:
        if plan.portable:  # _NET_WM_PID is a pid inside bwrap's namespace: cannot be matched
            return None
        try:
            return titles_of(sample.pids, programs_of(sample), child_env)
        except Exception:  # noqa: BLE001 - a note must never break the run
            return None

    # With stall advice on, each sample also looks at the run's windows (one xwininfo, and
    # xprop only for windows not seen before), so a stall note can name the newest one.
    tracker = None
    if stall is not None and not plan.portable:
        from gitfurl.windows import WindowTracker

        tracker = WindowTracker(child_env, options.window_list)

    def programs_of(sample) -> list[str]:
        programs = list(sample.helpers)
        if sample.crash_pids:  # its "Program Error" window belongs to winedbg.exe
            programs.extend(CRASH_HANDLER_NAMES)
        return programs

    # The monitor also runs for notes alone: it is how a crash is seen.
    if options.on_progress is not None or stall is not None or options.on_note is not None:
        def handle(sample):
            event("progress", elapsed_s=round(sample.elapsed_s), read_bytes=sample.read_bytes,
                  total_bytes=sample.total_bytes, written_bytes=sample.written_bytes,
                  percent=None if sample.percent is None else round(sample.percent, 2))
            observer.update(sample)
            if events:  # detailed log only
                event("processes", count=sample.procs, programs=list(sample.helpers))
                event("memory", mem_available_bytes=memory_available(options.meminfo))
            # At most one look at the windows per sample: the tracker's when it runs (stall
            # advice on), otherwise one only for a crash note. Each look is bounded (TOTAL_S).
            look = None
            if tracker is not None:
                found = tracker.observe(sample.pids, programs_of(sample), sample.elapsed_s)
                look = None if found is None else list(dict.fromkeys(t for _w, t in found))

            def titles_now():
                return look if tracker is not None else windows_now(sample)

            if crash["seen_s"] is None and new_crash(sample):
                crash["seen_s"] = sample.elapsed_s
                titles = titles_now()
                event("crash_handler", elapsed_s=round(sample.elapsed_s),
                      programs=list(sample.helpers), windows=titles, advisory=True)
                if options.on_note is not None:
                    from gitfurl.windows import titles_text

                    options.on_note(
                        "crash: Wine's crash handler (winedbg) started in the prefix, so a "
                        "Windows program there crashed. Its \"Program Error\" window may be "
                        "waiting; the run goes on until the installer exits or you stop it."
                        + titles_text(titles))
            idle = stall.check(sample) if stall else None
            if idle is not None:
                titles = titles_now()
                newest = tracker.newest() if tracker is not None else None
                event("possibly_stalled", idle_s=round(idle), after_s=options.stall_after_s,
                      windows=titles, newest_window=newest[0] if newest else None,
                      newest_window_s=round(newest[1]) if newest else None, advisory=True)
                if options.on_note is not None:
                    from gitfurl.windows import newest_text, titles_text

                    options.on_note(
                        f"possibly stalled: no payload read, CPU use under {QUIET_CPU_PCT:g}% and no log "
                        f"growth for "
                        f"{_dur(idle)}. This is advice only; nothing was stopped, and slow "
                        f"phases can be normal." + titles_text(titles)
                        + (newest_text(newest, sample.elapsed_s) if titles else ""))
            if options.on_progress is not None:
                options.on_progress(sample)

        observer = ActivityObserver()
        monitor = ProgressMonitor(plan.installer, plan.prefix, handle,
                                  options.progress_interval, options.proc_root)
    t0 = time.monotonic()
    # Variable names only, never values: the environment can hold secrets.
    event("start", command=shlex.join(argv), cwd=str(plan.cwd), prefix=str(plan.prefix),
          env_overrides=sorted(overrides), wine=wine, portable=plan.portable,
          unattended=plan.unattended is not None)
    if scope is not None:
        b = scope.budget
        event("memory_limit", unit=scope.unit, memory_high=b.high_bytes, memory_max=b.max_bytes,
              reserve=b.reserve, mem_total=b.total, mem_available=b.available)

        def memory_changed(old, new, available, usage):
            event("memory_limit_change", unit=scope.unit, old_memory_high=old,
                  memory_high=new, mem_available=available, scope_usage=usage)

        scope.on_change = memory_changed
        scope.on_note = options.on_note
    memory_report = None
    unattended_report = None
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as log:
            log.write(f"command: {shlex.join(argv)}\ncwd: {plan.cwd}\nprefix: {plan.prefix}\n")
            if options.scan_summary:
                log.write("".join(f"scan: {ln}\n" for ln in options.scan_summary.splitlines()))
            log.write("---\n")
            log.flush()
            output_start = log.tell()  # the run's own output starts here
            if monitor:
                crash["before"] = frozenset(
                    prefix_processes(plan.prefix, options.proc_root).crash_pids)
                if crash["before"]:
                    log.write("--- Wine's crash handler (winedbg) was already running in this "
                              "prefix before the run; it is not counted as this run's crash\n")
                    log.flush()
                monitor.start()
            if scope is not None:
                scope.start()
            started = True
            try:
                if run is not None:  # tests inject a fake runner
                    proc = run(argv, cwd=plan.cwd, env=child_env, stdout=log,
                               stderr=subprocess.STDOUT, check=False)
                else:
                    if plan.runtime == "wine" and not plan.portable:
                        server = wineserver_for(wine)
                        control.configure_server([server, "-k"] if server else None,
                                                 {**child_env, "WINEPREFIX": str(plan.prefix)})
                    proc = _run_in_group(argv, cwd=plan.cwd, env=child_env, stdout=log,
                                         control=control)
                    started = control.started
            finally:
                try:
                    final = monitor.stop() if monitor else None
                    if final is not None:
                        observer.update(final)
                        if crash["seen_s"] is None and new_crash(final):
                            crash["seen_s"] = final.elapsed_s
                finally:  # the scope's result is read and a failed unit cleared in every case
                    if scope is not None:
                        memory_report = scope.finish()
            if memory_report is not None and proc.returncode != 0 and not control.cancelled \
                    and not memory_report.hit_limit:
                memory_report.start_failure = _start_failure(log, log_path, output_start)
            if control.cancelled and plan.portable:
                control.notes.append("portable run: bwrap's process group was stopped and the "
                                     "sandbox ended with it (--die-with-parent); "
                                     "wineserver -k was not run")
            if control.cancelled:
                log.write(f"\n--- cancelled by user (process exit status {proc.returncode})\n")
                for note in control.notes:
                    log.write(f"--- {note}\n")
            else:
                log.write(f"\n--- exit status {proc.returncode}\n")
            if crash["seen_s"] is not None:
                log.write(f"--- Wine's crash handler (winedbg) was seen {_dur(crash['seen_s'])} "
                          "into the run: a Windows program in the prefix crashed\n")
            if plan.unattended is not None and started:
                from gitfurl.unattended import read_report

                unattended_report = read_report(plan.unattended, plan.prefix,
                                                None if control.cancelled else proc.returncode,
                                                cancelled=control.cancelled)
                for line in unattended_report.lines():
                    log.write(f"--- {line}\n")
            if memory_report is not None:
                for line in memory_report.lines():
                    log.write(f"--- {line}\n")
        if final is not None:
            event("progress_final", line=final.line())
        if unattended_report is not None:
            event("unattended_result", log_found=unattended_report.log_found,
                  succeeded=unattended_report.succeeded, files=unattended_report.files,
                  cancelled=unattended_report.cancelled)
        if memory_report is not None:
            event("memory_limit_result", unit=scope.unit, oom_kill=memory_report.oom_kill,
                  oom_result=memory_report.oom_result, high_events=memory_report.high,
                  changes=len(memory_report.changes))
        facts = {"started": started,
                 "extraction_observed": bool(observer and observer.observed),
                 "observation_measured": bool(observer and observer.measured),
                 "exit_status": proc.returncode if started else None,
                 "crash_seen_s": crash["seen_s"]}
        if control.cancelled:
            event("cancelled", notes=control.notes, seconds=round(time.monotonic() - t0, 1),
                  **facts)
        else:
            event("exit", status=proc.returncode, seconds=round(time.monotonic() - t0, 1),
                  **facts)
    finally:
        if events:
            events.close()
    return ExecResult(CANCELLED_STATUS if control.cancelled else proc.returncode, log_path,
                      memory=memory_report, unattended=unattended_report, **facts)


def run_with_prefix(plan: InstallPlan, wine: str, create_if_missing: bool,
                    env: dict[str, str] | None = None, run=None, on_ready=None,
                    options: RunOptions | None = None):
    """Create the plan's prefix first when asked and needed, then run the installer.

    Returns an ExecResult (unpacks as exit status, log path). If creating the prefix fails, the installer is not run.
    """
    from gitfurl.prefixes import create_prefix

    if plan.portable:  # refuse before a prefix is created for a run that cannot happen
        from gitfurl.portable import check_portable_supported, find_bwrap

        base = os.environ if env is None else env
        check_portable_supported(plan, find_bwrap(base), dict(base), plan.installer)
    opts = options or RunOptions()
    if plan.memory_limit:  # refuse before a prefix is created for a run that cannot happen
        from gitfurl.memlimit import prepare

        prepare(plan.prefix.name, os.environ if env is None else env,
                cgroup_root=opts.cgroup_root, meminfo=opts.meminfo)
    if plan.scan is not None:  # before anything is created; a hit or an error raises ScanBlocked
        options = _scan_gate(plan, opts)
        opts = options
    if not plan.prefix.is_dir():
        if not create_if_missing:
            raise FileNotFoundError(
                f"prefix does not exist: {plan.prefix} (run 'gitfurl cli prefix create')"
            )
        from gitfurl.prefixes import with_wineboot_env

        extra = with_wineboot_env({k: v for k, v in plan.env_overrides.items()
                                   if k != "WINEPREFIX"}, plan.runtime, plan.wine_addons)
        boot_scope = None
        if plan.memory_limit:  # wineboot gets the same limits, fixed (no adjustment)
            from gitfurl.memlimit import prepare

            boot_scope = prepare(plan.prefix.name, os.environ if env is None else env,
                                 cgroup_root=opts.cgroup_root, meminfo=opts.meminfo,
                                 label="wineboot")
        try:
            status = create_prefix(plan.prefix.name, wine, env, run, extra_env=extra,
                                   wrap=boot_scope.wrap if boot_scope else None)
        finally:
            boot_report = boot_scope.finish() if boot_scope else None
        if status != 0:
            hit = " " + boot_report.lines()[0] if boot_report and boot_report.hit_limit else ""
            raise RuntimeError(f"creating the prefix failed (wine exited {status}); "
                               f"the installer was not run.{hit}")
    if on_ready is not None:
        on_ready()  # the prefix exists; e.g. take a "before" snapshot
    return execute(plan, wine, env, run, options)


def _start_failure(log, log_path: Path, start: int) -> str | None:
    """A systemd-run error line in the run's output (up to 64 KiB of it), or None."""
    from gitfurl.memlimit import find_start_failure

    try:
        log.flush()
        with open(log_path, "rb") as fh:
            fh.seek(start)
            text = fh.read(64 * 1024).decode("utf-8", errors="replace")
    except OSError:
        return None
    return find_start_failure(text)


def _scan_gate(plan: InstallPlan, options: RunOptions) -> RunOptions:
    """Run the plan's ClamAV scan (or use the result the caller already has) and decide."""
    from dataclasses import replace

    from gitfurl.scan import ScanBlocked, decide, run_scan

    result = options.scan_result
    if result is None:
        control = options.control
        result = run_scan(plan.scan,
                          should_stop=(lambda: control.cancelled) if control else None)
    proceed, message = decide(result, options.allow_scan_hit)
    if not proceed:
        raise ScanBlocked(message, result)
    if options.on_note is not None:
        options.on_note(message)
    return replace(options, scan_summary=message)


def run_tracked(plan: InstallPlan, wine: str, create_if_missing: bool, track: bool,
                env: dict[str, str] | None = None, run=None, options: RunOptions | None = None):
    """Like run_with_prefix, and when track is true also report what the installer changed.

    Returns a TrackedResult (unpacks as exit status, log path, ChangeReport or None).
    """
    if not track:
        res = run_with_prefix(plan, wine, create_if_missing, env, run, options=options)
        return TrackedResult(**vars(res))
    from gitfurl.changes import compare, take_snapshot

    skip = [logs_root(env), plan.prefix.parent.parent]  # gitfurl's own data and state
    box = {}

    def before():
        box["b"] = take_snapshot(plan.prefix, skip=[logs_root(env)])

    res = run_with_prefix(plan, wine, create_if_missing, env, run, on_ready=before,
                          options=options)
    after = take_snapshot(plan.prefix, skip=skip[:1])
    return TrackedResult(**{**vars(res), "changes": compare(box["b"], after, plan.prefix)})
