"""Read-only format detection for an installer, from file headers and archive indexes.

Nothing here runs the installer or any other program, opens an archive with a third-party
library, extracts anything, or writes anything. It reads a bounded number of bytes, treats
the file as hostile and only ever reports a guess. A guess is not a promise that anything
can be extracted or installed.

It recognises two things, each with a confidence that says how sure it is:
  * Inno Setup: a Windows program (starts with "MZ") that holds an Inno Setup setup-header
    ID string such as "Inno Setup Setup Data (5.5.0)". The ID format is documented in
    innoextract's source. A string match alone is "likely": the offset table that would
    make it "certain" is not checked, so this module never selects a native route.
  * FreeArc: the four bytes "ArC" 0x01 at the start of a file (the installer itself, or a
    large file beside it, at most MAX_NEIGHBOURS of those opened). For each such file the
    archive's own index is read with gitfurl.farc (bounded reads, CRC-32 checked) from the
    same open file whose signature was checked, so the file is opened once. When the
    index reads cleanly the evidence is "likely" and the plan shows its file count, total
    size and, per data block, whether it uses storing, lzma or a method gitfurl does not
    handle. When it does not, the evidence stays "possible" and says why.

The data inside an archive is never decoded, and the files in it are never extracted.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

from gitfurl import farc

ROUTE_WINE = "wine"
ROUTE_NATIVE = "native"  # declared for later phases; decide() never returns it yet
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
                found = _find_inno(fh, size)
                if found:
                    offset, ident = found
                    det.evidence.append(Evidence(
                        "Inno Setup", CONF_LIKELY, f"byte offset {offset}",
                        f"setup header ID {clean(ident)!r}"))
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
                        "(nothing was extracted)", lines)
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
    """Pick a route: the Wine/Proton path, or unsupported when the file is unreadable.

    Native extraction does not exist yet, so ROUTE_NATIVE is never returned here, even when
    a FreeArc index was read.
    """
    if not det.readable:
        return Verdict(ROUTE_UNSUPPORTED, ["the file could not be read as a regular file"])
    reasons = ["Wine/Proton is the default route"]
    if det.evidence:
        reasons.append("native extraction is not available in this version, so a detection "
                       "changes nothing about the run unless you choose the unattended option "
                       "(Inno Setup only)")
    return Verdict(ROUTE_WINE, reasons)


def describe(det: Detection) -> str:
    """Plain text for the plan preview and the approval dialog."""
    lines = [("detected from file headers and archive indexes (read-only; nothing was run or "
              "extracted):")]
    if not det.readable:
        lines.append("  the file could not be read as a regular file")
    elif det.evidence:
        for ev in det.evidence:
            lines.append(f"  {ev.format}: {ev.confidence}. {ev.detail}; {ev.where}")
            lines.extend(f"    {line}" for line in ev.lines)
    else:
        lines.append("  no known format recognised (this says nothing about whether it runs)")
    lines.append("  the data inside any payload is not decoded or guessed; for FreeArc only "
                 "the archive's own index (names, sizes, methods) is read")
    lines.extend(f"  note: {n}" for n in det.notes)
    verdict = decide(det)
    lines.append(f"route: {verdict.route}. " + "; ".join(verdict.reasons) + ".")
    lines.append("A detection is a guess from a few bytes, not a promise that anything can be "
                 "extracted or installed.")
    return "\n".join(lines)
