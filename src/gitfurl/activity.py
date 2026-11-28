"""What a run is doing right now, for the live activity view (command line and window).

Native route: the decoders in farc_codecs.py keep plain counters (output made, time spent, the
archive bytes read, srep's kept bytes, cm jobs); extract.py tells an Activity record which
block, chain and file it is on. Nothing here is called per byte, and nothing locks the
decoders: snapshot() reads their counters from another thread (about once a second), and the
record's own state changes once per file or block, under a lock.

Wine route: wine_lines() turns a progress.Sample (processes in gitfurl's own process group or
the prefix, their CPU time and memory, the newest file written in the prefix, the write
rate) into the same kind of text.

Every figure is measured except the time left, which is an estimate and says so. A quiet
period is described with what is still moving; this module never calls a run stuck.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field

from gitfurl.detect import clean
from gitfurl.timeline import duration_text, size_text

WINDOW_S = 10.0  # throughput is measured over the last this many seconds
QUIET_S = 30.0  # no new output for this long: say what is still moving
ETA_MIN_FRACTION = 0.01  # the archive share read before a time-left estimate is shown
BUSY_CPU_PCT = 50.0  # a process using this much of one core is shown as busy


@dataclass(frozen=True)
class StageView:
    name: str  # gitfurl's own stage name (lzma, srep, cm, ...)
    bytes_in: int
    bytes_out: int
    busy_s: float  # this stage's own decoding time (the stages it reads from left out)
    detail: str = ""  # stage-specific counters (cm jobs, srep's kept bytes)


@dataclass(frozen=True)
class Snapshot:
    active: bool = False  # a block is being decoded
    elapsed_s: float = 0.0
    archive: str = ""
    block: int = 0  # 1-based, in its archive
    blocks: int = 0
    method: str = ""
    block_comp: int = 0  # the block's stored size
    block_read: int = 0  # ... of it read from the archive so far
    block_out: int = 0  # bytes decoded for its files so far
    block_size: int = 0  # its files' total size
    files_done: int = 0  # in this block
    files: int = 0
    current_file: str | None = None
    stages: tuple = ()  # StageView, from the file data to the stored data
    run_comp: int = 0  # stored bytes of every block to decode
    run_read: int = 0
    run_files_done: int = 0
    run_files: int = 0
    read_bps: float = 0.0  # archive bytes read per second over the last WINDOW_S
    out_bps: float = 0.0  # decoded bytes per second over the last WINDOW_S
    eta_s: float | None = None  # an estimate from the archive bytes read
    cpu_pct: float | None = None  # this process's CPU use over the last WINDOW_S
    window_s: float = 0.0  # the span those rates cover (WINDOW_S, or the time between samples)
    changed_s: float = 0.0  # seconds since any counter changed
    out_quiet_s: float = 0.0  # seconds since decoded output last grew
    read_quiet_s: float = 0.0  # seconds since the archive position last moved
    finished: bool = False

    @property
    def block_pct(self) -> float:
        return 100.0 * self.block_read / self.block_comp if self.block_comp else 100.0


@dataclass
class _Block:
    archive: str
    index: int
    blocks: int
    method: str
    comp: int
    size: int
    files: int
    parts: list  # (name, reader) from the stored data to the file data
    t0: float
    files_done: int = 0
    current: str | None = None


@dataclass
class _Marks:
    """Last values seen by snapshot(), with when each last changed."""
    sig: tuple = ()
    out: int = -1
    read: int = -1
    t_sig: float = 0.0
    t_out: float = 0.0
    t_read: float = 0.0
    window: deque = field(default_factory=deque)  # (t, run_read, run_out, cpu_s)


def _cpu_seconds() -> float:
    t = os.times()
    return t.user + t.system


class Activity:
    """The native unpack's live record. The unpack thread calls begin_run, begin_block,
    file_start, file_end and end_block; any thread may call snapshot()."""

    def __init__(self, clock=time.monotonic, cpu=_cpu_seconds):
        self.clock, self.cpu = clock, cpu
        self._lock = threading.Lock()
        self._t0 = clock()
        self._block: _Block | None = None
        self._run_comp = self._run_files = 0
        self._done_comp = self._done_out = self._files_done = 0
        self._finished = False
        self._marks = _Marks(t_sig=self._t0, t_out=self._t0, t_read=self._t0)
        self.summaries: list[list[str]] = []  # one list of stage rows per finished block
        self.last_block_s = 0.0  # how long the last finished block took

    # -- the unpack thread -----------------------------------------------------------------

    def begin_run(self, comp: int, files: int) -> None:
        with self._lock:
            self._t0 = self.clock()
            self._run_comp, self._run_files = comp, files
            self._marks = _Marks(t_sig=self._t0, t_out=self._t0, t_read=self._t0)

    def begin_block(self, archive: str, index: int, blocks: int, method: str, comp: int,
                    size: int, files: int, parts: list) -> None:
        with self._lock:
            self._block = _Block(clean(archive, 80), index, blocks, method, comp, size, files,
                                 list(parts), self.clock())

    def file_start(self, name: str) -> None:
        b = self._block
        if b is not None:
            b.current = name

    def file_end(self) -> None:
        with self._lock:
            b = self._block
            if b is not None:
                b.files_done += 1
                b.current = None
            self._files_done += 1

    def end_block(self) -> list[str]:
        """The block's stage rows ("cm: 17 jobs, 8.9 GB, 3 min 12 s"), also kept in
        `summaries`. The block's stored size counts as read from now on."""
        with self._lock:
            b = self._block
            if b is None:
                return []
            rows = [summary_row(v) for v in _stage_views(b.parts)]
            self.last_block_s = self.clock() - b.t0
            self._done_comp += b.comp
            self._done_out += _out(b.parts)
            self._block = None
            self.summaries.append(rows)
            return rows

    def finish(self) -> None:
        with self._lock:
            self._block = None
            self._finished = True

    # -- any thread ------------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            now = self.clock()
            b = self._block
            views = tuple(_stage_views(b.parts)) if b is not None else ()
            read = min(b.comp, _taken(b.parts)) if b is not None else 0
            out = _out(b.parts) if b is not None else 0
            run_read, run_out = self._done_comp + read, self._done_out + out
            m = self._marks
            sig = (run_read, run_out, self._files_done,
                   tuple(_counters(r) for _n, r in b.parts) if b is not None else ())
            if sig != m.sig:
                m.sig, m.t_sig = sig, now
            if run_out != m.out:
                m.out, m.t_out = run_out, now
            if run_read != m.read:
                m.read, m.t_read = run_read, now
            cpu_s = self.cpu()
            w = m.window
            w.append((now, run_read, run_out, cpu_s))
            while len(w) > 2 and w[1][0] <= now - WINDOW_S:
                w.popleft()
            t_a, read_a, out_a, cpu_a = w[0]
            span = now - t_a
            read_bps = (run_read - read_a) / span if span > 0 else 0.0
            out_bps = (run_out - out_a) / span if span > 0 else 0.0
            cpu_pct = 100.0 * (cpu_s - cpu_a) / span if span > 0 else None
            eta = None
            if read_bps > 0 and self._run_comp and run_read >= ETA_MIN_FRACTION * self._run_comp:
                eta = max(0.0, (self._run_comp - run_read) / read_bps)
            return Snapshot(
                active=b is not None, elapsed_s=now - self._t0,
                archive=b.archive if b else "", block=b.index + 1 if b else 0,
                blocks=b.blocks if b else 0, method=b.method if b else "",
                block_comp=b.comp if b else 0, block_read=read, block_out=out,
                block_size=b.size if b else 0, files_done=b.files_done if b else 0,
                files=b.files if b else 0, current_file=b.current if b else None,
                stages=views, run_comp=self._run_comp, run_read=run_read,
                run_files_done=self._files_done, run_files=self._run_files,
                read_bps=read_bps, out_bps=out_bps, eta_s=eta, cpu_pct=cpu_pct, window_s=span,
                changed_s=now - m.t_sig, out_quiet_s=now - m.t_out,
                read_quiet_s=now - m.t_read, finished=self._finished)


