"""Read-only inspection of FreeArc archives (files that start with "ArC" 0x01).

What it reads: the footer block at the end of the file and the directory blocks it points
to (the archive's own index), with bounded reads. From those it reports the files, their
sizes, where the archive starts (the size of a self-extracting program before it, if any),
whether the archive is locked, and for each data block whether its method is one gitfurl
decodes (a chain of `storing`, `lzma`, `exe`, `x86`, `delta`, `rep`, `4x4` (with `lzma`
inside), `srep`, cm and `rolz` stages) or a method gitfurl does not handle.

What it never does (extraction is in extract.py, which uses this reader): it never
extracts, decodes or verifies file data, never writes or
creates anything, never runs anything, and never follows a symlink. Only the index itself
is decompressed (when it is stored with `lzma`), and only up to MAX_CONTROL_BLOCK bytes.

What it refuses (FarcError): anything that is not a regular file; a file with no footer in
its last FOOTER_SCAN bytes; an index whose CRC-32 does not match (unless explicitly
accepted with a warning); an index that is
encrypted or stored with a method other than a single `storing` or `lzma`; counts and sizes over
the caps below (bytes read, bytes decoded, name length, and the total length of the
folder-and-name paths, which a shared folder name could otherwise multiply); and offsets,
lengths or blocks (header, directory and data blocks) that lie outside the file or overlap.

All text from the archive (file and folder names) is untrusted. summarise() passes it
through detect.clean() before it is shown, and never shows an archive's method string: a
supported chain is shown by gitfurl's own stage names (Method.kind), anything else only as
"a method gitfurl does not handle".

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
DICT_CAP = 1 << 30  # 1 GiB: larger LZMA dictionaries, rep windows or delta blocks are refused
DELTA_DEFAULT = 8 << 20  # delta's block size when the method names none
REP_DEFAULT = 64 << 20  # rep's window size when the method names none
MAX_METHOD_TEXT = 2048  # bytes of one compressor string (FreeArc's own limit)
MAX_STAGE_TEXT = 512  # bytes of one method in it
MAX_FIELDS = 200  # ":"-separated fields of one method
MAX_STAGES = 8  # methods in one chain that gitfurl decodes
X4_PIECE_CAP = DICT_CAP  # a larger 4x4 piece size in the method's own fields is refused
SREP_BLOCK_DEFAULT = 8 << 20  # srep blocks up to this size are always accepted
SREP_BLOCK_MAX = 64 << 20  # an srep block size option above this is refused
# Names under which installers' configuration files register srep; the stream's own header
# decides whether it is one gitfurl reads.
SREP_NAMES = frozenset(["srep", "srep64", "srep_new", "srep_old"])
# The x86 code filter's names in FreeArc's method strings (FreeArc's own name for it is
# dispack; gitfurl calls it x86 and never shows these).
X86_NAMES = frozenset(["dispack070", "dispack"])
X86_DEFAULT = 8 << 20  # its block size when the method names none
# The external method gitfurl calls the cm stage (a context-mixing LZ in independent jobs of
# up to 512 MiB, decoded by gitfurl's optional native module). This is the archive's text for
# it; it is kept only here and never shown.
CM_NAMES = frozenset(["magic2"])

# The external method gitfurl calls rolz (an LZ with reduced-offset matches, an x86 stage and
# long-range copies, in records of its own container). This is the archive's text for it; it
# is kept only here and never shown.
ROLZ_NAMES = frozenset(["rzwb"])
MAX_EXAMPLES = 3  # unsafe names shown in a summary
MAX_KINDS = 4  # chains of supported methods named in a summary (besides storing and lzma)
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
_REP_LETTERS = frozenset("blcdsha")
_DRIVE = re.compile(r"^[A-Za-z]:")
_SEP = re.compile(r"[/\\]")
_CONTROL = re.compile("[\x00-\x1f\x7f-\x9f]")  # exactly Unicode category Cc


class FarcError(ValueError):
    """The file is not a FreeArc archive gitfurl can read, or its structure is damaged."""


@dataclass(frozen=True)
class Stage:
    """One method of a supported chain, by gitfurl's own name (never the archive's text)."""

    name: str  # "storing", "lzma", "exe", "x86", "delta", "rep", "4x4", "srep", "cm" or "rolz"
    lzma: dict | None = None  # lzma, and the lzma inside 4x4: dict_size, lc, lp, pb
    # delta, x86: block size; rep: window size; 4x4: piece size named in its fields (0: none);
    # srep: largest block accepted (from the method's parameters)
    size: int = 0


@dataclass(frozen=True)
class Method:
    """A classified compressor string. The string itself is not kept, so it cannot be shown."""

    supported: bool
    reason: str = ""  # generic wording only; never contains the archive's text
    lzma: dict | None = None  # dict_size, lc, lp, pb when the method is a single lzma
    encrypted: bool = False
    stages: tuple[Stage, ...] = ()  # in the archive's order: stages[0] gives the file data

    @property
    def kind(self) -> str:
        """Safe to show: gitfurl's own stage names joined with "+", or KIND_OTHER."""
        if not self.supported:
            return KIND_OTHER
        return "+".join(st.name for st in self.stages)

    @property
    def single(self) -> bool:
        """A single storing or lzma: the only methods used for the archive's own index."""
        return self.supported and self.kind in (KIND_STORING, KIND_LZMA)


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
    warnings: list[str] = field(default_factory=list)

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


