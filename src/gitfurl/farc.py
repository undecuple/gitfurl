"""Read-only inspection of FreeArc archives (files that start with "ArC" 0x01).

What it reads: the footer block at the end of the file and the directory blocks it points
to (the archive's own index), with bounded reads. From those it reports the files, their
sizes, where the archive starts (the size of a self-extracting program before it, if any),
whether the archive is locked, and for each data block whether its method is `storing`, a
single `lzma`, or a method gitfurl does not handle.

What it never does: it never extracts, decodes or verifies file data, never writes or
creates anything, never runs anything, and never follows a symlink. Only the index itself
is decompressed (when it is stored with `lzma`), and only up to MAX_CONTROL_BLOCK bytes.

What it refuses (FarcError): anything that is not a regular file; a file with no footer in
its last FOOTER_SCAN bytes; an index whose CRC-32 does not match; an index that is
encrypted or stored with a method other than `storing` or `lzma`; counts and sizes over
the caps below (bytes read, bytes decoded, name length, and the total length of the
folder-and-name paths, which a shared folder name could otherwise multiply); and offsets,
lengths or blocks (header, directory and data blocks) that lie outside the file or overlap.

All text from the archive (file and folder names) is untrusted. summarise() passes it
through detect.clean() before it is shown, and never shows an archive's method string.

The layout is restated from FreeArc's published format notes; no FreeArc code is used.
"""

from __future__ import annotations

import lzma
import os
import re
import stat
import zlib
from dataclasses import dataclass, field
from pathlib import Path

SIGNATURE = b"ArC\x01"
FOOTER_SCAN = 4096  # the footer's descriptor is in the last 4096 bytes
MAX_CONTROL_BLOCK = 64 * 1024 * 1024  # cap for one footer or directory block (packed or not)
MAX_INDEX_TOTAL = 256 * 1024 * 1024  # cap for all index bytes read, and decoded, per archive
MAX_NAME = 4096  # bytes of one stored string: a file name, folder name or method
MAX_PATH_TOTAL = 128 * 1024 * 1024  # characters of all folder/name paths of one archive
MAX_FILES = 1_000_000
MAX_BLOCKS = 100_000
DICT_CAP = 1 << 30  # 1 GiB: larger LZMA dictionaries are not handled
MAX_EXAMPLES = 3  # unsafe names shown in a summary
NAME_LIMIT = 80  # characters of one name shown in a summary

HEADER_BLOCK = 1
DIR_BLOCK = 3
FOOTER_BLOCK = 4

KIND_STORING = "storing"
KIND_LZMA = "lzma"
KIND_OTHER = "a method gitfurl does not handle"

_MEM_UNITS = {"b": 1, "k": 1 << 10, "m": 1 << 20, "g": 1 << 30}
_MATCH_FINDERS = {"bt2", "bt3", "bt4", "hc4", "ht4"}
_PRESETS = {"fastest", "fast", "normal", "max", "ultra"}
_CIPHERS = ("aes", "blowfish", "twofish", "serpent")
_DRIVE = re.compile(r"^[A-Za-z]:")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")  # exactly Unicode category Cc


class FarcError(ValueError):
    """The file is not a FreeArc archive gitfurl can read, or its structure is damaged."""


@dataclass(frozen=True)
class Method:
    """A classified compressor string. The string itself is not kept, so it cannot be shown."""

    supported: bool
    reason: str = ""  # generic wording only; never contains the archive's text
    lzma: dict | None = None  # dict_size, lc, lp, pb for a supported lzma method
    encrypted: bool = False

    @property
    def kind(self) -> str:
        if not self.supported:
            return KIND_OTHER
        return KIND_LZMA if self.lzma else KIND_STORING


@dataclass(frozen=True, slots=True)
class Entry:
    path: str  # folder and name joined with "/", as stored (not checked for safety)
    size: int
    mtime: int
    is_dir: bool
    crc: int
    block: int  # index into Archive.blocks


@dataclass(frozen=True)
class DataBlock:
    method: Method
    offset: int  # absolute position in the file
    compsize: int
    origsize: int  # sum of the sizes of its files


@dataclass
class Archive:
    path: Path
    size: int
    sfx_size: int
    locked: bool
    blocks: list[DataBlock] = field(default_factory=list)
    entries: list[Entry] = field(default_factory=list)

    @property
    def total_size(self) -> int:
        return sum(e.size for e in self.entries)

    @property
    def unsupported(self) -> list[DataBlock]:
        return [b for b in self.blocks if not b.method.supported]


