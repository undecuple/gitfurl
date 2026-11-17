"""Optional ClamAV scan of the installer before a run. Off by default.

gitfurl only runs the `clamscan` program that is already on PATH. It never installs ClamAV,
never runs `freshclam` and makes no network request: you keep the signature database up to
date yourself. The scan is read-only. No option that removes, moves or copies files is ever
passed, and nothing is written into the installer's folder.

What is scanned: the installer file and the regular files directly beside it in its folder,
named one by one on the command line. Subfolders and symbolic links are not scanned.

Documented behaviour this relies on (clamscan(1), ClamAV's docs/man/clamscan.1.in):
  return codes 0 "No virus found.", 1 "Virus(es) found.", 2 "Some error(s) occurred.";
  --no-summary "Do not display summary at the end of scanning.";
  --stdout "Write all messages (except for libclamav output) to the standard output".
The result lines ("PATH: OK", "PATH: SIGNAME FOUND", "PATH: Empty file", "PATH: MSG ERROR")
and the version line ("ClamAV ENGINE/DBVERSION/" followed by ctime() of the daily database)
come from ClamAV's source (clamscan/manager.c, common/misc.c), not from the man page.

A hit blocks the run unless the user overrides it. A scan error also blocks it: there is no
silent fallback to running unscanned. Output that cannot be read counts as an error, never as
a clean result. A clean result means only that no known signature matched.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gitfurl.detect import clean
from gitfurl.procgroup import SigintGuard, group_alive

DEFAULT_TIMEOUT_S = 600.0
VERSION_TIMEOUT_S = 5
OUTPUT_CAP = 1024 * 1024  # bytes of stdout and of stderr kept; the rest is read and dropped
MAX_FILES = 1000  # files beside the installer; a bigger folder is refused, not cut short
DB_WARN_DAYS = 7
# clamscan(1): --max-filesize "(default: 100 MB, max: 2 GB)"; --alert-exceeds-max is off by
# default and the man page does not say what an over-limit file reports. gitfurl does not
# rely on that: files above this size are named as possibly not fully scanned.
LIMIT_BYTES = 100 * 1000 * 1000
STOP_GRACE_S = 2.0

# The only options gitfurl ever passes. Nothing here removes, moves or copies a file.
SCAN_OPTIONS = ("--no-summary", "--stdout")
FORBIDDEN_OPTIONS = ("--remove", "--move", "--copy")

CLEAN, HIT, ERROR = "clean", "hit", "error"

FALSE_POSITIVE_NOTE = ("ClamAV often flags game installers and repacks falsely (packers and "
                       "self-extractors look like malware to signatures).")


class ScanBlocked(RuntimeError):
    """The scan stopped the run. `result` is the ScanResult (None if it never ran)."""

    def __init__(self, message: str, result: ScanResult | None = None):
        super().__init__(message)
        self.result = result


def find_clamscan() -> str | None:
    """Absolute path of clamscan on PATH, or None. A relative result is refused."""
    found = shutil.which("clamscan")
    return found if found and os.path.isabs(found) else None


@dataclass(frozen=True)
class ScanPlan:
    clamscan: str
    files: tuple[str, ...]
    timeout_s: float = DEFAULT_TIMEOUT_S
    skipped: int = 0  # entries beside the installer that are not scanned (folders, links, ...)
    large: tuple[str, ...] = ()  # files above ClamAV's default per-file limit (LIMIT_BYTES)

    @property
    def argv(self) -> list[str]:
        return [self.clamscan, *SCAN_OPTIONS, *self.files]


def build_scan_plan(installer: Path, clamscan: str | None,
                    timeout_s: float = DEFAULT_TIMEOUT_S) -> ScanPlan:
    """The scan's argument list. Reads directory entries only; runs nothing. Raises ValueError.

    Every path is absolute, so none can be read as an option.
    """
    if not clamscan:
        raise ValueError("clamscan was not found on PATH. gitfurl does not install ClamAV; "
                         "install it yourself, or turn the scan off")
    if not os.path.isabs(clamscan):
        raise ValueError("clamscan must be an absolute path")
    if not timeout_s or timeout_s <= 0:
        raise ValueError("the scan timeout must be above 0")
    installer = Path(installer).resolve()
    files, skipped, seen = [], 0, 0
    try:
        with os.scandir(installer.parent) as it:
            for e in it:
                seen += 1
                if seen > MAX_FILES:
                    raise ValueError(
                        f"more than {MAX_FILES} entries beside the installer; move the installer "
                        "into a folder of its own to scan it")
                if e.name == installer.name:
                    continue
                try:
                    regular = e.is_file(follow_symlinks=False)
                except OSError:
                    regular = False
                if regular:
                    files.append(os.path.join(installer.parent, e.name))
                else:
                    skipped += 1
    except OSError as exc:
        raise ValueError(f"could not list the installer's folder ({exc.strerror or exc})") from exc
    files.sort()
    files.insert(0, str(installer))
    large = []
    for f in files:
        if "\n" in f or "\r" in f:
            raise ValueError(f"a file name contains a line break ({clean(Path(f).name, 60)}); "
                             "its scan result could not be read reliably")
        try:
            if os.stat(f, follow_symlinks=False).st_size > LIMIT_BYTES:
                large.append(f)
        except OSError:
            pass
    return ScanPlan(clamscan, tuple(files), float(timeout_s), skipped, tuple(large))


def large_note(large: tuple[str, ...]) -> str:
    names = ", ".join(clean(Path(f).name, 60) for f in large[:5])
    more = f" and {len(large) - 5} more" if len(large) > 5 else ""
    return (f"{len(large)} file(s) are larger than ClamAV's default per-file limit (100 MB) and "
            f"may not have been scanned completely: {names}{more}")


# ---- database version (clamscan --version) ----

@dataclass
class DbInfo:
    line: str  # first line, cleaned
    engine: str | None = None
    db_version: str | None = None
    db_date: datetime | None = None
    problem: str | None = None

    def age_days(self, now: datetime | None = None) -> float | None:
        if self.db_date is None:
            return None
        return ((now or datetime.now()) - self.db_date).total_seconds() / 86400

    def date_text(self) -> str:
        return self.db_date.strftime("%Y-%m-%d") if self.db_date else "unknown"


def parse_version(text: str) -> DbInfo:
    """Read `ClamAV ENGINE/DBVERSION/CTIME-DATE`. Anything else is kept as the raw line."""
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    info = DbInfo(line=clean(first, 200))
    if not first.startswith("ClamAV "):
        info.problem = "unrecognised version output"
        return info
    parts = first[len("ClamAV "):].split("/")
    info.engine = clean(parts[0].strip(), 40)
    if len(parts) < 3:
        info.problem = "no signature database reported (clamscan found none)"
        return info
    info.db_version = clean(parts[1].strip(), 40)
    try:
        info.db_date = datetime.strptime(" ".join("/".join(parts[2:]).split()),
                                         "%a %b %d %H:%M:%S %Y")
    except ValueError:
        info.problem = "database date not recognised"
    return info


def database_info(clamscan: str, runner=subprocess.run) -> DbInfo:
    """Run `clamscan --version` (a bounded probe, as `doctor` does for wine). Never raises."""
    try:
        proc = runner([clamscan, "--version"], capture_output=True, text=True,
                      timeout=VERSION_TIMEOUT_S, check=False, stdin=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        return DbInfo("", problem=f"version check timed out after {VERSION_TIMEOUT_S}s")
    except OSError as exc:
        return DbInfo("", problem=f"could not run: {exc.strerror or exc}")
    if proc.returncode != 0:
        return DbInfo("", problem=f"version check exited with status {proc.returncode}")
    return parse_version(proc.stdout or "")


def describe_database(info: DbInfo, now: datetime | None = None) -> list[str]:
    lines = [f"ClamAV version line: {info.line or '(none)'}"]
    if info.db_date is not None:
        age = info.age_days(now)
        lines.append(f"signature database {info.db_version}, dated {info.date_text()}")
        if age is not None and age > DB_WARN_DAYS:
            lines.append(f"WARNING: the signature database is {int(age)} days old (more than "
                         f"{DB_WARN_DAYS}). Update it yourself with freshclam; gitfurl never "
                         "updates it.")
    if info.problem:
        lines.append(f"note: {info.problem}")
    return lines


def describe_scan_plan(plan: ScanPlan, db: DbInfo | None = None,
                       now: datetime | None = None) -> list[str]:
    """Plain text for the dry run, the plan preview and the approval text."""
    n = len(plan.files)
    lines = [
        ("ClamAV scan first (read-only; a hit or a scan error stops the run before anything "
         "is run or created):"),
        f"  scan command: {_shown_argv(plan)}",
        (f"  scans the installer and {n - 1} other regular file(s) directly in its folder; "
         f"subfolders and symbolic links are not scanned ({plan.skipped} skipped). Timeout "
         f"{plan.timeout_s:g} s."),
    ]
    if plan.large:
        lines.append("  " + large_note(plan.large) + ".")
    if db is not None:
        lines.extend("  " + ln for ln in describe_database(db, now))
    lines.append("  gitfurl never runs freshclam or updates the database; keep it current "
                 "yourself.")
    return lines


def _shown_argv(plan: ScanPlan) -> str:
    import shlex

    return clean(shlex.join(plan.argv), 4000)


# ---- running and reading the result ----

@dataclass
class ScanResult:
    status: str  # CLEAN, HIT or ERROR
    reason: str = ""
    hits: list[tuple[str, str]] = field(default_factory=list)  # (path, signature), raw
    exit_code: int | None = None
    db: DbInfo | None = None
    notes: list[str] = field(default_factory=list)
    large: tuple[str, ...] = ()  # files possibly not scanned completely (size above the limit)


def parse_output(text: str, files: tuple[str, ...]) -> tuple[list[tuple[str, str]], list[str],
                                                             set[str], list[str]]:
    """Read clamscan's result lines for the files that were passed.

    Returns (hits, errors, files with a result, unreadable lines). Only lines that start with
    one of the passed paths followed by ": " are understood; anything else is unreadable.
    """
    hits, errors, covered, bad = [], [], set(), []
    by_len = sorted(files, key=len, reverse=True)
    for line in text.splitlines():
        if not line.strip():
            continue
        path = next((f for f in by_len if line.startswith(f + ": ")), None)
        if path is None:
            bad.append(line)
            continue
        rest = line[len(path) + 2:]
        if rest in ("OK", "Empty file"):
            covered.add(path)
        elif rest.endswith(" FOUND") and rest[:-6].strip():
            hits.append((path, rest[:-6].strip()))
            covered.add(path)
        elif rest.endswith(" ERROR"):
            errors.append(f"{path}: {rest}")
            covered.add(path)
        else:
            bad.append(line)
    return hits, errors, covered, bad


def interpret(code: int | None, stdout: str, files: tuple[str, ...], truncated: bool = False
              ) -> ScanResult:
    """Decide clean, hit or error from the exit code and the output. Conservative."""
    hits, errors, covered, bad = parse_output(stdout, files)
    if truncated:
        return ScanResult(ERROR, "clamscan's output was larger than gitfurl reads; the result "
                          "could not be checked", hits, code)
    if code == 1:
        if not hits:
            return ScanResult(ERROR, "clamscan reported a match (exit status 1) but gitfurl could "
                              "not read which file matched", hits, code)
        res = ScanResult(HIT, "a known signature matched", hits, code)
        if errors or bad:
            res.notes.append("some files could not be scanned or their result was not readable")
        return res
    if code == 0:
        if hits:
            return ScanResult(ERROR, "clamscan exited 0 but printed a match; the output is "
                              "inconsistent", hits, code)
        if bad:
            return ScanResult(ERROR, "clamscan printed lines gitfurl could not read: "
                              + clean(bad[0], 200), hits, code)
        if errors:
            return ScanResult(ERROR, "clamscan reported an error: " + clean(errors[0], 200),
                              hits, code)
        missing = [f for f in files if f not in covered]
        if missing:
            return ScanResult(ERROR, "clamscan reported no result for "
                              + clean(missing[0], 200), hits, code)
        return ScanResult(CLEAN, "no known signature matched", [], code)
    if code == 2:
        detail = f": {clean(errors[0], 200)}" if errors else ""
        return ScanResult(ERROR, f"clamscan reported an error (exit status 2){detail}", hits,
                          code)
    return ScanResult(ERROR, f"clamscan exited with unexpected status {code}", hits, code)


def _reader(stream, box: list, cap: int):
    def work():
        kept, size, over = [], 0, False
        while True:
            try:
                chunk = stream.read(65536)
            except (OSError, ValueError):  # closed after a reader that never ended
                over = True
                break
            if not chunk:
                break
            if size < cap:
                kept.append(chunk[:cap - size])
                over = over or len(chunk) > cap - size
                size += min(len(chunk), cap - size)
            else:
                over = True
        box.append((b"".join(kept), over))

    t = threading.Thread(target=work, daemon=True)
    t.start()
    return t


def _stop_group(pgid: int) -> list[str]:
    """TERM, then KILL, to this one process group (the scan's own). Never by name."""
    notes = []
    for sig, wait in ((signal.SIGTERM, STOP_GRACE_S), (signal.SIGKILL, 3.0)):
        if not group_alive(pgid):
            break
        try:
            os.killpg(pgid, sig)
        except (ProcessLookupError, PermissionError):
            pass
        end = time.monotonic() + wait
        while time.monotonic() < end and group_alive(pgid):
            time.sleep(0.05)
        if sig == signal.SIGKILL:
            notes.append("the scan did not stop on SIGTERM and was force-killed")
    if group_alive(pgid):
        notes.append("could not confirm that the scan's processes ended")
    return notes


def _decode(data: bytes) -> str:
    return data.decode("utf-8", "surrogateescape")


def run_scan(plan: ScanPlan, should_stop=None, env: dict[str, str] | None = None) -> ScanResult:
    """Run the approved scan in its own process group, with a timeout. Never raises for a
    scan problem (that is an ERROR result); re-raises KeyboardInterrupt after stopping it.

    Only the scan's own group is ever signalled. `should_stop` (a callable) lets a Stop button
    end the scan early; the result is then an ERROR.
    """
    try:
        db = database_info(plan.clamscan)  # subprocess.run kills its child on Ctrl-C
    except KeyboardInterrupt as exc:
        exc.scan_interrupted = True
        exc.notes = ["the ClamAV version check was interrupted; the installer was not started"]
        raise
    guard = SigintGuard()
    guard.__enter__()
    try:
        try:
            proc = subprocess.Popen(plan.argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, start_new_session=True, cwd="/",
                                    env=dict(os.environ if env is None else env))
        except OSError as exc:
            return ScanResult(ERROR, f"could not start clamscan: {exc.strerror or exc}", db=db)
        out_box, err_box = [], []
        readers = [_reader(proc.stdout, out_box, OUTPUT_CAP),
                   _reader(proc.stderr, err_box, OUTPUT_CAP)]
        deadline = time.monotonic() + plan.timeout_s
        ended, notes = None, []
        try:
            guard.release()
            if guard.hits:
                raise KeyboardInterrupt
            while ended is None:
                try:  # wait without reaping, so the group id stays ours until we are done
                    info = os.waitid(os.P_PID, proc.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
                except ChildProcessError:
                    info = True
                if info:
                    ended = "exited"
                elif should_stop is not None and should_stop():
                    ended = "stopped"
                elif time.monotonic() >= deadline:
                    ended = "timeout"
                else:
                    time.sleep(0.05)
        except BaseException as exc:
            guard.hits = 0
            guard.__enter__()
            notes = _stop_group(proc.pid)
            proc.wait()
            exc.scan_interrupted = True
            exc.notes = ["the ClamAV scan was stopped; the installer was not started", *notes]
            raise
        if ended == "exited":
            if group_alive(proc.pid):  # something the scan left behind in its own group
                notes = _stop_group(proc.pid)
        else:
            notes = _stop_group(proc.pid)
        code = proc.wait()
        for t in readers:
            t.join(5)
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
        stdout, over = out_box[0] if out_box else (b"", False)
        stderr, _ = err_box[0] if err_box else (b"", False)
        if ended == "timeout":
            res = ScanResult(ERROR, f"the scan did not finish within {plan.timeout_s:g} s and "
                             "was stopped", exit_code=None)
        elif ended == "stopped":
            res = ScanResult(ERROR, "the scan was stopped by you", exit_code=None)
        else:
            res = interpret(code, _decode(stdout), plan.files, over)
            if res.status == ERROR and stderr.strip():
                res.notes.append("clamscan said: " + clean(_decode(stderr).strip()
                                                           .splitlines()[-1], 200))
        res.db = db
        res.large = plan.large
        res.notes.extend(notes)
        return res
    finally:
        guard.release()


def decide(result: ScanResult, allow_hit: bool = False) -> tuple[bool, str]:
    """(run may proceed, message). A hit blocks unless allowed; an error always blocks."""
    db = result.db
    dated = f"database dated {db.date_text()}" if db and db.db_date else "database date unknown"
    notes = "".join(f"\n  note: {n}" for n in result.notes)
    if result.status == CLEAN:
        if result.large:
            return True, (f"ClamAV scan: no known signature matched in the parts ClamAV scanned "
                          f"(ClamAV, {dated}). {large_note(result.large)}.{notes}")
        return True, f"ClamAV scan: no known signature matched (ClamAV, {dated}).{notes}"
    if result.status == HIT:
        listed = "\n".join(f"  {clean(p, 200)}: {clean(s, 120)}" for p, s in result.hits[:20])
        more = f"\n  ... and {len(result.hits) - 20} more" if len(result.hits) > 20 else ""
        head = (f"ClamAV scan: a known signature matched ({dated}):\n{listed}{more}{notes}\n"
                f"{FALSE_POSITIVE_NOTE}")
        if allow_hit:
            return True, head + "\nYou chose to run it anyway."
        return False, (head + "\nThe run was stopped: nothing was run or created. To run it "
                       "anyway, add --allow-scan-hit on the command line, or confirm the second "
                       "dialog in the app.")
    return False, (f"ClamAV scan failed: {result.reason}.{notes}\nThe run was stopped: nothing "
                   "was run or created. gitfurl does not run an installer unscanned when the "
                   "scan was asked for; fix the problem or turn the scan off.")
