"""A plain timeline of one run: what each step did and why, one line per step.

Entries come from the places that already know the facts (the route choice, the approval,
runner.py's run loop, the native unpack); this module only collects and formats them. It
starts no process and reads nothing.

    HH:MM:SS  what - why

Entries that keep changing (progress, a run of data blocks with the same method) carry a fold
key: a new entry with the same key replaces the older one while only such changing entries
came after it, so a run of progress samples is one row that updates.

After the approval the timeline is saved next to the run's log as
`<prefix>-<stamp>.timeline.txt` (mode 0600, created with O_EXCL, never through a link).
Each row is written when it is final (folded rows when they stop changing), so a crash
leaves every row written so far. Text from the installer (window titles, file names) goes
through shown(), the same cleaning gitfurl uses for any shown text; every text is capped.
"""

from __future__ import annotations

import os
import re
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from gitfurl.detect import clean

KINDS = ("plan", "approval", "prefix", "start", "progress", "window", "note", "stall", "crash",
         "archive", "files", "skip", "changes", "cleanup", "end")
ACTS = ("prefix", "start", "archive", "files")  # kinds of rows that mean something was done
MAX_ENTRIES = 2000  # rows kept in memory and written to the file
KEEP_HEAD = 40  # when over the cap, the first rows (the plan) stay; the oldest after them go
MAX_TEXT = 300  # characters of each "what" and "why"
SHOWN_MAX = 120  # characters of a text taken from the installer (a title, a file name)
SEP = " \u2014 "  # " - " between what and why
SUFFIX = ".timeline.txt"
READ_MAX = 4 << 20  # the most of a timeline file read back (gitfurl's own files are smaller)


def shown(text, limit: int = SHOWN_MAX) -> str:
    """Text from the installer (a window title, a file name): printable only, capped."""
    return clean(str(text), limit)


def _tidy(text, limit: int = MAX_TEXT) -> str:
    """One line: no control characters, no line breaks, capped."""
    one = " ".join(str(text).split())
    return clean(one, limit)


@dataclass(frozen=True)
class Entry:
    time: datetime
    kind: str
    what: str
    why: str = ""
    fold: object = None  # entries with the same key fold into one row


def format_entry(e: Entry) -> str:
    """'HH:MM:SS  what - why' (the why only when there is one)."""
    head = f"{e.time:%H:%M:%S}  {e.what}"
    return head + (SEP + e.why if e.why else "")


def format_lines(entries, left_out: int = 0, at: int = KEEP_HEAD) -> list[str]:
    lines = [format_entry(e) for e in entries]
    if left_out:
        lines.insert(min(at, len(lines)),
                     f"          ({left_out} earlier rows left out here; the timeline keeps "
                     f"{MAX_ENTRIES})")
    return lines


def size_text(n: int) -> str:
    """1.6 GB, 420 MB, 12 KB: short and in powers of ten, like the progress line."""
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} bytes"


def progress_text(label: str, done: int, total: int) -> str:
    """'Reading the installer's data: 42 %, 1.6 GB of 3.8 GB'."""
    if total <= 0:
        return f"{label}: {size_text(done)}"
    pct = min(100, int(100 * done / total))
    return f"{label}: {pct} %, {size_text(done)} of {size_text(total)}"


def duration_text(seconds: float) -> str:
    s = int(round(seconds))
    if s < 60:
        return f"{s} s"
    m, s = divmod(s, 60)
    if m < 60:
        return f"{m} min {s:02d} s"
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min"


