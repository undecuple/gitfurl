"""Which route an install takes, and the native route itself.

choose_route() is pure: it reads the installer's headers (already in a Detection) and the
FreeArc archives' own indexes beside it (bounded reads, through gitfurl.extract.plan_extract)
and writes nothing. The route is:

  native  when the installer is Inno Setup (or is itself a FreeArc archive), every FreeArc
          archive beside it reads cleanly, uses only storing, lzma, exe, x86, delta, rep,
          4x4 (lzma inside), srep and rolz, and holds only names gitfurl can write safely, and (for the
          automatic choice) the archives are linked to the installer (its setup script names
          them all, or they share its name) and no option that only a Setup run uses was
          given. Setup is then NOT run at all: gitfurl
          unpacks the archives itself into C:\\Games\\<prefix>, after creating the prefix with
          Wine when asked. Setup's own files, shortcuts, registry entries, redistributables and
          script steps are skipped, and the plan says so word for word.
  wine    otherwise (Setup runs under Wine/Proton, as before), with the reason.
  unsupported  when the installer could not be read as a regular file.

Why Setup is not run "for its Windows parts only" on the native route: the payload unpack is
the setup script's own code, which no switch turns off; hiding the archives leaves Setup
waiting on its own error window, and stopping it partway leaves an unknown state. So either
Setup does everything (wine) or gitfurl does the unpack and nothing of Setup runs (native).

run_native() performs an approved native plan's unpack: a local log (mode 0600), an optional
JSON-lines event log, progress lines printed by the caller's callback on the calling thread,
and the listings afterwards (nothing found is run). When the plan holds the setup's registry
entries and shortcuts (opt-in, gitfurl.setup_entries), they are written after a complete
unpack, without Wine. It starts no process.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gitfurl import farc
from gitfurl.prefixes import QUIET_WINEBOOT_ENV
from gitfurl.detect import (
    CONF_LIKELY,
    ROUTE_NATIVE,
    ROUTE_UNSUPPORTED,
    ROUTE_WINE,
    Detection,
    clean,
)

ROUTE_CHOICES = ("auto", "native", "wine")

METHODS_TEXT = "storing, lzma, exe, x86, delta, rep, 4x4 (with lzma inside), srep or rolz"

SKIPPED_TEXT = (
    "Setup is not run. Skipped: Setup's own files (for example its uninstaller and small extra "
    "files it holds itself), shortcuts, registry entries, redistributables it would run, and "
    "every step of its script. The archives are unpacked with their own folder layout into "
    "{target}; the script may have placed files elsewhere, picked only some archives, or done "
    "more after unpacking. If the program needs any of that, run again with --route wine into "
    "a new prefix.")

SETUP_UNPACKS_ITSELF = ("Setup unpacks its own archives with its own code; gitfurl cannot help "
                        "with that part")

NATIVE_COVERS = ("the native route covers Inno Setup installers whose payload is in FreeArc "
                 "archives (and FreeArc archives themselves)")

# Folder and file names that mark a redistributable installer (case ignored). Listed only.
REDIST_DIRS = frozenset(["_redist", "redist", "_commonredist", "directx"])
REDIST_PREFIXES = ("vcredist", "vc_redist", "dxsetup", "dxwebsetup", "dotnet", "oalinst")
MAX_LISTED = 20

# Only for a prefix gitfurl creates on the native route: Wine's Mono and Gecko install dialogs
# are turned off for wineboot, so creating the prefix needs no answer. The unpack runs no
# Windows program, so it needs neither.
NATIVE_WINEBOOT_ENV = QUIET_WINEBOOT_ENV
# Free space kept for a prefix that is still to be created (new prefixes took about 300 to
# 570 MB in our runs).
PREFIX_ALLOWANCE = 768 * 1024 * 1024

# A set number at the end of an archive's name: "-01", "_2", ".003", " 4", "part1".
_SET_NUMBER = re.compile(r"[-_. ]?(?:part)?\d{1,3}$")


@dataclass(frozen=True)
class RouteChoice:
    route: str  # native | wine | unsupported
    chosen_by: str  # "gitfurl" (auto) or "you" (--route)
    reasons: tuple[str, ...]
    extract: object = None  # gitfurl.extract.ExtractPlan, only for native
    links: object = None  # gitfurl.namelinks.LinkProposal used for the link check, if any

    def line(self) -> str:
        if self.route == ROUTE_UNSUPPORTED:
            return f"route: {self.route}. " + "; ".join(self.reasons) + "."
        return (f"route: {self.route} (chosen by {self.chosen_by}). "
                + "; ".join(self.reasons) + ".")


def _inno(det: Detection) -> bool:
    return any(ev.format == "Inno Setup" and ev.confidence == CONF_LIKELY for ev in det.evidence)


def _starts_with_freearc(installer: Path) -> bool:
    return farc.has_signature(Path(installer))


def _base_name(name: str) -> str:
    """An archive's or installer's name without its extension and set number, case folded."""
    stem = name.casefold()
    if "." in stem:
        stem = stem.rsplit(".", 1)[0]
    return _SET_NUMBER.sub("", stem).strip(" -_.")


def script_names(installer: Path) -> set[str] | None:
    """Casefolded names of the files beside an Inno Setup installer that its setup script
    names ([Files] external entries and [Code] string constants, see
    gitfurl.inno.named_beside), or None when the setup data cannot be read. Never raises:
    a hostile file must not stop the plan. Reads the installer's headers and the folder's
    names only."""
    from gitfurl import inno

    try:
        return inno.named_beside(inno.read_setup(installer), Path(installer).parent)
    except Exception:  # noqa: BLE001
        return None


def _shown(paths: list[Path]) -> str:
    return ", ".join(clean(p.name, 60) for p in paths[:4]) + (", ..." if len(paths) > 4 else "")


def link_check(installer: Path, paths: list[Path], named: set[str] | None = None,
               has_script: bool = True) -> tuple[str | None, str | None]:
    """(why the archives are not linked to the installer, how they are linked); one is None.

    Linked when the setup script names every archive (`named`, from script_names()), or by
    name: every archive has the same base name (the name without its extension and set
    number such as "-01"), and that base name is the installer's own, or is part of the name
    of the installer's folder. An installer that is itself a FreeArc archive is linked to
    itself. `has_script` is False for an installer that is not Inno Setup (it has no setup
    script to read). Reads names only.
    """
    installer = Path(installer)
    others = [p for p in paths if p.name != installer.name]
    if not others:
        return None, "the installer is the archive"
    unnamed = others if named is None else [p for p in others if p.name.casefold() not in named]
    if not unnamed:
        return None, "the setup script names every archive"
    if not has_script:
        script = "the installer has no setup script gitfurl can read for the names it uses"
    elif named is None:
        script = "the setup script could not be read for the names it uses"
    else:
        script = (f"the setup script names {len(others) - len(unnamed)} of the "
                  f"{len(others)} (not named: {_shown(unnamed)}; names it builds while it "
                  "runs are not seen)")
    own = _base_name(installer.name)
    bases = {_base_name(p.name) for p in others}
    if len(bases) != 1 or "" in bases:
        return (f"the FreeArc archives beside the installer ({_shown(paths)}) do not share one "
                f"base name, and {script}, so gitfurl cannot tell which of them Setup uses",
                None)
    (base,) = bases
    if base == own or (len(base) >= 3 and base in installer.parent.name.casefold()):
        return None, "the archives share the installer's name or its folder's"
    return (f"the FreeArc archives beside the installer ({_shown(paths)}) are not linked to it "
            f"by name (their base name {clean(base, 60)!r} is neither the installer's name nor "
            f"part of its folder's name), and {script}", None)


def archives_not_linked(installer: Path, paths: list[Path],
                        named: set[str] | None = None) -> str | None:
    """Why the archives beside the installer are not linked to it, or None (see link_check)."""
    return link_check(installer, paths, named)[0]


def _with_links(named: set[str] | None, links) -> set[str] | None:
    """The script's names plus the archives that stand for a name it expects (links, a
    gitfurl.namelinks.LinkProposal, when --link-names is on and it has a mapping)."""
    if named is None or links is None or not links.ok:
        return named
    return named | links.mapped_archives()


def _linked_text(linked: str, links) -> str:
    if links is None or not links.ok or linked != "the setup script names every archive":
        return linked
    return f"{linked}, with the names linked ({links.stands_for()})"


def choose_route(installer: Path, prefix_name: str, det: Detection, requested: str = "auto",
                 env: dict[str, str] | None = None,
                 setup_options: tuple[str, ...] | list[str] = (), links=None, *,
                 accept_bad_index_crc: bool = False) -> RouteChoice:
    """Pick the route. Writes nothing and runs nothing.

    `setup_options` are the options the user gave that only a Setup run uses (for example
    --unattended); with requested="auto" they choose the Wine/Proton route.

    `links` is a gitfurl.namelinks.LinkProposal (--link-names on). With a mapping, the
    archives it maps count as named by the setup script for the link check, and the reasons
    say which archive stands for which name.

    Raises ValueError when `requested` is "native" and the native route is not possible (the
    message says why), or when the prefix name is not valid.
    """
    from gitfurl.extract import ExtractRefused, find_archives, plan_extract
    from gitfurl.prefixes import validate_prefix_name

    if requested not in ROUTE_CHOICES:
        raise ValueError(f"unknown route: {requested!r} (use auto, native or wine)")
    validate_prefix_name(prefix_name)
    if not det.readable:
        if requested == "native":
            raise ValueError("native route not possible: the file could not be read as a "
                             "regular file")
        return RouteChoice(ROUTE_UNSUPPORTED, "gitfurl",
                           ("the file could not be read as a regular file",))
    if requested == "wine":
        return RouteChoice(ROUTE_WINE, "you", ("you chose the Wine/Proton route",
                                               "Setup runs and does all of its own steps"))
    if requested == "auto" and setup_options:
        return RouteChoice(ROUTE_WINE, "gitfurl", (
            "Setup runs under Wine/Proton",
            "you asked for options that only a Setup run uses (" + ", ".join(setup_options)
            + "); to have gitfurl unpack the archives itself instead, use --route native "
            "(these options then do not apply)"))
    by = "you" if requested == "native" else "gitfurl"

    def wine(*reasons: str) -> RouteChoice:
        if requested == "native":
            raise ValueError("native route not possible: " + "; ".join(reasons))
        return RouteChoice(ROUTE_WINE, by, ("Setup runs under Wine/Proton", *reasons))

    is_arc = _starts_with_freearc(installer)
    inno = _inno(det)
    if not (inno or is_arc):
        return wine(f"{NATIVE_COVERS}; this installer is not recognised as either")
    try:
        paths, _ = find_archives(installer)
    except ExtractRefused as exc:
        return wine(str(exc))
    if not paths:
        return wine("no FreeArc archives beside the installer; Setup holds or fetches its "
                    "files itself, and the native route does not unpack Inno Setup's own "
                    "file data")
    try:
        plan = plan_extract(installer, prefix_name, env,
                            accept_bad_index_crc=accept_bad_index_crc)
    except ExtractRefused as exc:
        reasons = [f"the archives cannot be unpacked by gitfurl: {exc}"]
        if inno:
            reasons.append(SETUP_UNPACKS_ITSELF)
        return wine(*reasons)
    unlinked, linked = link_check(installer, paths,
                                  _with_links(script_names(installer), links) if inno else None,
                                  has_script=inno)
    if unlinked and requested == "auto":
        return wine(unlinked, "to unpack them anyway, use --route native")
    if linked:
        linked = _linked_text(linked, links)
    what = "a FreeArc archive" if is_arc and not inno else "an Inno Setup installer"
    n = len(plan.archives)
    reason = (f"{what}; its payload is {n} FreeArc archive{'s' if n != 1 else ''} whose every "
              f"data block uses {METHODS_TEXT}; gitfurl can unpack them itself")
    reasons = [reason, linked or "you chose this route although " + unlinked, "Setup is not run"]
    return RouteChoice(ROUTE_NATIVE, by, tuple(reasons), plan,
                       links if links is not None and links.ok else None)


def setup_unpacked(installer: Path, det: Detection, links=None):
    """For a Setup run's preflight: extract.unpacked_size() of the archives beside the
    installer, but only when they belong to it as the route judges that (an Inno Setup
    installer or a FreeArc one, and archives linked to it by its script or by name), so
    unrelated archives in the same folder (a downloads folder) are not counted. None
    otherwise. Read-only; never raises for a folder it cannot read."""
    from gitfurl.extract import ExtractRefused, find_archives, unpacked_size

    installer = Path(installer)
    inno = _inno(det)
    if not (inno or _starts_with_freearc(installer)):
        return None
    try:
        paths, _notes = find_archives(installer)
    except (ExtractRefused, OSError):
        return None
    if not paths:
        return None
    unlinked, _linked = link_check(installer, paths,
                                   _with_links(script_names(installer), links) if inno else None,
                                   has_script=inno)
    return None if unlinked else unpacked_size(installer)


# --- the native plan ------------------------------------------------------------------------

@dataclass
class NativeInstallPlan:
    installer: Path
    prefix_name: str
    prefix: Path
    choice: RouteChoice
    create_prefix: bool  # the prefix is missing and creating it was asked for
    wine: str | None = None  # needed only to create the prefix
    scan_lines: list[str] = field(default_factory=list)  # describe_scan_plan, if --scan
    cleanup_text: str | None = None  # CleanupPlan.describe(), if a cleanup mode was chosen
    unused: list[str] = field(default_factory=list)  # options that do not apply here
    memory_check: bool = True  # compare the decoders' memory with MemAvailable before the start
    # setup_entries.SetupEntriesPlan when the setup's registry entries and shortcuts were asked
    # for (--apply-setup-entries); None otherwise
    setup_entries: object = None

    @property
    def extract(self):
        return self.choice.extract

    def skipped(self) -> str:
        text = SKIPPED_TEXT.format(target=self.extract.target)
        if self.setup_entries is not None:
            text = text.replace("shortcuts, registry entries,",
                                "shortcuts and registry entries other than those the plan "
                                "lists,")
        return text

    def describe(self) -> list[str]:
        lines = [self.choice.line(), self.skipped()]
        n = 0

        def step(text: str) -> None:
            nonlocal n
            n += 1
            lines.append(f"step {n}: {text}")

        if self.scan_lines:
            step("ClamAV scan of the installer and the files beside it (a match or a scan "
                 "error stops the run before anything is created)")
            lines.extend(f"  {ln}" for ln in self.scan_lines)
        if self.create_prefix:
            argv = [self.wine or "wine", "wineboot", "--init"]
            env = " ".join(f"{k}={v}" for k, v in NATIVE_WINEBOOT_ENV.items())
            step(f"create the prefix {self.prefix} with Wine: {env} {shlex.join(argv)} (with "
                 "WINEPREFIX and WINEDLLOVERRIDES set for that command only; the override "
                 "turns off Wine's Mono and Gecko install dialogs, so this step needs no "
                 "answer, and the prefix has no Mono or Gecko, which the unpack does not "
                 "need); if it does not finish, the prefix is marked incomplete and gitfurl "
                 "will not unpack into it; this is the only Wine step")
        links = self.choice.links
        if links is not None and links.ok:
            lines.append(f"names: {links.stands_for()}; the native route reads these archives "
                         "directly (no links are made)")
            if links.unmatched:
                lines.append("still missing, no counterpart in the folder (not needed for the "
                             "unpack): " + ", ".join(clean(n, 60) for n in links.unmatched[:6]))
        step("unpack the archives with gitfurl's own reader (no Windows program runs):")
        lines.extend(f"  {ln}" for ln in self.extract.describe())
        step("list program candidates and redistributable installers found in the new folder "
             "(nothing is run)")
        if self.setup_entries is not None:
            step("only after a complete unpack: add the registry entries and menu entries the "
                 "setup declares that gitfurl can apply exactly (no Wine program runs; its "
                 "redistributables and [Run] entries are not run):")
            lines.extend(f"  {ln}" for ln in self.setup_entries.describe())
        if self.cleanup_text is not None:
            step("only after a complete unpack (every file written, every CRC-32 matching): "
                 + self.cleanup_text.replace("\n", "\n  "))
        lines.append("memory: the unpack runs inside gitfurl and is not put in a systemd "
                     "scope; the decoders' memory is capped per data block"
                     + ("; before the start that cap is compared with MemAvailable minus the "
                        "memory kept free for the rest of the system, and the run stops if it "
                        "does not fit (--no-memory-limit skips this check)"
                        if self.memory_check else
                        "; --no-memory-limit: it is not compared with free memory"))
        if self.unused:
            lines.append("options that do not apply to the native route (use --route wine for "
                         "them): " + ", ".join(self.unused))
        lines.append("One approval covers every step above.")
        return lines


def unpack_memory_problem(need: int, meminfo: str = "/proc/meminfo") -> str | None:
    """Why the unpack's decoders may not fit in free memory now, or None. Reads meminfo only.

    `need` is ExtractPlan.memory (the most one data block's decoders may hold at once). The
    room is MemAvailable minus the memory the memory limit keeps free for the rest of the
    system (the larger of 1 GiB and 10% of MemTotal).
    """
    from gitfurl.memlimit import RESERVE_FRACTION, RESERVE_MIN, gib, read_meminfo

    if need <= 0:
        return None
    mem = read_meminfo(meminfo)
    if mem is None:
        return (f"could not read MemTotal and MemAvailable from {meminfo}, so the unpack's "
                f"memory (up to {farc._size(need)}) cannot be compared with free memory")
    total, available = mem
    reserve = max(RESERVE_MIN, int(total * RESERVE_FRACTION))
    room = available - reserve
    if need > room:
        return (f"the unpack may need up to {farc._size(need)} to decode one data block, but "
                f"only {gib(max(0, room))} is free after keeping {gib(reserve)} for the rest "
                f"of the system (MemAvailable {gib(available)})")
    return None


# --- listings after the unpack ---------------------------------------------------------------

def written_files(plan) -> list[tuple[list[str], str]]:
    """(folder parts, file name) of every planned file, in plan order."""
    return [(plan.parts(folder), name) for ap in plan.archives for folder, name, _ in ap.files]


def is_redist(parts: list[str], name: str) -> bool:
    low = name.casefold()
    if not low.endswith((".exe", ".msi")):
        return False
    return any(p.casefold() in REDIST_DIRS for p in parts) or low.startswith(REDIST_PREFIXES)


def listings(plan, prefix_name: str) -> list[str]:
    """Program candidates and redistributable installers in the new folder. Reads file
    metadata only; nothing is run."""
    from gitfurl.changes import find_game_executables

    files = written_files(plan)
    paths = [str(plan.dest.joinpath(*parts, name)) for parts, name in files]
    out: list[str] = []
    found = find_game_executables(plan.prefix, paths)[:MAX_LISTED]
    if found:
        out.append("program candidates (a guess from names and sizes; nothing was run):")
        for path, size in found:
            rel = Path(path).relative_to(plan.dest)
            win = plan.target + "\\" + "\\".join(rel.parts)
            out.append(f"  {clean(str(rel), 160)} ({farc._size(size)})")
            out.append(f"    start it with: WINEPREFIX={shlex.quote(str(plan.prefix))} wine "
                       f"{shlex.quote(clean(win, 300))}")
    else:
        out.append("program candidates: none found (no .exe that looks like a program)")
    redist = [(parts, name) for parts, name in files if is_redist(parts, name)]
    if redist:
        out.append("redistributable installers found (not run):")
        for parts, name in redist[:MAX_LISTED]:
            out.append(f"  {clean('/'.join([*parts, name]), 160)}")
        if len(redist) > MAX_LISTED:
            out.append(f"  ... and {len(redist) - MAX_LISTED} more")
        out.append("  If the program needs one, run it with: gitfurl cli install <file> "
                   f"--prefix {prefix_name}")
    return out


# --- the run ---------------------------------------------------------------------------------

@dataclass
class NativeResult:
    status: int  # 0 complete, 130 stopped, 1 error or CRC-32 mismatch
    result: object  # gitfurl.extract.ExtractResult
    log_path: Path
    events_path: Path | None
    lines: list[str]


def _rate_text(done: int, total: int, elapsed: float) -> str:
    pct = 100.0 * done / total if total else 100.0
    rate = done / elapsed if elapsed > 0 else 0.0
    eta = ""
    if rate > 0 and total > done:
        left = int((total - done) / rate)
        m, s = divmod(left, 60)
        h, m = divmod(m, 60)
        eta = f", ETA {h}h{m:02d}m" if h else f", ETA {m}m{s:02d}s"
    return (f"native unpack: {farc._size(done)} of {farc._size(total)} ({pct:.1f}%), "
            f"{farc._size(int(rate))}/s{eta}")


def run_native(plan: NativeInstallPlan, cancel=None, on_line=None, detailed_log: bool = False,
               progress_interval: float = 10.0, env: dict[str, str] | None = None,
               scan_summary: str | None = None, clock=time.monotonic,
               on_progress=None, timeline=None) -> NativeResult:
    """Unpack an approved native plan. The prefix must exist. Starts no process.

    `on_line(text)` receives throttled progress lines and `on_progress(done, total, elapsed)`
    the same samples as numbers (both on this thread; there is no progress thread).
    `timeline` (a gitfurl.timeline.Timeline) gets a row for each step and is saved next to
    the log. Raises
    ExtractRefused before anything is written (nothing was created then
    except, rarely, empty folders the refusal lists); afterwards every problem is in the
    result. A Ctrl-C while files are written ends the unpack (status 130); files already
    written are kept. A Ctrl-C after the unpack has returned (while the listings or the report
    are made) also gives status 130, with the unpack's result and an "interrupted after the
    unpack" line in the log. A KeyboardInterrupt that leaves this function carries
    `unpack_started`: True when the destination folder existed by then (files may have been
    written), False when nothing was written.
    """
    from gitfurl.extract import extract
    from gitfurl.runner import logs_root

    xplan = plan.extract
    log_dir = logs_root(env)
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{plan.prefix_name}-{stamp}.log"
    events_path = log_dir / f"{plan.prefix_name}-{stamp}.events.jsonl" if detailed_log else None
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    events = None
    if events_path is not None:
        events = os.fdopen(os.open(events_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600),
                           "w")

    def event(kind, **data):
        if events:
            events.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"),
                                     "event": kind, **data}) + "\n")
            events.flush()

    t0 = clock()
    last = [t0]

    def progress(done: int, total: int) -> None:
        now = clock()
        if now - last[0] < progress_interval:
            return
        last[0] = now
        pct = round(100.0 * done / total, 2) if total else 100.0
        event("progress", bytes_written=done, total_bytes=total, percent=pct)
        if on_line is not None:
            on_line(_rate_text(done, total, now - t0))
        if on_progress is not None:
            on_progress(done, total, now - t0)
        _tl_unpack_progress(timeline, done, total, now - t0)

    result = None
    try:
        with os.fdopen(fd, "w") as log:
            log.write("route: native (Setup is not run)\n")
            log.write(f"installer: {clean(str(plan.installer), 300)}\n")
            for ap in xplan.archives:
                log.write(f"archive: {clean(str(ap.path), 300)}\n")
            log.write(f"destination: {xplan.target} ({xplan.dest})\n")
            if scan_summary:
                log.write("".join(f"scan: {ln}\n" for ln in scan_summary.splitlines()))
            for warning in xplan.warnings:
                log.write(f"warning: {warning}\n")
            log.write("---\n")
            log.flush()
            event("start", route=ROUTE_NATIVE, prefix=str(plan.prefix),
                  destination=str(xplan.dest), archives=[ap.path.name for ap in xplan.archives],
                  total_bytes=xplan.total_size, files=xplan.file_count)
            on_event = _tl_unpack_start(timeline, xplan, log_dir / f"{plan.prefix_name}-{stamp}",
                                        log_path)
            try:
                result = extract(xplan, cancel, progress,
                                 **({"on_event": on_event} if on_event else {}))
            except BaseException as exc:
                started = isinstance(exc, KeyboardInterrupt) and os.path.lexists(xplan.dest)
                if started:
                    log.write("--- interrupted during the unpack, after the destination was "
                              "created; files already written are kept and nothing was "
                              "deleted; the unpack's result was not recorded\n")
                    event("interrupted", stage="unpack")
                else:
                    log.write(f"--- not started: {type(exc).__name__}: "
                              f"{clean(str(exc), 300)}\n")
                    event("not_started", reason=clean(str(exc), 300))
                if isinstance(exc, KeyboardInterrupt):
                    exc.unpack_started = started
                raise

            def data():
                return {"files": result.files, "bytes_written": result.bytes_written,
                        "crc_errors": len(result.crc_errors),
                        "seconds": round(clock() - t0, 1)}

            try:
                status = 130 if result.stopped else 0 if result.ok else 1
                _tl_unpack_end(timeline, xplan, result)
                lines = list(result.lines())
                if result.ok:
                    lines.extend(listings(xplan, plan.prefix_name))
                else:
                    lines.append("program listing: skipped, the unpack did not complete")
                if plan.setup_entries is not None:
                    if result.ok and cancel is not None and cancel.is_set():
                        lines.append("setup entries: not added, Stop was asked for")
                        _tl_entries_not(timeline, "Stop was asked for")
                    elif result.ok:
                        from gitfurl.setup_entries import Applied, apply_entries

                        applied = Applied()
                        written = apply_entries(plan.setup_entries, log_dir, stamp,
                                                applied=applied)
                        event("setup_entries", lines=written)
                        lines.append("setup entries (registry values and menu entries):")
                        lines.extend(f"  {ln}" for ln in written)
                        _tl_entries(timeline, plan.setup_entries, applied)
                    else:
                        lines.append("setup entries: not added, the unpack did not complete")
                        _tl_entries_not(timeline, "the unpack did not complete")
                lines.append(plan.skipped())
                for ln in lines:
                    log.write(f"--- {ln}\n")
                if result.stopped:
                    event("cancelled", **data())
                else:
                    event("exit", status=status, ok=result.ok, error=result.error, **data())
            except KeyboardInterrupt:
                # The unpack itself has returned: report its result, never "nothing written".
                status = 130
                lines = [*result.lines(),
                         ("interrupted after the unpack: the program listing and the rest of "
                          "the report were not finished; nothing was deleted"),
                         plan.skipped()]
                log.write("--- interrupted after the unpack\n")
                for ln in lines:
                    log.write(f"--- {ln}\n")
                event("interrupted", stage="after_unpack", ok=result.ok, **data())
    finally:
        if events:
            events.close()
    return NativeResult(status, result, log_path, events_path, lines)


# ---- timeline rows for the unpack (gitfurl.timeline); nothing without a timeline -----------

def _tl_unpack_start(tl, xplan, stem: Path, log_path: Path):
    """Attach the timeline file beside the log, add the start rows, and return the
    on_event handler for extract() (None without a timeline)."""
    if tl is None:
        return None
    from gitfurl.timeline import SUFFIX, shown, size_text

    tl.log_path = log_path
    tl.attach(Path(f"{stem}{SUFFIX}"))
    n = len(xplan.archives)
    tl.add("files", f"Destination: {xplan.target}",
           f"a new folder in the prefix ({shown(str(xplan.dest), 200)}); it must not exist "
           "yet, and no file is overwritten")
    tl.add("start", "Started the unpack with gitfurl's own reader",
           f"{n} archive{'s' if n != 1 else ''}, {xplan.file_count} files, "
           f"{size_text(xplan.total_size)} to write; no Windows program runs")
    run = {"key": None}

    def on_event(kind, ap, *details):
        name = shown(ap.path.name, 80)
        blocks = len(ap.archive.blocks)
        if kind == "archive":
            tl.add("archive", f"Opened {name}: {len(ap.files)} files in {blocks} data "
                   f"block{'s' if blocks != 1 else ''}",
                   "its index was read again and matches the plan")
            run["key"] = None
            return
        k, blk, files = details
        kind_text = blk.method.kind  # gitfurl's own stage names, never the archive's text
        key = run["key"]
        if key is None or key[1] != ap.path.name or key[2] != kind_text:
            run["key"] = key = ("blocks", ap.path.name, kind_text, k)
            run["files"] = 0
        run["files"] += files
        first = key[3] + 1
        span = f"Data block {k + 1}" if first == k + 1 else f"Data blocks {first}-{k + 1}"
        many = "s" if run["files"] != 1 else ""
        tl.add("archive", f"{span} of {blocks} in {name}: {kind_text}",
               f"{run['files']} file{many}; decoded by gitfurl's own decoders", fold=key)

    return on_event


def _tl_unpack_progress(tl, done: int, total: int, elapsed: float) -> None:
    if tl is None:
        return
    from gitfurl.timeline import progress_text, size_text

    rate = done / elapsed if elapsed > 0 else 0
    tl.progress(progress_text("Unpacking", done, total),
                f"{size_text(int(rate))}/s" if rate else "")


def _tl_unpack_end(tl, xplan, result) -> None:
    if tl is None:
        return
    from gitfurl.timeline import size_text

    tl.add("files", f"{result.files} files written ({size_text(result.bytes_written)}), "
           "each one CRC-32 checked",
           "every CRC-32 matched the archive's index" if not result.crc_errors else
           "the files that did not match were removed again")
    if result.crc_errors:
        tl.add("skip", f"{len(result.crc_errors)} files removed again",
               "their CRC-32 did not match the archive's index")
    if result.not_removed:
        tl.add("skip", f"{len(result.not_removed)} damaged files could not be removed",
               "they failed the CRC-32 check; see the log")
    if result.incomplete:
        tl.add("skip", "One file was left incomplete (kept)",
               "the unpack ended while it was being written")
    if result.error:
        tl.add("note", "The unpack ended with an error", result.error)
    if result.stopped:
        tl.add("note", "The unpack stopped", "Stop was asked for; files already written are "
               "kept and nothing was deleted")
    if result.ok:
        redist = sum(1 for parts, name in written_files(xplan) if is_redist(parts, name))
        if redist:
            tl.add("skip", f"{redist} redistributable installer{'s' if redist != 1 else ''} "
                   "unpacked but not run", "gitfurl never runs what it unpacks; they are listed")


TL_SKIPPED_ROWS = 20  # skipped setup entries with a row each; the rest are counted in one


def _tl_entries(tl, entries, applied) -> None:
    """Rows for the setup's registry values and menu entries written after the unpack, and
    one row per entry left out with its reason (the log has every one)."""
    if tl is None:
        return
    from gitfurl.setup_entries import SERVER_WAIT_S
    from gitfurl.timeline import shown

    for name, n in applied.values.items():
        tl.add("files", f"Registry values written into {name}: {n}",
               "the setup's [Registry] entries gitfurl can apply exactly, written without Wine "
               "while no Wine server for the prefix ran; the file was copied into the log "
               "folder first")
    if entries.values and not applied.values:
        tl.add("note", "No registry value was written",
               f"each value the plan listed was kept or refused (a Wine server is waited for up "
               f"to {SERVER_WAIT_S:g} s); see below and the log")
    if applied.menu_entries:
        many = "s" if applied.menu_entries != 1 else ""
        tl.add("files", f"Menu entries written: {applied.menu_entries}",
               f"one .desktop file{many} per program under {{app}} in your data folder's "
               "applications folder, starting it in this prefix")
    items = [*entries.skipped, *applied.not_done]
    for item in items[:TL_SKIPPED_ROWS]:
        tl.add("skip", "Setup entry not applied", shown(item, 280))
    if len(items) > TL_SKIPPED_ROWS:
        tl.add("skip", f"{len(items) - TL_SKIPPED_ROWS} more setup entries not applied",
               "the log lists each one with its reason")


def _tl_entries_not(tl, why: str) -> None:
    if tl is not None:
        tl.add("skip", "The setup's registry entries and menu entries were not added", why)


def native_prefix_create(plan: NativeInstallPlan, memory_limit: bool,
                         env: dict[str, str] | None = None, run=None, should_stop=None) -> int:
    """Create the plan's prefix with wineboot, as the Wine route does (same memory scope when
    the memory limit is on), with Wine's Mono and Gecko dialogs turned off for that command
    only (NATIVE_WINEBOOT_ENV). Returns wine's exit status. Raises as create_prefix does, and
    MemoryLimitError when the limit was asked for and cannot be used. `should_stop` lets a
    Stop button end wineboot (prefixes.PrefixStopped)."""
    from gitfurl.prefixes import create_prefix

    scope = None
    if memory_limit:
        from gitfurl.memlimit import prepare

        scope = prepare(plan.prefix_name, os.environ if env is None else env, label="wineboot")
    try:
        return create_prefix(plan.prefix_name, plan.wine or "wine", env, run,
                             extra_env=dict(NATIVE_WINEBOOT_ENV),
                             wrap=scope.wrap if scope else None, should_stop=should_stop)
    finally:
        if scope is not None:
            scope.finish()


# --- what stops a native run before anything is done (the command line and the window) ----

@dataclass(frozen=True)
class NativeStop:
    kind: str  # no_prefix | no_wine | scope | space | memory
    detail: str = ""

    @property
    def memory_related(self) -> bool:
        """The command line exits with status 2 for these (status 1 for the others)."""
        return self.kind in ("scope", "memory")


def native_stops(xplan, *, missing: bool, create_ok: bool, wine: str | None,
                 scope_problem: str | None, memory_check: bool,
                 meminfo: str = "/proc/meminfo") -> list[NativeStop]:
    """Why a native run of `xplan` must not start, in the order they are reported. Reads
    `/proc/meminfo` and free space only; changes nothing.

    missing: the prefix folder does not exist; create_ok: creating it was asked for;
    wine: the Wine found on PATH (needed only to create the prefix); scope_problem: why the
    memory limit cannot be used for wineboot (None if it can, or is off); memory_check: compare
    the decoders' memory with free memory.
    """
    create = missing and create_ok
    stops = []
    if missing and not create_ok:
        stops.append(NativeStop("no_prefix", str(xplan.prefix)))
    if create and wine is None:
        stops.append(NativeStop("no_wine"))
    if scope_problem:
        stops.append(NativeStop("scope", scope_problem))
    # For a prefix still to be created, the space is measured on the nearest existing folder
    # above it, with room for the prefix itself, so nothing is created when it cannot fit.
    problem = (xplan.space_problem_before_prefix(PREFIX_ALLOWANCE) if create
               else None if missing else xplan.problem())
    if problem:
        stops.append(NativeStop("space", problem))
    if memory_check:
        mem = unpack_memory_problem(xplan.memory, meminfo)
        if mem:
            stops.append(NativeStop("memory", mem))
    return stops


# --- one approved native run on a worker thread (the window) ------------------------------

class NativeControl:
    """Stop for a native run started on another thread: the scan, wineboot and the unpack
    each check it. Only gitfurl's own processes (the scan's and wineboot's groups) are ever
    signalled."""

    def __init__(self):
        import threading

        self.event = threading.Event()
        self.notes: list[str] = []

    @property
    def cancelled(self) -> bool:
        return self.event.is_set()

    def request_stop(self) -> list[str]:
        self.event.set()
        return self.notes


@dataclass
class NativeOutcome:
    """How perform_native ended. `result` is the unpack's NativeResult when it ran; otherwise
    `message` says why nothing (or only the prefix) was done, and `status` is 1, 2 or 130."""

    status: int
    message: str | None = None
    result: NativeResult | None = None
    scan_summary: str | None = None
    prefix_created: bool = False


def perform_native(plan: NativeInstallPlan, control: NativeControl, *, memory_limit: bool,
                   scan_plan=None, scan_result=None, allow_scan_hit: bool = False,
                   on_progress=None, on_note=None, detailed_log: bool = False,
                   progress_interval: float = 3.0, env: dict[str, str] | None = None,
                   create=native_prefix_create, run=run_native,
                   timeline=None) -> NativeOutcome:
    """Run an approved native plan, in the command line's order: the ClamAV scan (if any),
    the prefix creation (if asked), then the unpack. Meant for a worker thread.

    Raises scan.ScanBlocked when the scan stops the run (so the caller can offer the override
    the Wine route offers). Every other problem is returned as a NativeOutcome. The caller
    does the cleanup, as before.
    """
    import subprocess

    from gitfurl.extract import ExtractRefused
    from gitfurl.memlimit import MemoryLimitError
    from gitfurl.prefixes import PrefixStopped

    summary = None
    if scan_plan is not None:
        from gitfurl.scan import ScanBlocked, decide, run_scan

        result = scan_result if scan_result is not None else \
            run_scan(scan_plan, should_stop=lambda: control.cancelled)
        if control.cancelled and scan_result is None:
            return NativeOutcome(130, "Stopped during the ClamAV scan. Nothing was run or "
                                      "created.")
        proceed, summary = decide(result, allow_scan_hit)
        if not proceed:
            if timeline is not None:
                timeline.add("note", "The ClamAV scan stopped the run", summary)
            raise ScanBlocked(summary, result)
        if timeline is not None:
            timeline.add("note", "ClamAV scan passed", summary)
        if on_note is not None:
            on_note(summary)
    xplan = plan.extract
    if plan.create_prefix:
        if control.cancelled:
            return NativeOutcome(130, "Stopped before the prefix was created. Nothing was "
                                      "unpacked.", scan_summary=summary)
        if timeline is not None:
            timeline.add("prefix", "Creating the prefix with wineboot",
                         "it does not exist yet; Wine's Mono and Gecko dialogs are off for "
                         "this command, so it needs no answer")
        try:
            status = create(plan, memory_limit, env, should_stop=lambda: control.cancelled)
        except MemoryLimitError as exc:
            return NativeOutcome(2, f"{exc}. Nothing was unpacked.", scan_summary=summary)
        except PrefixStopped as exc:
            return NativeOutcome(130, f"{exc} Nothing was unpacked.", scan_summary=summary)
        except FileExistsError as exc:
            return NativeOutcome(1, f"{exc}. Nothing was unpacked.", scan_summary=summary)
        except subprocess.TimeoutExpired:
            return NativeOutcome(1, "wine did not finish in time; the prefix folder may be "
                                    "incomplete. Nothing was unpacked.", scan_summary=summary)
        except RuntimeError as exc:
            return NativeOutcome(1, f"{exc} Nothing was unpacked.", scan_summary=summary)
        if status != 0:
            return NativeOutcome(1, f"creating the prefix failed (wine exited {status}). "
                                    "Nothing was unpacked.", scan_summary=summary)
        if timeline is not None:
            timeline.add("prefix", "Prefix created", "wineboot exited with status 0")
        problem = xplan.problem()
        if problem:
            return NativeOutcome(1, f"{problem}. The prefix was created; nothing was written "
                                    "in it.", scan_summary=summary, prefix_created=True)
    if control.cancelled:
        return NativeOutcome(130, "Stopped before the unpack. Nothing was unpacked.",
                             scan_summary=summary, prefix_created=plan.create_prefix)
    existed = os.path.lexists(xplan.dest)  # extract() refuses a destination that exists
    try:
        res = run(plan, cancel=control.event, detailed_log=detailed_log,
                  progress_interval=progress_interval, env=env, scan_summary=summary,
                  on_progress=on_progress,
                  **({"timeline": timeline} if timeline is not None else {}))
    except ExtractRefused as exc:
        if exc.stopped:
            return NativeOutcome(130, "Stopped before the unpack. Nothing was unpacked.",
                                 scan_summary=summary, prefix_created=plan.create_prefix)
        return NativeOutcome(1, f"{exc}. {exc.outcome()}", scan_summary=summary,
                             prefix_created=plan.create_prefix)
    except OSError as exc:
        why = exc.strerror or exc
        if not existed and os.path.lexists(xplan.dest):
            msg = (f"the log could not be written ({why}) after the unpack started; files "
                   f"already written in {xplan.dest} are kept and nothing was deleted. The "
                   "unpack's result was not recorded.")
        else:
            msg = f"the log could not be written ({why}). Nothing was unpacked."
        return NativeOutcome(1, msg, scan_summary=summary, prefix_created=plan.create_prefix)
    return NativeOutcome(res.status, None, res, summary, plan.create_prefix)
