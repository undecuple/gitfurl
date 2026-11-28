"""Read-only format detection for an installer, from file headers and archive indexes.

Nothing here runs the installer or any other program, opens an archive with a third-party
library, extracts anything, or writes anything. It reads a bounded number of bytes, treats
the file as hostile and only ever reports a guess. A guess is not a promise that anything
can be extracted or installed.

It recognises two things, each with a confidence that says how sure it is:
  * Inno Setup: a Windows program (starts with "MZ"). gitfurl.inno reads the setup's own
    offset table, format ID and header blocks (bounded reads, CRC-32 checked) from the same
    open file (or, when setup.exe has no loader, from <stem>-0.bin beside it); when that
    works the evidence lists what Inno itself would write, where its data is (slice files
    beside the installer are listed and checked by name), the [Run]
    entries a silent install would run (redistributables marked), and the files the setup
    names beside itself, checked against the folder (the archive-name preflight). When it
    does not, a setup-header ID string such as "Inno Setup Setup Data (5.5.0)" found in the
    first MAX_SCAN_BYTES still gives "likely", with the reason the data was not read. Either
    way this module only reports it (gitfurl.route decides).
  * FreeArc: the four bytes "ArC" 0x01 at the start of a file (the installer itself, or a
    large file beside it, at most MAX_NEIGHBOURS of those opened). For each such file the
    archive's own index is read with gitfurl.farc (bounded reads, CRC-32 checked) from the
    same open file whose signature was checked, so the file is opened once. When the
    index reads cleanly the evidence is "likely" and the plan shows its file count, total
    size and, per data block, whether it uses storing, lzma, another chain of methods gitfurl
    decodes, or a method gitfurl does not handle. When it does not, the evidence stays "possible" and says why.

The data inside an archive is never decoded, and the files in it are never extracted. The
route itself (native or Wine/Proton) is chosen in gitfurl.route; describe() prints that
choice when the caller passes it.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from gitfurl import farc, inno

ROUTE_WINE = "wine"
ROUTE_NATIVE = "native"  # chosen only by gitfurl.route.choose_route
ROUTE_UNSUPPORTED = "unsupported"

MAX_SCAN_BYTES = 32 * 1024 * 1024  # how far into the installer the Inno ID is looked for
CHUNK = 1024 * 1024
OVERLAP = 256
MAX_NEIGHBOURS = 16  # large files beside the installer whose first 4 bytes are read
MAX_ENTRIES = 1000  # directory entries examined at all (a huge folder is not walked in full)
MIN_NEIGHBOUR_BYTES = 1 << 20
# A FreeArc file beside the installer has its index read with this lower cap (bytes read and
# decoded), so a folder of hostile files cannot hold up the plan for long. A real index is
# far smaller. The installer itself uses farc's own cap.
NEIGHBOUR_INDEX_LIMIT = 16 * 1024 * 1024

MZ = b"MZ"
FREEARC_SIGNATURE = b"ArC\x01"
_INNO_ID = re.compile(
    rb"(?:My )?Inno Setup(?: Extensions)? Setup Data \((\d+(?:\.\d+)+[A-Za-z]?)\)((?: \([uU]\))?)")
_INNO_LEGACY = re.compile(rb"i1\.2\.10--(?:16|32)\x1a")

CONF_LIKELY = "likely"
CONF_POSSIBLE = "possible"


def clean(text: str, limit: int = 120) -> str:
    """File names and IDs come from untrusted files: keep printable characters only."""
    # Cut first: only the part that is shown is looked at, however long the text is.
    out = "".join(c if c.isprintable() else "?" for c in text[:limit])
    return out if len(text) <= limit else out + "..."


@dataclass
class Evidence:
    format: str
    confidence: str
    where: str
    detail: str
    lines: list[str] = field(default_factory=list)  # extra plan lines (already cleaned)
    # FreeArc only: True when the index read cleanly and every data block uses a method
    # gitfurl can decode; False when some block does not; None when not known.
    extractable: bool | None = None


@dataclass
class Detection:
    path: Path
    size: int | None = None
    readable: bool = True
    evidence: list[Evidence] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass
class Verdict:
    route: str
    reasons: list[str]


def _open_regular(path: Path):
    """Open a regular file for reading without following a final symlink or blocking on a FIFO."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise OSError("not a regular file")
        return os.fdopen(fd, "rb", buffering=0), info.st_size
    except BaseException:
        os.close(fd)
        raise


def _find_inno(fh, size: int) -> tuple[int, str] | None:
    """Look for an Inno Setup ID in the first MAX_SCAN_BYTES. Returns (offset, id) or None."""
    fh.seek(0)
    pos, tail = 0, b""
    limit = min(size, MAX_SCAN_BYTES)
    while pos < limit:
        data = fh.read(min(CHUNK, limit - pos))
        if not data:
            break
        buf = tail + data
        base = pos - len(tail)
        for rx in (_INNO_ID, _INNO_LEGACY):
            m = rx.search(buf)
            if m:
                return base + m.start(), m.group(0).decode("ascii", "replace").rstrip("\x1a")
        pos += len(data)
        tail = buf[-OVERLAP:]
    return None