class Timeline:
    """Collects a run's entries. Safe to call from the run's threads.

    `clock` gives the time of each entry (tests inject it). `on_line(text)` gets each row
    when it is final (the command line's --timeline prints them).
    """

    def __init__(self, clock=datetime.now, max_entries: int = MAX_ENTRIES, on_line=None):
        self._clock = clock
        self._max = max(KEEP_HEAD + 2, max_entries)
        self._lock = threading.Lock()
        self._entries: list[Entry] = []
        self._final = 0  # rows before this index are final (written)
        self._left_out = 0
        self._written = 0
        self._file = None
        self.path: Path | None = None
        self.log_path: Path | None = None  # the run's own log, when there is one
        self.on_line = on_line
        self.version = 0  # grows with every change (the window redraws when it moves)
        self.ended = False
        self.problem: str | None = None  # why the file could not be written, if so

    # ---- adding ----
    def add(self, kind: str, what: str, why: str = "", fold=None) -> None:
        if kind not in KINDS:
            raise ValueError(f"unknown timeline kind: {kind}")
        entry = Entry(self._clock(), kind, _tidy(what), _tidy(why) if why else "", fold)
        with self._lock:
            if self.ended:
                return
            tail = self._entries[self._final:]
            if fold is not None:
                for i in range(len(tail) - 1, -1, -1):
                    if tail[i].fold == fold:
                        del self._entries[self._final + i]
                        break
            else:
                self._finalise()
            self._entries.append(entry)
            if fold is None:
                self._finalise()
            self._trim()
            self.version += 1

    def progress(self, what: str, why: str = "", key="progress") -> None:
        """A progress row that updates. A sample that says the same as the last progress
        row (nothing moved while other rows came in between) adds no row."""
        with self._lock:
            last = next((e for e in reversed(self._entries) if e.fold == key), None)
            if last is not None and (last.what, last.why) == (_tidy(what), _tidy(why)
                                                               if why else ""):
                return
        self.add("progress", what, why, fold=key)

    def end(self, what: str, why: str = "") -> None:
        """The outcome in one sentence. Later entries are ignored; the file is closed."""
        if self.ended:
            return
        self.add("end", what, why)
        with self._lock:
            self.ended = True
            self._close()

    # ---- reading ----
    def entries(self) -> list[Entry]:
        with self._lock:
            return list(self._entries)

    def lines(self) -> list[str]:
        with self._lock:
            return format_lines(self._entries, self._left_out)

    def text(self) -> str:
        return "\n".join(self.lines())

    # ---- the file ----
    def attach(self, path: Path) -> None:
        """Start the file at `path` (a new file, mode 0600, never through a link) and write
        every final row so far. A file that cannot be made is noted in `problem`; the run
        goes on without it."""
        with self._lock:
            if self._file is not None or self.ended:
                return
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600)
            except OSError as exc:
                self.problem = f"the timeline file could not be written ({exc.strerror or exc})"
                return
            self._file = os.fdopen(fd, "w", encoding="utf-8")
            self.path = Path(path)
            for e in self._entries[:self._final]:
                self._write(e)

    def save(self, log_dir: Path, prefix_name: str) -> None:
        """attach() under a new name in `log_dir` when the run made no log of its own (it
        stopped before Setup or the unpack started). Only when the run did something after
        the approval (the prefix creation started): a run refused before that, for example by
        the scan, writes nothing, as before."""
        if self._file is not None or self.ended or not self.acted():
            return
        log_dir = Path(log_dir)
        try:
            log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            self.problem = f"the timeline file could not be written ({exc.strerror or exc})"
            return
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        for n in range(1, 10):
            path = log_dir / (f"{prefix_name}-{stamp}" + (f"-{n}" if n > 1 else "") + SUFFIX)
            if not os.path.lexists(path):
                self.attach(path)
                return
        self.problem = "the timeline file could not be written (too many in one second)"

    def acted(self) -> bool:
        """A step that changes something (prefix creation, Setup, the unpack) came after the
        approval."""
        with self._lock:
            entries = list(self._entries)
        at = max((i for i, e in enumerate(entries) if e.kind == "approval"), default=None)
        return at is not None and any(e.kind in ACTS for e in entries[at + 1:])

    def close(self) -> None:
        with self._lock:
            self._close()

    # ---- inside the lock ----
    def _finalise(self) -> None:
        for e in self._entries[self._final:]:
            self._write(e)
        self._final = len(self._entries)

    def _write(self, e: Entry) -> None:
        line = format_entry(e)
        if self.on_line is not None:
            try:
                self.on_line(line)
            except Exception:  # noqa: BLE001 - a print must never stop the run
                pass
        if self._file is None:
            return
        try:
            if self._written < self._max or e.kind == "end":
                self._file.write(line + "\n")
            elif self._written == self._max:
                self._file.write(f"          (more rows left out; the file keeps {self._max})\n")
            self._written += 1
            self._file.flush()
        except OSError as exc:
            self.problem = f"writing the timeline file failed ({exc.strerror or exc})"
            self._file = None

    def _trim(self) -> None:
        while len(self._entries) > self._max:
            del self._entries[KEEP_HEAD]
            self._final -= 1
            self._left_out += 1

    def _close(self) -> None:
        self._finalise()
        if self._file is not None:
            try:
                self._file.close()
            except OSError:
                pass
            self._file = None