def _parse_size(s: str) -> int | None:
    """Sizes in delta and rep parameters: one optional '=', digits, then an optional lower-case
    unit (as _parse_mem)."""
    t = s.removeprefix("=")
    i = 0
    while i < len(t) and t[i].isascii() and t[i].isdigit():
        i += 1
    if i == 0 or (i < len(t) and t[i] not in "bkmg^"):
        return None
    return _parse_mem(t)


def _parse_lzma(params: list[str]) -> tuple[dict | None, str]:
    """lzma settings (dict_size, lc, lp, pb), or None and a generic reason."""
    d: int | None = 64 << 20
    lc: int | None = 3
    lp: int | None = 0
    pb: int | None = 2
    for raw in params:
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
            return None, "lzma settings gitfurl does not understand"
    if d is None or lc is None or lp is None or pb is None:
        return None, "lzma settings gitfurl does not understand"
    if d > DICT_CAP:
        return None, f"an lzma dictionary over the {DICT_CAP >> 20} MiB limit"
    if lc > 4 or lp > 4 or lc + lp > 4 or pb > 4:
        return None, "lzma settings the decoder does not support"
    return {"dict_size": max(d, 4096), "lc": lc, "lp": lp, "pb": pb}, ""


def _parse_delta(params: list[str]) -> int | None:
    """The block size of a delta stage (default 8 MiB), or None."""
    size = DELTA_DEFAULT
    for p in params:
        if p == "x":
            continue
        size = _parse_size(p.removeprefix("b"))
        if size is None:
            return None
    return size


def _parse_rep(params: list[str]) -> int | None:
    """The window size of a rep stage (default 64 MiB), or None. The other parameters only
    steer compression; they are checked for form and otherwise ignored."""
    window = REP_DEFAULT
    for p in params:
        if p == "max":
            continue
        if p[:1] in _REP_LETTERS:
            value = p[1:]
            if p[0] in "bd":
                size = _parse_size(value)
                if size is None:
                    return None
                if p[0] == "b":
                    window = size
            elif _parse_int(value) is None:
                return None
        elif (p.endswith("%") and _parse_int(p[:-1]) is not None) \
                or _parse_int(p) is not None:
            continue  # a percentage, or the minimum match length
        else:
            size = _parse_size(p)
            if size is None:
                return None
            window = size
    return window


_X4_R = re.compile(r"r\d+(\.\d+)?")