def _parse_int(s: str) -> int | None:
    s = s.removeprefix("=")
    return int(s) if s.isascii() and s.isdigit() and len(s) <= 12 else None


def _parse_mem(s: str) -> int | None:
    """FreeArc memory sizes: digits, then b/k/m/g (only the first letter counts), else 2^n."""
    s = s.removeprefix("=")
    i = 0
    while i < len(s) and s[i].isascii() and s[i].isdigit():
        i += 1
    if i == 0 or i > 12:
        return None
    n = int(s[:i])
    unit = s[i].lower() if i < len(s) else "^"
    if unit == "^":
        return 1 << n if n < 64 else None
    return n * _MEM_UNITS[unit] if unit in _MEM_UNITS else None


def parse_method(text: str) -> Method:
    """Classify one compressor string. Never raises."""
    encrypted = any(c in text.lower() for c in _CIPHERS)

    def no(reason: str) -> Method:
        return Method(False, reason, encrypted=encrypted)

    if encrypted:
        return no("encryption")
    if "+" in text:
        return no("a chain of methods")
    params = text.split(":")
    name = params[0]
    if name == "storing":
        return Method(True) if len(params) == 1 else no(KIND_OTHER)
    if name != "lzma":
        return no(KIND_OTHER)
    d: int | None = 64 << 20
    lc: int | None = 3
    lp: int | None = 0
    pb: int | None = 2
    for raw in params[1:]:
        p = raw.removeprefix("*")
        value: int | None = 0
        if p.startswith("d"):
            value = d = _parse_mem(p[1:])
        elif p.startswith("h") and _parse_mem(p[1:]) is not None:
            pass
        elif p.startswith(("fb", "mc")):
            value = _parse_int(p[2:])
        elif p.startswith("a") and not p[1:2].isalpha():
            value = _parse_int(p[1:])
        elif p.startswith("lc"):
            value = lc = _parse_int(p[2:])
        elif p.startswith("lp"):
            value = lp = _parse_int(p[2:])
        elif p.startswith("pb"):
            value = pb = _parse_int(p[2:])
        elif p.startswith("mf"):
            value = 0 if p[2:].lstrip("=").lower() in _MATCH_FINDERS else None
        elif p in _PRESETS or p.lower() in _MATCH_FINDERS or _parse_int(p) is not None:
            pass
        elif _parse_mem(p) is not None:
            d = _parse_mem(p)
        else:
            value = None
        if value is None and not raw.startswith("*"):
            return no("lzma settings gitfurl does not understand")
    if d is None or lc is None or lp is None or pb is None:
        return no("lzma settings gitfurl does not understand")
    if d > DICT_CAP:
        return no(f"an lzma dictionary over the {DICT_CAP >> 20} MiB limit")
    if lc > 4 or lp > 4 or lc + lp > 4 or pb > 4:
        return no("lzma settings the decoder does not support")
    return Method(True, lzma={"dict_size": max(d, 4096), "lc": lc, "lp": lp, "pb": pb})


class _Buf:
    """Bounds-checked reader over one decoded index block."""

    def __init__(self, data: bytes):
        self.data, self.pos = data, 0

    def _take(self, n: int) -> bytes:
        if n < 0 or self.pos + n > len(self.data):
            raise FarcError("archive structure damaged (read past the end of a block)")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b

    def varint(self) -> int:
        """Little-endian; the low 1-bits before the first 0-bit count the extra bytes."""
        if self.pos >= len(self.data):
            raise FarcError("archive structure damaged (read past the end of a block)")
        first = self.data[self.pos]
        ones = 0
        while ones < 8 and first >> ones & 1:
            ones += 1
        if ones == 8:  # 0xFF: a plain 8-byte integer follows
            self._take(1)
            return int.from_bytes(self._take(8), "little")
        return int.from_bytes(self._take(ones + 1), "little") >> (ones + 1)

    def count(self, cap: int, what: str) -> int:
        n = self.varint()
        if n > cap:
            raise FarcError(f"archive structure damaged (more than {cap} {what})")
        return n

    def u8(self) -> int:
        return self._take(1)[0]

    def u32(self) -> int:
        return int.from_bytes(self._take(4), "little")

    def cstr(self) -> str:
        end = self.data.find(b"\0", self.pos, self.pos + MAX_NAME + 1)
        if end < 0:
            if self.data.find(b"\0", self.pos) < 0:
                raise FarcError("archive structure damaged (unterminated string)")
            raise FarcError(f"archive structure damaged (a name longer than {MAX_NAME} bytes)")
        s = self.data[self.pos:end].decode("utf-8", errors="replace")
        self.pos = end + 1
        return s


