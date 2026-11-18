"""Read-only format detection for an installer, from file headers only.

Nothing here runs the installer or any other program, opens an archive with a third-party
library, or writes anything. It reads a bounded number of bytes, treats the file as hostile
and only ever reports a guess. A guess is not a promise that anything can be extracted or
installed.

Phase 1 only recognises two things, both with a confidence that says how sure it is:
  * Inno Setup: a Windows program (starts with "MZ") that holds an Inno Setup setup-header
    ID string such as "Inno Setup Setup Data (5.5.0)". The ID format is documented in
    innoextract's source. A string match alone is "likely": the offset table that would
    make it "certain" is not checked, so this module never selects a native route.
  * FreeArc: the four bytes "ArC" 0x01 at the start of a file (the installer itself, or a
    large file beside it). That signature is "possible, unverified": the format document
    does not state it, so it rests on one observation of a generated sample.

What is inside the payload (which codec) cannot be seen from headers and is never guessed.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path

ROUTE_WINE = "wine"
ROUTE_NATIVE = "native"  # declared for later phases; decide() never returns it in Phase 1
ROUTE_UNSUPPORTED = "unsupported"

MAX_SCAN_BYTES = 32 * 1024 * 1024  # how far into the installer the Inno ID is looked for
CHUNK = 1024 * 1024
OVERLAP = 256
MAX_NEIGHBOURS = 16  # large files beside the installer whose first 4 bytes are read
MAX_ENTRIES = 1000  # directory entries examined at all (a huge folder is not walked in full)
MIN_NEIGHBOUR_BYTES = 1 << 20

MZ = b"MZ"
FREEARC_SIGNATURE = b"ArC\x01"
_INNO_ID = re.compile(
    rb"(?:My )?Inno Setup(?: Extensions)? Setup Data \((\d+(?:\.\d+)+[A-Za-z]?)\)((?: \([uU]\))?)")
_INNO_LEGACY = re.compile(rb"i1\.2\.10--(?:16|32)\x1a")

CONF_LIKELY = "likely"
CONF_POSSIBLE = "possible"


def clean(text: str, limit: int = 120) -> str:
    """File names and IDs come from untrusted files: keep printable characters only."""
    out = "".join(c if c.isprintable() else "?" for c in text)
    return out if len(out) <= limit else out[:limit] + "..."


@dataclass
class Evidence:
    format: str
    confidence: str
    where: str
    detail: str


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
    """Identify what the installer looks like from headers. Never raises for a bad file."""
    det = Detection(path=Path(path))
    try:
        fh, size = _open_regular(Path(path))
    except OSError as exc:
        det.readable = False
        det.notes.append(f"could not read the file ({exc.strerror or exc})")
        return det
    det.size = size
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
            if head == FREEARC_SIGNATURE:
                det.evidence.append(Evidence(
                    "FreeArc", CONF_POSSIBLE, "start of the installer file",
                    "the 4 bytes 'ArC' 0x01 (signature unverified)"))
        except OSError as exc:
            det.notes.append(f"reading stopped early ({exc.strerror or exc})")
    _neighbours(det)
    return det


def _neighbours(det: Detection) -> None:
    """Read only the first 4 bytes of large regular files beside the installer.

    At most MAX_ENTRIES directory entries are examined and at most MAX_NEIGHBOURS files are
    opened; the listing is never sorted as a whole, so a huge folder costs a bounded amount.
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
                        hits.append(e.name)
            except OSError:
                continue
    if hits:
        hits.sort()
        shown = ", ".join(clean(n, 60) for n in hits[:3]) + (", ..." if len(hits) > 3 else "")
        det.evidence.append(Evidence(
            "FreeArc", CONF_POSSIBLE, f"{len(hits)} large file(s) beside the installer",
            f"start with the 4 bytes 'ArC' 0x01 (signature unverified): {shown}"))
    if partial:
        det.notes.append("not all files beside the installer were looked at (at most "
                         f"{MAX_ENTRIES} entries and {MAX_NEIGHBOURS} large files are examined)")


def decide(det: Detection) -> Verdict:
    """Pick a route. In Phase 1 that is the Wine/Proton path, or unsupported when unreadable.

    Native extraction does not exist yet, so ROUTE_NATIVE is never returned here.
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
    lines = ["detected from file headers (read-only; nothing was run or opened as an archive):"]
    if not det.readable:
        lines.append("  the file could not be read as a regular file")
    elif det.evidence:
        for ev in det.evidence:
            lines.append(f"  {ev.format}: {ev.confidence}. {ev.detail}; {ev.where}")
    else:
        lines.append("  no known format recognised (this says nothing about whether it runs)")
    lines.append("  what is inside any payload is not looked at and not guessed")
    lines.extend(f"  note: {n}" for n in det.notes)
    verdict = decide(det)
    lines.append(f"route: {verdict.route}. " + "; ".join(verdict.reasons) + ".")
    lines.append("A detection is a guess from a few bytes, not a promise that anything can be "
                 "extracted or installed.")
    return "\n".join(lines)