def detect(path: Path) -> Detection:
    """Identify what the installer looks like from headers and archive indexes.

    Never raises for a bad file.
    """
    det = Detection(path=Path(path))
    try:
        fh, size = _open_regular(Path(path))
    except OSError as exc:
        det.readable = False
        det.notes.append(f"could not read the file ({exc.strerror or exc})")
        return det
    det.size = size
    head = b""
    freearc = None
    with fh:
        try:
            head = fh.read(4)
            if head[:2] == MZ:
                setup, why = _read_inno(fh.fileno(), Path(path), size)
                found = None if setup else _find_inno(fh, size)
                if setup:
                    read = ("offset table and header blocks were read" if setup.loader else
                            "header blocks were read (setup.exe has no loader)")
                    det.evidence.append(Evidence(
                        "Inno Setup", CONF_LIKELY, setup.where,
                        f"setup data format {setup.format} ({clean(setup.format_id)!r}); its "
                        f"{read} and their CRC-32 checks match (nothing was extracted or run)",
                        _summarise_inno(setup)))
                elif found:
                    offset, ident = found
                    det.evidence.append(Evidence(
                        "Inno Setup", CONF_LIKELY, f"byte offset {offset}",
                        f"setup header ID {clean(ident)!r}",
                        [f"the setup's own data was not read: {why}"] if why else []))
                    if size > MAX_SCAN_BYTES:
                        det.notes.append(f"only the first {MAX_SCAN_BYTES >> 20} MiB were "
                                         "searched")
                elif size > MAX_SCAN_BYTES:
                    det.notes.append(f"no Inno Setup ID in the first {MAX_SCAN_BYTES >> 20} MiB; "
                                     "the rest was not searched")
        except OSError as exc:
            det.notes.append(f"reading stopped early ({exc.strerror or exc})")
        if head == FREEARC_SIGNATURE:  # read from this same open file, not the path again
            freearc = _freearc_evidence(fh.fileno(), Path(path), "start of the installer file")
    if freearc is not None:
        det.evidence.append(freearc)
    _neighbours(det)
    return det


def _read_inno(fd: int, path: Path, size: int):
    """(Setup, None) when gitfurl.inno reads the setup data, else (None, reason or None when
    there is no Inno offset table at all). Never raises."""
    try:
        return inno.read_fd(fd, path, size), None
    except inno.InnoError as exc:
        text = str(exc)
        if text.startswith(("no Inno Setup offset table", "not a Windows program")):
            return None, None
        return None, clean(text, 200)
    except Exception:  # noqa: BLE001 - a hostile file must never stop the plan
        return None, "the setup data could not be read"


def _summarise_inno(setup) -> list[str]:
    """inno.summarise(), never raising: a hostile setup must not stop the plan."""
    try:
        return inno.summarise(setup)
    except Exception:  # noqa: BLE001 - a hostile file must never stop the plan
        return ["the setup data was read, but its summary could not be made"]


def _freearc_evidence(fd: int, path: Path, where: str,
                      index_limit: int | None = None) -> Evidence:
    """Read one FreeArc archive's own index from an open file. Never raises.

    `fd` is the descriptor whose first 4 bytes were checked; the path is not opened again.
    Only the index is decoded; nothing is extracted.
    """
    try:
        arc = farc.read_fd(fd, path, index_limit)
        lines = farc.summarise(arc)
    except farc.FarcError as exc:
        reason = clean(str(exc), 200)
    except MemoryError:
        reason = "there was not enough memory to read the archive's index"
    except Exception:  # noqa: BLE001 - a hostile file must never stop the plan
        reason = "the archive's index could not be read"
    else:
        return Evidence("FreeArc", CONF_LIKELY, where,
                        "the archive's own index was read and its CRC-32 checks match "
                        "(nothing was extracted)", lines, extractable=not arc.unsupported)
    return Evidence("FreeArc", CONF_POSSIBLE, where,
                    "the 4 bytes 'ArC' 0x01, but the archive's index could not be read: "
                    f"{reason}")