def _parse_4x4(fields: list[str]) -> tuple[Stage | None, str]:
    """4x4's own fields (piece size, threads, buffers, threshold), then the method inside it,
    which must be lzma. The own fields only steer compression; they are checked for form."""
    piece = 0
    for i, f in enumerate(fields):
        if not f:
            return None, "4x4 settings gitfurl does not understand"
        own = f[0].isascii() and f[0].isdigit() or \
            len(f) > 1 and f[1].isascii() and f[1].isdigit()
        if not own:
            inner = fields[i:]
            break
        if f.isascii() and f.isdigit():
            continue  # thread count
        if f[0] in "ti":
            ok = _parse_int(f[1:]) is not None
        elif f[0] == "r":
            ok = _X4_R.fullmatch(f) is not None
        else:
            size = _parse_size(f[1:] if f[0] == "b" else f)
            ok = size is not None
            if ok:
                piece = size
        if not ok:
            return None, "4x4 settings gitfurl does not understand"
    else:
        return None, KIND_OTHER  # no method inside: 4x4's own default, which is not lzma
    if piece > X4_PIECE_CAP:
        return None, f"a 4x4 piece size over the {X4_PIECE_CAP >> 20} MiB limit"
    if inner[0] != "lzma":
        return None, KIND_OTHER
    opts, reason = _parse_lzma(inner[1:])
    return (Stage("4x4", lzma=opts, size=piece), "") if opts else (None, reason)


def _parse_srep(params: list[str]) -> tuple[Stage | None, str]:
    """srep's options are its packer's command-line switches. The stream carries everything
    the decoder needs, except: `index=` (the match lists kept in a file outside the archive)
    is refused, and `b<size>` raises the largest block accepted."""
    block = SREP_BLOCK_DEFAULT
    for p in params:
        if not p or p.startswith("index"):
            return None, "srep settings gitfurl does not understand"
        if p[0] == "b" and p[1:2].isascii() and p[1:2].isdigit():
            size = _parse_size(p[1:])
            if size is None:
                return None, "srep settings gitfurl does not understand"
            if size > SREP_BLOCK_MAX:
                return None, f"an srep block size over the {SREP_BLOCK_MAX >> 20} MiB limit"
            block = max(block, size)
    return Stage("srep", size=block), ""


def _parse_x86(params: list[str]) -> tuple[Stage | None, str]:
    """The x86 code filter's parameters, left to right: `x` (a flag decoding does not use),
    `b<size>` or `<size>` (the block size; a later one wins)."""
    block = X86_DEFAULT
    for p in params:
        if p == "x":
            continue
        size = _parse_mem(p[1:] if p.startswith("b") else p)
        if size is None:
            return None, "x86 settings gitfurl does not understand"
        block = size
    if block == 0:
        return None, "x86 settings gitfurl does not understand"
    if block > DICT_CAP:
        return None, f"an x86 block size over the {DICT_CAP >> 20} MiB limit"
    return Stage("x86", size=block), ""


def _parse_stage(text: str) -> tuple[Stage | None, str]:
    params = text.split(":")
    name, rest = params[0], params[1:]
    if name == "4x4":
        return _parse_4x4(rest)
    if name in SREP_NAMES:
        return _parse_srep(rest)
    if name in X86_NAMES:
        return _parse_x86(rest)
    if name in CM_NAMES:  # it takes no parameters in the archive's text
        return (Stage("cm"), "") if not rest else (None, "cm stage settings gitfurl does not "
                                                          "understand")
    if name in ROLZ_NAMES:  # it takes no parameters in the archive's text
        return (Stage("rolz"), "") if not rest else (None, "rolz settings gitfurl does not "
                                                            "understand")
    if name == "storing":
        return (Stage("storing"), "") if not rest else (None, KIND_OTHER)
    if name == "exe":
        return (Stage("exe"), "") if not rest else (None, KIND_OTHER)
    if name == "lzma":
        opts, reason = _parse_lzma(rest)
        return (Stage("lzma", lzma=opts), "") if opts else (None, reason)
    if name in ("delta", "rep"):
        size = _parse_delta(rest) if name == "delta" else _parse_rep(rest)
        if size is None:
            return None, f"{name} settings gitfurl does not understand"
        if size > DICT_CAP:
            return None, f"a {name} buffer over the {DICT_CAP >> 20} MiB limit"
        if name == "rep" and size == 0:
            return None, "rep settings gitfurl does not understand"
        return Stage(name, size=size), ""
    return None, KIND_OTHER