# --- reading the decoders' counters (plain attributes; see farc_codecs) ---------------------

def _taken(parts) -> int:
    return sum(getattr(r, "taken", 0) for _n, r in parts)


def _counters(r) -> tuple:
    """What counts as a change (a running job's clock does not)."""
    return (getattr(r, "made", 0), getattr(r, "taken", 0), getattr(r, "kept", 0),
            getattr(r, "jobs_started", 0), getattr(r, "jobs_done", 0))


def _out(parts) -> int:
    if not parts:
        return 0
    top = parts[-1][1]
    return top.taken if parts[-1][0] == "source" else getattr(top, "made", 0)


def _stage_views(parts) -> list[StageView]:
    """From the file data to the stored data (the archive's order)."""
    views = []
    for i in range(len(parts) - 1, 0, -1):
        name, st = parts[i]
        below_name, below = parts[i - 1]
        if below_name == "source":  # cm and rolz read the archive themselves, srep may too
            bytes_in = below.taken + getattr(st, "taken", 0)
            inner = 0.0
        else:
            bytes_in, inner = below.made, below.busy
        views.append(StageView(name, bytes_in, st.made, max(0.0, st.busy - inner),
                               _detail(name, st)))
    return views


def _detail(name: str, st) -> str:
    if name == "cm":
        total = st.jobs_total
        of = f" of {total}" if total is not None else ""
        if st.jobs_started > st.jobs_done:
            return (f"job {st.jobs_started}{of} ({size_text(st.job_in)} stored), running for "
                    f"{duration_text(time.perf_counter() - st.job_t0)}")
        if st.jobs_done:
            return f"{st.jobs_done}{of} jobs done"
        return f"no job started yet{(' (' + str(total) + ' jobs)') if total else ''}"
    if name == "srep":
        return f"keeps {size_text(st.kept)} of its {size_text(st.budget)} cap"
    return ""