def _pread(fd: int, size: int, pos: int, n: int) -> bytes:
    if pos < 0 or n < 0 or pos + n > size:
        raise FarcError("archive structure damaged (a block lies outside the file)")
    try:
        data = os.pread(fd, n, pos)
    except OSError as exc:
        raise FarcError(f"the file could not be read ({exc.strerror or exc})") from exc
    if len(data) != n:
        raise FarcError("the file changed or could not be read completely")
    return data


class _Reader:
    def __init__(self, fd: int, size: int, index_limit: int | None = None):
        self.fd, self.size = fd, size
        # Caps are read here, not at import, so a caller (or a test) can lower them.
        self.index_limit = MAX_INDEX_TOTAL if index_limit is None \
            else min(index_limit, MAX_INDEX_TOTAL)
        self.path_limit = min(MAX_PATH_TOTAL, self.index_limit)
        self.read = self.decoded = 0  # index bytes read from the file / decoded, so far
        self.path_chars = 0  # characters of all joined folder/name paths, so far

    def control_block(self, method_text: str, pos: int, compsize: int, origsize: int,
                      crc: int) -> _Buf:
        """Read, decode (storing or lzma only) and CRC-check one index block."""
        method = parse_method(method_text)
        if method.encrypted:
            raise FarcError("the archive's own index is encrypted; gitfurl does not handle "
                            "encrypted archives")
        if not method.supported:
            raise FarcError(f"the archive's index uses {KIND_OTHER}")
        if origsize > MAX_CONTROL_BLOCK or compsize > MAX_CONTROL_BLOCK:
            raise FarcError("the archive's index is larger than gitfurl reads")
        # Both sides count: a small origsize with a large compsize must not be re-read
        # over and over at no cost.
        self.read += compsize
        self.decoded += origsize
        if self.read > self.index_limit or self.decoded > self.index_limit:
            raise FarcError("the archive's index is larger than gitfurl reads")
        raw = _pread(self.fd, self.size, pos, compsize)
        if method.lzma:
            # Output is capped at origsize, so no match can reach further back than that:
            # a dictionary larger than the output is never needed and is not allocated.
            opts = dict(method.lzma, dict_size=min(method.lzma["dict_size"],
                                                   max(4096, origsize)))
            try:
                dec = lzma.LZMADecompressor(lzma.FORMAT_RAW,
                                            filters=[{"id": lzma.FILTER_LZMA1, **opts}])
                data = dec.decompress(raw, max_length=origsize)
            except (lzma.LZMAError, ValueError, MemoryError) as exc:
                raise FarcError("the archive's index could not be decompressed") from exc
        else:
            if compsize != origsize:
                raise FarcError("archive structure damaged (index sizes do not agree)")
            data = raw
        if len(data) != origsize or zlib.crc32(data) != crc:
            raise FarcError("archive structure damaged (index failed its CRC check)")
        return _Buf(data)


def has_signature(path: Path) -> bool:
    """True when a regular file (not a symlink) starts with the FreeArc signature."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError):
        return False
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            return False
        return os.pread(fd, 4, 0) == SIGNATURE
    except OSError:
        return False
    finally:
        os.close(fd)


def read_archive(path: Path, index_limit: int | None = None) -> Archive:
    """Read the index of one archive. Raises FarcError (only) for anything it cannot read."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or "bad path"
        raise FarcError(f"the file could not be opened ({reason})") from exc
    try:
        return read_fd(fd, path, index_limit)
    finally:
        os.close(fd)


def read_fd(fd: int, path: Path, index_limit: int | None = None) -> Archive:
    """Read the index of an archive from a file descriptor the caller already opened.

    Used so that the file whose signature was checked is the same file whose index is read.
    The descriptor is not closed here. `path` is only recorded in the result.
    `index_limit` lowers the cap on index bytes read and decoded (MAX_INDEX_TOTAL at most);
    the cap on joined path text follows it.
    """
    try:
        st = os.fstat(fd)
    except OSError as exc:
        raise FarcError(f"the file could not be read ({exc.strerror or exc})") from exc
    if not stat.S_ISREG(st.st_mode):
        raise FarcError("not a regular file")
    return _read(_Reader(fd, st.st_size, index_limit), Path(path))