def parse_method(text: str) -> Method:
    """Classify one compressor string: a chain of storing, lzma, exe, x86, delta, rep, 4x4, srep
    cm and rolz stages, or unsupported. The cm stage reads the block's stored bytes (its jobs
    are found by their lengths in that data) and rolz reads its stored data at random (its index
    is at the end), so each must be the last method, the one that reads the block. Never raises;
    the string itself is not kept."""
    encrypted = any(c in text.lower() for c in _CIPHERS)

    def no(reason: str) -> Method:
        return Method(False, reason, encrypted=encrypted)

    if encrypted:
        return no("encryption")
    if len(text.encode("utf-8", "surrogatepass")) >= MAX_METHOD_TEXT:
        return no(KIND_OTHER)
    parts = text.split("+")
    if len(parts) > MAX_STAGES:
        return no("a longer chain of methods than gitfurl decodes")
    stages = []
    for part in parts:
        if len(part.encode("utf-8", "surrogatepass")) >= MAX_STAGE_TEXT \
                or part.count(":") >= MAX_FIELDS:
            return no(KIND_OTHER)
        stage, reason = _parse_stage(part)
        if stage is None:
            return no(reason)
        stages.append(stage)
    if any(st.name == "cm" for st in stages[:-1]):
        return no("the cm stage only as the last method of a chain")
    if any(st.name == "rolz" for st in stages[:-1]):
        return no("rolz only as the last method of a chain")
    single = stages[0].lzma if len(stages) == 1 and stages[0].name == "lzma" else None
    return Method(True, lzma=single, stages=tuple(stages))


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
    def __init__(self, fd: int, size: int, index_limit: int | None = None, *,
                 accept_bad_index_crc: bool = False):
        self.fd, self.size = fd, size
        self.accept_bad_index_crc = accept_bad_index_crc
        self.warnings: list[str] = []
        # Caps are read here, not at import, so a caller (or a test) can lower them.
        self.index_limit = MAX_INDEX_TOTAL if index_limit is None \
            else min(index_limit, MAX_INDEX_TOTAL)
        self.path_limit = min(MAX_PATH_TOTAL, self.index_limit)
        self.read = self.decoded = 0  # index bytes read from the file / decoded, so far
        self.path_chars = 0  # characters of all joined folder/name paths, so far

    def control_block(self, method_text: str, pos: int, compsize: int, origsize: int,
                      crc: int) -> _Buf:
        """Read, decode (a single storing or lzma only) and CRC-check one index block."""
        method = parse_method(method_text)
        if method.encrypted:
            raise FarcError("the archive's own index is encrypted; gitfurl does not handle "
                            "encrypted archives")
        if not method.single:
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
        if len(data) != origsize:
            raise FarcError("archive structure damaged (index failed its CRC check)")
        self.check_crc("index", crc, zlib.crc32(data))
        return _Buf(data)


    def check_crc(self, what: str, stored: int, computed: int) -> None:
        if stored == computed:
            return
        if not self.accept_bad_index_crc:
            raise FarcError(f"archive structure damaged ({what} failed its CRC check); "
                            "use --accept-bad-index-crc to read it anyway")
        self.warnings.append(
            f"The archive's {what} failed its CRC check "
            f"(stored {stored:08X}, computed {computed:08X}). "
            "You chose to read it anyway; each file is still checked against its own CRC, "
            "and a file that fails is not kept.")


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


def read_archive(path: Path, index_limit: int | None = None, *,
                 accept_bad_index_crc: bool = False) -> Archive:
    """Read the index of one archive. Raises FarcError (only) for anything it cannot read."""
    path = Path(path)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except (OSError, ValueError) as exc:
        reason = getattr(exc, "strerror", None) or "bad path"
        raise FarcError(f"the file could not be opened ({reason})") from exc
    try:
        return read_fd(fd, path, index_limit, accept_bad_index_crc=accept_bad_index_crc)
    finally:
        os.close(fd)