def summary_row(v: StageView) -> str:
    """One stage at the end of its block: "cm: 17 jobs, 8.9 GB, 3 min 12 s"."""
    if v.name == "cm" and v.detail:
        jobs = v.detail.split(" ", 1)[0]
        head = f"{jobs} job{'s' if jobs != '1' else ''}, " if jobs.isdigit() else ""
        return f"cm: {head}{size_text(v.bytes_out)}, {_dur(v.busy_s)}"
    return f"{v.name}: {size_text(v.bytes_in)} in, {size_text(v.bytes_out)} out, {_dur(v.busy_s)}"


def _dur(seconds: float) -> str:
    return "under 1 s" if seconds < 0.5 else duration_text(seconds)


def _eta(seconds: float) -> str:
    return "under 1 s left (estimate)" if seconds < 0.5 else \
        f"about {duration_text(seconds)} left (estimate)"


def _span(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 9.95 else duration_text(seconds)


# --- wording --------------------------------------------------------------------------------

def quiet_text(s: Snapshot, after_s: float = QUIET_S) -> str | None:
    """What is still moving when no new output came for `after_s` seconds, or None."""
    if not s.active or s.out_quiet_s < after_s:
        return None
    head = f"no new output for {duration_text(s.out_quiet_s)}"
    if s.read_quiet_s < after_s:
        return (f"{head}; the decoder is still reading the archive "
                f"({s.block_pct:.0f} % of this block)")
    if s.changed_s < after_s:
        return f"{head}; the decoders are still working (their counters still change)"
    cpu = f"; gitfurl used {s.cpu_pct:.0f} % CPU over the last {_span(s.window_s)}" \
        if s.cpu_pct is not None else ""
    return (f"{head}, and no counter has changed for {duration_text(s.changed_s)}{cpu}; one "
            "large step (a cm job, an x86 code block) gives no figure until it ends. Stop "
            "still works")


def _rate(bps: float) -> str:
    return f"{size_text(int(bps))}/s"


def _stage_line(v: StageView) -> str:
    text = f"{v.name}: {size_text(v.bytes_in)} in, {size_text(v.bytes_out)} out"
    return f"{text}; {v.detail}" if v.detail else text


def block_lines(s: Snapshot) -> list[str]:
    """The multi-line block (terminal and window): block, stages, file, rates, estimate."""
    if s.finished and not s.active:
        return [f"native unpack: finished after {duration_text(s.elapsed_s)}; "
                f"{s.run_files_done} of {s.run_files} files written"]
    if not s.active:
        return [f"native unpack: {duration_text(s.elapsed_s)}; between data blocks; "
                f"{s.run_files_done} of {s.run_files} files written"]
    lines = [f"native unpack: data block {s.block} of {s.blocks} in {s.archive} ({s.method})",
             f"  archive read: {size_text(s.block_read)} of {size_text(s.block_comp)} "
             f"({s.block_pct:.0f} % of this block); decoded {size_text(s.block_out)} of "
             f"{size_text(s.block_size)}"]
    lines += [f"  {_stage_line(v)}" for v in s.stages]
    cur = f", now {s.current_file}" if s.current_file else ""
    lines.append(f"  files: {s.files_done} of {s.files} in this block{cur}")
    eta = _eta(s.eta_s) if s.eta_s is not None \
        else "no estimate yet"
    lines.append(f"  last {_span(s.window_s)}: archive {_rate(s.read_bps)}, decoded "
                 f"{_rate(s.out_bps)}; {eta}" if s.window_s > 0 else
                 f"  rates: not measured yet; {eta}")
    quiet = quiet_text(s)
    lines.append(f"  {quiet}" if quiet else
                 f"  last change {duration_text(s.changed_s)} ago; elapsed "
                 f"{duration_text(s.elapsed_s)}")
    return lines


def one_line(s: Snapshot) -> str:
    """The same in one line (output that is not a terminal: logs)."""
    if not s.active:
        return block_lines(s)[0]
    busy = "; ".join(_stage_line(v) for v in s.stages if v.detail)
    eta = f", {_eta(s.eta_s)}" if s.eta_s is not None else ""
    quiet = quiet_text(s)
    return (f"native unpack: block {s.block}/{s.blocks} ({s.method}), archive "
            f"{s.block_pct:.0f} % of this block, decoded {size_text(s.block_out)}, files "
            f"{s.files_done}/{s.files}, {_rate(s.read_bps)} read{eta}"
            + (f"; {busy}" if busy else "")
            + (f"; {quiet}" if quiet else f"; last change {duration_text(s.changed_s)} ago"))


def panel_text(s: Snapshot | None) -> str:
    """The window's Activity panel."""
    if s is None:
        return "Shown while a run works: what each step is doing right now."
    return "\n".join(ln[2:] if ln.startswith("  ") else ln for ln in block_lines(s))


# --- the Wine route ---------------------------------------------------------------------------

def wine_lines(sample) -> list[str]:
    """Processes, the newest file and the write rate from a progress.Sample."""
    lines = []
    procs = getattr(sample, "processes", ())
    for name, cpu, rss in procs[:6]:
        if cpu is None:
            state = "CPU not measured yet"
        elif cpu >= BUSY_CPU_PCT:
            state = f"CPU {cpu:.0f} %, busy"
        elif cpu > 0:
            state = f"CPU {cpu:.0f} %, CPU time growing"
        else:
            state = "CPU 0 %, CPU time not growing"
        lines.append(f"{clean(str(name), 40)}: {state}, {size_text(rss)} memory")
    if len(procs) > 6:
        lines.append(f"... and {len(procs) - 6} more processes")
    newest = getattr(sample, "newest", None)
    if newest:
        age = getattr(sample, "newest_age_s", None)
        when = f", {duration_text(age)} ago" if age is not None else ""
        lines.append(f"newest file in the prefix: {newest}{when}")
    rate = getattr(sample, "write_bps", None)
    if rate is not None:
        lines.append(f"writing to the prefix: {_rate(rate)}")
    return lines


def wine_panel_text(sample) -> str:
    return "\n".join([sample.line(), *wine_lines(sample)])


# --- the command line -------------------------------------------------------------------------

class Printer:
    """Prints the record every `interval` seconds on its own thread: the block (a terminal)
    or one line (anything else). Lines only, never redrawn in place, so the output stays
    the same in a log. stop() prints the last state once."""

    MIN_INTERVAL = 0.2

    def __init__(self, record: Activity, interval: float, write, terminal: bool):
        self.record, self.write, self.terminal = record, write, terminal
        self.interval = max(self.MIN_INTERVAL, interval)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True)

    def text(self) -> str:
        s = self.record.snapshot()
        if self.terminal:
            return "\n".join(f"[progress] {ln}" if i == 0 else f"           {ln.lstrip()}"
                             for i, ln in enumerate(block_lines(s)))
        return f"[progress] {one_line(s)}"

    def start(self) -> None:
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.write(self.text())
            except Exception:  # noqa: BLE001, S110 - progress must never break the unpack
                pass

    def stop(self, final: bool = True) -> None:
        self._stop.set()
        self._thread.join(timeout=5.0)
        if final:
            self.write(self.text())