def _neighbours(det: Detection) -> None:
    """Read the first 4 bytes of large regular files beside the installer.

    At most MAX_ENTRIES directory entries are examined and at most MAX_NEIGHBOURS files are
    opened, each once; the listing is never sorted as a whole, so a huge folder costs a
    bounded amount. Only those opened files that start with the FreeArc signature have
    their index read, from the same open file.
    """
    hits, looked, seen, partial = [], 0, 0, False
    try:
        it = os.scandir(det.path.parent)
    except OSError:
        return
    with it:
        for e in it:
            seen += 1
            if seen > MAX_ENTRIES:
                partial = True
                break
            try:
                if e.name == det.path.name or not e.is_file(follow_symlinks=False) \
                        or e.stat(follow_symlinks=False).st_size < MIN_NEIGHBOUR_BYTES:
                    continue
            except OSError:
                continue
            if looked >= MAX_NEIGHBOURS:
                partial = True
                break
            looked += 1
            try:
                fh, _ = _open_regular(Path(e.path))
                with fh:
                    if fh.read(4) == FREEARC_SIGNATURE:
                        hits.append((e.name, _freearc_evidence(
                            fh.fileno(), Path(e.path),
                            f"large file beside the installer: {clean(e.name, 60)}",
                            NEIGHBOUR_INDEX_LIMIT)))
            except OSError:
                continue
    hits.sort(key=lambda h: h[0])  # at most MAX_NEIGHBOURS
    det.evidence.extend(ev for _, ev in hits)
    if partial:
        det.notes.append("not all files beside the installer were looked at (at most "
                         f"{MAX_ENTRIES} entries and {MAX_NEIGHBOURS} large files are examined)")


def decide(det: Detection) -> Verdict:
    """The verdict describe() prints when no route was chosen by the caller.

    Only an unreadable file is decided here (unsupported). The route between native and
    Wine/Proton is chosen by gitfurl.route.choose_route, which reads the archives fully.
    """
    if not det.readable:
        return Verdict(ROUTE_UNSUPPORTED, ["the file could not be read as a regular file"])
    return Verdict("not chosen here", [("this text only describes the file; the plan's route "
                                        "is chosen by gitfurl.route")])


def evidence_lines(ev: Evidence, choice=None) -> list[str]:
    """Render observations in their route context without changing the detection.

    Missing source names describe what Setup would look for. On the native route Setup
    does not run, and a chosen name mapping reads the original archive directly.
    """
    if ev.format != "Inno Setup" or choice is None or choice.route != ROUTE_NATIVE:
        return ev.lines
    links = choice.links
    mapped = links.links if links is not None and links.ok else ()
    # Display text is not an identity: cleaning and shortening can make different source
    # names look alike (including a name with no mapping). Only annotate an unambiguous,
    # unchanged name; the chosen mapping is still stated in the route for the others.
    usable = []
    ambiguous = False
    for name, target in mapped:
        shown = clean(name, inno.SHOW)
        prefix = f"file beside the installer: {shown}: missing"
        if shown == name and sum(line.startswith(prefix) for line in ev.lines) == 1:
            usable.append((prefix, shown, target))
        else:
            ambiguous = True
    lines = []
    for line in ev.lines:
        if line.startswith("warning:") and line.endswith(
                "not found beside the installer; a silent run is likely to stop on "
                "the script's own error window"):
            lines.append("note: the source-file names above describe Setup's requirements; "
                         "Setup is not run, and the native unpack reads only the archives "
                         "listed in its plan")
            continue
        for prefix, shown, target in usable:
            if line.startswith(prefix):
                line = (f"file beside the installer: {shown}: supplied by "
                        f"{clean(target, inno.SHOW)} (read directly; no link is made)"
                        + line[len(prefix):])
                break
        lines.append(line)
    if ambiguous:
        lines.append("note: shortened or ambiguous source names are left as observed above; "
                     "the chosen mapping is listed in the route")
    return lines


def describe(det: Detection, choice=None) -> str:
    """Plain text for the plan preview and the approval dialog.

    `choice` is a gitfurl.route.RouteChoice; with it the route line says which route was
    chosen, by whom, and why. Without it the line says no route was chosen here.
    """
    lines = [("detected from file headers and archive indexes (read-only; nothing was run or "
              "extracted):")]
    if not det.readable:
        lines.append("  the file could not be read as a regular file")
    elif det.evidence:
        for ev in det.evidence:
            lines.append(f"  {ev.format}: {ev.confidence}. {ev.detail}; {ev.where}")
            lines.extend(f"    {line}" for line in evidence_lines(ev, choice))
    else:
        lines.append("  no known format recognised (this says nothing about whether it runs)")
    lines.append("  the data inside any payload is not decoded or guessed; for FreeArc only "
                 "the archive's own index (names, sizes, methods) is read, for Inno Setup only "
                 "the setup's own header blocks")
    lines.extend(f"  note: {n}" for n in det.notes)
    if choice is not None:
        lines.append(choice.line())
    else:
        verdict = decide(det)
        lines.append(f"route: {verdict.route}. " + "; ".join(verdict.reasons) + ".")
    lines.append("A detection is a guess from a few bytes, not a promise that anything can be "
                 "extracted or installed.")
    return "\n".join(lines)