def read_fd(fd: int, path: Path, index_limit: int | None = None, *,
            accept_bad_index_crc: bool = False) -> Archive:
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
    return _read(_Reader(fd, st.st_size, index_limit,
                         accept_bad_index_crc=accept_bad_index_crc), Path(path))


def _footer_descriptor(tail: bytes, reader: _Reader | None = None
                       ) -> tuple[int, int, str, int, int, int]:
    """Find the footer's descriptor: the last signature in `tail` whose CRC-32 matches.

    With the opt-in, a parsed descriptor with a bad CRC is used only if none matches.
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
    if seen and reader is not None and reader.accept_bad_index_crc:
        # Prefer a CRC-valid descriptor exactly as before. Only if there is none, take
        # the last signature whose complete descriptor fields parse. Do not try another
        # candidate if reading its blocks later fails.
        end = len(tail) - 4
        while True:
            at = tail.rfind(SIGNATURE, 0, end)
            if at < 0:
                break
            d = tail[at:]
            try:
                b = _Buf(d[:-4])
                b.u32()
                fields = b.varint(), b.cstr(), b.varint(), b.varint(), b.u32()
                kind, _, orig, comp, _ = fields
                dpos = reader.size - len(tail) + at
                if b.pos != len(b.data) or kind != FOOTER_BLOCK or not 0 < comp <= dpos \
                        or orig <= 0:
                    raise FarcError("archive structure damaged (unexpected footer descriptor)")
            except FarcError:
                end = at + len(SIGNATURE) - 1
                continue
            reader.check_crc("footer descriptor", int.from_bytes(d[-4:], "little"),
                             zlib.crc32(d[:-4]))
            return (at, *fields)
    if seen:
        raise FarcError("archive structure damaged (footer descriptor failed its CRC check); "
                        "use --accept-bad-index-crc to read it anyway")
    raise FarcError("no FreeArc footer found at the end of the file")


def _read(r: _Reader, path: Path) -> Archive:
    size = r.size
    if size < 16:
        raise FarcError("too small to be a FreeArc archive")
    scan = min(size, FOOTER_SCAN)
    tail = _pread(r.fd, size, size - scan, scan)
    at, btype, method_text, origsize, compsize, crc = _footer_descriptor(tail, r)
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
    arc.warnings = list(r.warnings)
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


def relative_parts(path: str) -> list[str] | None:
    """The folder parts of a stored name as a relative path, or None when it is not safe.

    "/" and "\\" both separate folders (an archive made on Windows may use either); empty
    and "." parts are dropped. None for an empty name, an absolute path, a ".." part, a
    drive letter, or a control character anywhere. An empty list means the name is only
    "." (the archive's own top folder).
    """
    if not path or path[0] in "/\\" or _CONTROL.search(path):
        return None
    parts = _SEP.split(path)
    if any(p == ".." or _DRIVE.match(p) for p in parts):
        return None
    return [p for p in parts if p not in ("", ".")]


def unsafe_name(path: str) -> bool:
    """True when a stored name could not be used under a destination folder.

    The same rule extraction uses (relative_parts). Nothing is extracted here; this only
    feeds the summary.
    """
    return relative_parts(path) is None


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
    kinds = {KIND_STORING: 0, KIND_LZMA: 0}
    other = 0
    for blk in arc.blocks:
        if blk.method.supported:
            kinds[blk.method.kind] = kinds.get(blk.method.kind, 0) + 1
        else:
            other += 1
    # The kinds are gitfurl's own stage names, never the archive's text. At most MAX_KINDS
    # chains are named besides storing and lzma; the rest are counted together.
    listed = list(kinds.items())
    parts = [f"{n:,} {kind}" for kind, n in listed[:2 + MAX_KINDS]]
    rest = sum(n for _, n in listed[2 + MAX_KINDS:])
    if rest:
        parts.append(f"{rest:,} with other chains of these methods")
    if other:
        parts.append(f"{other:,} {KIND_OTHER}")
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
    lines.extend(f"warning: {warning}" for warning in arc.warnings)
    return lines