# ---- entries from the plan, shared by the command line and the window ----------------------

def detection_entries(tl: Timeline, det) -> None:
    """What the headers and archive indexes showed (a gitfurl.detect.Detection)."""
    if det is None:
        return
    if not det.readable:
        tl.add("plan", "The installer could not be read as a regular file")
        return
    for ev in det.evidence:
        first = ev.detail.split("; ")[0]
        tl.add("plan", f"Detected {ev.format} ({ev.confidence})",
               first + (f" ({ev.where})" if ev.where and len(ev.where) < 120 else ""))
        for line in ev.lines:
            text = line.strip()
            if text.startswith("warning:"):
                first, _sep, rest = text[len("warning:"):].strip().partition("; ")
                tl.add("note", f"Warning: {first}",
                       (rest + "; " if rest else "") + "seen in the setup data before the run")


ROUTE_WORDS = {"native": "gitfurl unpacks the archives itself",
               "wine": "Setup runs under Wine/Proton",
               "unsupported": "no route"}


def route_entries(tl: Timeline, choice) -> None:
    """The route and every reason the plan gives for it (a gitfurl.route.RouteChoice)."""
    if choice is None:
        return
    # "Setup is not run" has its own row (skip_entries); the route's own words are the title.
    reasons = [r for r in choice.reasons
               if r not in (ROUTE_WORDS.get(choice.route), "Setup is not run")]
    by = "you chose it" if choice.chosen_by == "you" else "gitfurl chose it"
    tl.add("plan", f"Route: {choice.route} ({ROUTE_WORDS.get(choice.route, choice.route)}; {by})",
           reasons[0] if reasons else "")
    for r in reasons[1:]:
        tl.add("plan", "Reason for the route", r)


def prefix_entry(tl: Timeline, path: Path, exists: bool, create: bool) -> None:
    name = shown(Path(path).name, 80)
    if exists:
        tl.add("prefix", f"Prefix {name}: existing",
               f"used as it is, nothing in it is removed ({path})")
    elif create:
        tl.add("prefix", f"Prefix {name}: new",
               f"it does not exist yet; the plan creates it first ({path})")
    else:
        tl.add("prefix", f"Prefix {name}: missing", f"creating it was not asked for ({path})")


def skip_entries(tl: Timeline, choice, target: str | None = None,
                 setup_entries: bool = False) -> None:
    """What the route leaves out, in the plan's words. With setup_entries on, the setup's own
    registry entries and shortcuts are added after the unpack (listed in the plan), so they
    are not named as skipped."""
    if choice is None:
        return
    if choice.route == "native":
        where = target or "C:\\Games\\PREFIX"
        left = ("Setup's own files, redistributables it would run and every step of its "
                "script; the registry entries and shortcuts it declares are added after the "
                "unpack, as the plan lists" if setup_entries else
                "Setup's own files, shortcuts, registry entries, redistributables it would "
                "run and every step of its script")
        tl.add("skip", "Setup is not run",
               f"skipped: {left}; the archives are unpacked with their own folder layout "
               f"into {where}")
    elif choice.route == "wine":
        tl.add("skip", "gitfurl does not unpack anything itself",
               "Setup does all of its own steps")