def _footer_descriptor(tail: bytes) -> tuple[int, int, str, int, int, int]:
    """Find the footer's descriptor: the last signature in `tail` whose CRC-32 matches.

    Returns (offset in tail, type, method, origsize, compsize, crc).
    """
    end, seen = len(tail) - 4, False
    while True:
        at = tail.rfind(SIGNATURE, 0, end)
        if at < 0:
            break
        seen = True
        d = tail[at:]
        if len(d) >= 8 and zlib.crc32(d[:-4]) == int.from_bytes(d[-4:], "little"):
            b = _Buf(d[:-4])
            b.u32()
            return at, b.varint(), b.cstr(), b.varint(), b.varint(), b.u32()
        end = at + len(SIGNATURE) - 1
    if seen:
        raise FarcError("archive structure damaged (footer descriptor failed its CRC check)")
    raise FarcError("no FreeArc footer found at the end of the file")


def _read(r: _Reader, path: Path) -> Archive:
    size = r.size
    if size < 16:
        raise FarcError("too small to be a FreeArc archive")
    scan = min(size, FOOTER_SCAN)
    tail = _pread(r.fd, size, size - scan, scan)
    at, btype, method_text, origsize, compsize, crc = _footer_descriptor(tail)
    dpos = size - scan + at
    if btype != FOOTER_BLOCK or origsize <= 0 or compsize <= 0 or compsize > dpos:
        raise FarcError("archive structure damaged (unexpected footer descriptor)")
    fpos = dpos - compsize
    fb = r.control_block(method_text, fpos, compsize, origsize, crc)
    descriptors = []
    for _ in range(fb.count(MAX_BLOCKS, "blocks")):
        t, m, rel, osz, csz, c = fb.varint(), fb.cstr(), fb.varint(), fb.varint(), \
            fb.varint(), fb.u32()
        descriptors.append((t, m, fpos - rel, osz, csz, c))
    if not descriptors:
        raise FarcError("archive structure damaged (empty footer)")
    locked = bool(fb.u8())
    sfx = descriptors[0][2]
    if not 0 <= sfx < fpos or _pread(r.fd, size, sfx, 4) != SIGNATURE:
        raise FarcError("archive structure damaged (the index does not point at the start "
                        "of a FreeArc archive)")
    arc = Archive(path, size, sfx, locked)
    # Header and directory blocks must lie inside the archive and must not overlap, checked
    # before any is decoded: one directory block cannot be listed (and read) many times.
    regions = [(pos, csz) for t, _, pos, _, csz, _ in descriptors
               if t in (HEADER_BLOCK, DIR_BLOCK)]
    for pos, csz in regions:
        if pos < sfx or pos + csz > fpos:
            raise FarcError("archive structure damaged (a block lies outside the archive)")
    _check_overlap(regions)
    dirs = 0
    for t, m, pos, osz, csz, c in descriptors:
        if t == DIR_BLOCK:
            _read_directory(arc, r.control_block(m, pos, csz, osz, c), pos, r)
            dirs += 1
    if not dirs:
        raise FarcError("archive structure damaged (no directory block)")
    _check_overlap(regions + [(b.offset, b.compsize) for b in arc.blocks])
    return arc