class CliProgress:
    """--progress for a native run: a record and its Printer (or neither without --progress).
    Use as a context manager around the unpack; the last state is printed once unless the
    unpack was interrupted."""

    def __init__(self, enabled: bool, interval: float, write=None, terminal: bool | None = None):
        import sys

        self.record = Activity() if enabled else None
        if terminal is None:
            terminal = sys.stdout.isatty()
        write = write or (lambda text: print(text, flush=True))
        self.printer = Printer(self.record, interval, write, terminal) if enabled else None

    def kwargs(self) -> dict:
        return {"activity": self.record} if self.record is not None else {}

    def __enter__(self):
        if self.printer is not None:
            self.printer.start()
        return self

    def __exit__(self, kind, exc, tb):
        if self.printer is not None:
            self.printer.stop(final=kind is None)
        return False


def wine_progress_text(sample, terminal: bool) -> str:
    """--progress for a Setup run: the sample's line, then (on a terminal) one line per
    process, the newest file and the write rate; elsewhere the busiest process and the write
    rate are added to the one line."""
    if terminal:
        return "\n".join([f"[progress] {sample.line()}",
                          *(f"           {ln}" for ln in wine_lines(sample))])
    procs = getattr(sample, "processes", ())
    busiest = max(procs, key=lambda p: p[1] or 0.0, default=None)
    tail = ""
    if busiest is not None and busiest[1] is not None:
        tail = f"; busiest: {clean(str(busiest[0]), 40)} CPU {busiest[1]:.0f} %"
    rate = getattr(sample, "write_bps", None)
    if rate is not None:
        tail += f"; writing {_rate(rate)}"
    return f"[progress] {sample.line()}{tail}"