def plan_entries(tl: Timeline, det, choice, prefix: Path, exists: bool, create: bool,
                 target: str | None = None, setup_entries: bool = False) -> None:
    detection_entries(tl, det)
    route_entries(tl, choice)
    prefix_entry(tl, prefix, exists, create)
    skip_entries(tl, choice, target, setup_entries)


def approval_entry(tl: Timeline, how: str) -> None:
    """how: "window" (the approval dialog), "typed" (a typed yes) or "yes" (--yes)."""
    if how == "yes":
        tl.add("approval", "Approved with --yes",
               "the command line was given --yes, so it did not ask; nothing ran before this")
    elif how == "typed":
        tl.add("approval", "You approved the plan", "you typed yes; nothing ran before this")
    else:
        tl.add("approval", "You approved the plan",
               "you answered Yes to the plan in the window; nothing ran before this")


def end_words(headline: str, details: str = "") -> tuple[str, str]:
    """A result headline as the end row: its first sentence, and the next one (or the first
    line of the details) as the reason."""
    parts = re.split(r"(?<=[.!?])\s+", headline.strip(), maxsplit=2)
    what = parts[0] if parts and parts[0] else "The run ended"
    why = parts[1] if len(parts) > 1 else details.strip().partition("\n")[0]
    return what, why


def changes_entry(tl: Timeline, report) -> None:
    """A gitfurl.changes.ChangeReport in one row."""
    if report is None:
        return
    tl.add("changes", f"Changes: {len(report.prefix_new)} new, {len(report.prefix_changed)} "
           f"changed, {len(report.prefix_removed)} removed in the prefix",
           (f"also {len(report.host_new) + len(report.host_changed) + len(report.host_removed)}"
            " outside it, in the places checked" if report.host_touched() else
            "nothing changed in the other places checked")
           + (f"; {len(report.executables)} possible program(s) found"
              if report.executables else ""))


# ---- reading saved timelines back (the window's Last run and `gitfurl cli log`) ------------

_NAME = re.compile(r"(?P<p>.+)-(?P<s>\d{8}-\d{6})(?:-(?P<n>\d))?" + re.escape(SUFFIX))


def kept(log_dir: Path, prefix_name: str | None = None) -> list[Path]:
    """The timeline files in `log_dir` (for one prefix when named), oldest first. Regular
    files only; a link is left out. Reads names and file types only."""
    out = []
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    for name in names:
        m = _NAME.fullmatch(name)
        if m is None or (prefix_name is not None and m["p"] != prefix_name):
            continue
        path = Path(log_dir) / name
        if path.is_file() and not path.is_symlink():
            out.append((m["s"], int(m["n"] or 1), name, path))
    return [p for *_k, p in sorted(out)]


def latest(log_dir: Path, prefix_name: str) -> Path | None:
    found = kept(log_dir, prefix_name)
    return found[-1] if found else None


def read_text(path: Path, limit: int = READ_MAX) -> str:
    """A saved timeline, read without following a link, at most `limit` bytes, every line
    cleaned again (the file could have been edited since)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        data = fh.read(limit + 1)
    text = data[:limit].decode("utf-8", errors="replace")
    lines = [clean(ln, 2 * MAX_TEXT + 40) for ln in text.splitlines()]
    if len(data) > limit:
        lines.append("(the rest of the file was not read)")
    return "\n".join(lines)


def log_for(path: Path) -> Path | None:
    """The run's own log beside a timeline file, when there is one."""
    p = Path(path)
    log = p.with_name(p.name[:-len(SUFFIX)] + ".log")
    return log if log.is_file() and not log.is_symlink() else None