def _read_directory(arc: Archive, b: _Buf, dir_pos: int, r: _Reader) -> None:
    nblocks = b.count(MAX_BLOCKS, "data blocks")
    if nblocks + len(arc.blocks) > MAX_BLOCKS:
        raise FarcError(f"archive structure damaged (more than {MAX_BLOCKS} data blocks)")
    counts = [b.count(MAX_FILES, "files") for _ in range(nblocks)]
    total = sum(counts)
    if total + len(arc.entries) > MAX_FILES:
        raise FarcError(f"archive lists more than {MAX_FILES} files")
    methods = [b.cstr() for _ in range(nblocks)]
    offsets = [b.varint() for _ in range(nblocks)]
    compsizes = [b.varint() for _ in range(nblocks)]
    folders = [b.cstr() for _ in range(b.count(MAX_FILES, "folders"))]
    names = [b.cstr() for _ in range(total)]
    dirnums = [b.varint() for _ in range(total)]
    sizes = [b.varint() for _ in range(total)]
    times = [b.u32() for _ in range(total)]
    isdir = [b.u8() for _ in range(total)]
    crcs = [b.u32() for _ in range(total)]
    first = len(arc.blocks)
    i = 0
    for k in range(nblocks):
        orig = 0
        for _ in range(counts[k]):
            if dirnums[i] >= len(folders):
                raise FarcError("archive structure damaged (bad folder number)")
            folder = folders[dirnums[i]]
            # Many files can share one folder name, so the joined text is capped as a whole:
            # a small index must not expand into gigabytes of paths.
            r.path_chars += len(folder) + 1 + len(names[i])
            if r.path_chars > r.path_limit:
                raise FarcError("the archive's file list is longer than gitfurl reads")
            path = f"{folder}/{names[i]}" if folder else names[i]
            arc.entries.append(Entry(path, sizes[i], times[i], bool(isdir[i]), crcs[i],
                                     first + k))
            orig += sizes[i]
            i += 1
        pos = dir_pos - offsets[k]
        if pos < arc.sfx_size or pos + compsizes[k] > dir_pos:
            raise FarcError("archive structure damaged (a data block lies outside the archive)")
        method = parse_method(methods[k])
        if method.kind == KIND_STORING and compsizes[k] != orig:
            raise FarcError("archive structure damaged (stored block sizes do not agree)")
        arc.blocks.append(DataBlock(method, pos, compsizes[k], orig))


def _check_overlap(regions: list[tuple[int, int]]) -> None:
    """Refuse when any two (offset, size) regions of the file overlap."""
    end = -1
    for offset, size in sorted(regions):
        if offset < end:
            raise FarcError("archive structure damaged (blocks overlap)")
        end = offset + size


def unsafe_name(path: str) -> bool:
    """True when a stored name could not be used as-is under a destination folder later.

    Empty names, absolute paths, ".." parts, drive letters, backslashes and control
    characters all count. Nothing is extracted here; this only feeds the summary.
    """
    if not path or path.endswith("/") or path.startswith("/") or "\\" in path:
        return True
    if _CONTROL.search(path):
        return True
    parts = path.split("/")
    return any(p == ".." or _DRIVE.match(p) for p in parts)


def _size(n: int) -> str:
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if n >= 1 << shift:
            return f"{n / (1 << shift):.1f} {unit}"
    return f"{n} bytes" if n != 1 else "1 byte"


def _n(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def summarise(arc: Archive) -> list[str]:
    """Plain-text lines for the plan: counts and sizes only, names cleaned and capped."""
    from .detect import clean  # imported here: detect.py imports this module

    files = sum(1 for e in arc.entries if not e.is_dir)
    folders = sum(1 for e in arc.entries if e.is_dir and e.path not in ("", "."))
    lines = [(f"{_n(files, 'file', 'files')} and {_n(folders, 'folder', 'folders')}, "
             f"{_size(arc.total_size)} in total")]
    kinds = {KIND_STORING: 0, KIND_LZMA: 0, KIND_OTHER: 0}
    for blk in arc.blocks:
        kinds[blk.method.kind] += 1
    parts = [f"{kinds[KIND_STORING]:,} storing", f"{kinds[KIND_LZMA]:,} lzma"]
    if kinds[KIND_OTHER]:
        parts.append(f"{kinds[KIND_OTHER]:,} {KIND_OTHER}")
    lines.append(f"{_n(len(arc.blocks), 'data block', 'data blocks')}: " + ", ".join(parts))
    if arc.sfx_size:
        lines.append(f"the archive starts after a {_size(arc.sfx_size)} program "
                     "(self-extracting stub)")
    lines.append("locked: yes" if arc.locked else "locked: no")
    unsafe = [e.path for e in arc.entries if unsafe_name(e.path)]
    if unsafe:
        shown = ", ".join(repr(clean(p, NAME_LIMIT)) for p in unsafe[:MAX_EXAMPLES])
        more = ", ..." if len(unsafe) > MAX_EXAMPLES else ""
        lines.append(f"{_n(len(unsafe), 'name', 'names')} would not be safe to extract "
                     f"as stored: {shown}{more}")
    return lines
