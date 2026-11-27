"""Read-only reader for Inno Setup installers, and extraction of Inno's own [Files] payload.

read_setup(path) reads what an Inno Setup setup.exe holds, without running it: the loader's
offset table (a PE resource) or, when setup.exe has none (UseSetupLdr=no), <stem>-0.bin beside
it, recognised by the 64-byte format ID at its start; the format ID, the two header blocks
(CRC-32 checked, LZMA1-compressed or stored), the setup header, every entry list ([Types],
[Components], [Tasks], [Dirs], [Files], [Icons], [INI], [Registry], [InstallDelete],
[UninstallDelete], [Run], [UninstallRun]), the file locations of the second block, and the
string constants of the compiled [Code] blob (a read-only walk; the code is never run or
decompiled).

Formats read: the data formats of Inno Setup 5.5.0 to 7.1.1, Unicode only (the format IDs
are listed in FORMATS), and the one-extra-byte 5.5.0 variant that some modified builds
write. Formats 5.5.7, 6.3.0, 6.7.0 and 7.1.1 were checked against setups built with the
official compilers; the others follow the specification only and are unverified.

Refused (InnoError, with a plain reason): anything that is not a regular file or not a
Windows program; no offset table; a damaged table, block, chunk or record (CRC-32, sizes,
counts, block ends); the ANSI edition of Inno Setup 5; development, unofficial and unknown
format IDs; setups whose header blocks are encrypted (Encryption=full); a layout Inno's
compiler does not write (slice files with a compressed file location block from a loader, or a
second slice or more than one slice per disk without disk spanning); a <stem>-0.bin whose ID
or blocks do not check out.

Where the file data is: inside setup.exe from Offset1 (the loader build without spanning), or
in slice files beside the installer when Offset1 is 0 (disk spanning) or there is no loader.
Slice number S is <stem>-<S div P + 1>.bin, with a letter a..z after the number when P (slices
per disk) is above 1; the stem is setup.exe's own file name. A slice file starts with a header
(idska32 + 32-bit size up to data format 6.5.0, idskb32 + 64-bit size from 6.5.2) whose size
must equal the file's. A chunk starts at StartOffset in slice FirstSlice and runs to the end of
that file, then on from just after the header of each next slice, ending in LastSlice. Only the
installer's own folder is searched (names match without regard to letter case); the summary
lists the slice files, present or missing, by name only. Formats and layouts that were never
checked against a real installer are noted as unverified (slice and setup-0.bin reading was
checked for data formats 6.3.0, 6.7.0 and 7.1.1 only).

Extraction (plan_inno_extract, extract_inno_files) writes only the files stored in setup.exe
or its slice files whose destination is under {app}, for the default choice of setup type,
components, tasks and the first language; everything else (temporary helpers, files outside
{app}, external and downloaded files, the uninstaller, entries that depend on a [Code] check
function) is listed as skipped. Refused before anything is written: a missing, short or wrong
slice file (opened with O_NOFOLLOW; signature and size checked), a chunk that does not fit its
slice files exactly, a password-protected setup (even when its files are not encrypted),
encrypted files, a compression method gitfurl does not handle, an unsafe or Windows-unusable
name, a name that collides with a folder, an LZMA dictionary over 1 GiB, and the usual prefix
checks of extract.py. Files are created new (O_CREAT|O_EXCL|
O_NOFOLLOW) relative to the destination's own file descriptor; each file's SHA-1 or SHA-256
is checked, and a file that does not match (or was cut short by Stop or an error) is removed
again, so nothing half-made is kept. Slice files are checked again before writing and when a
chunk reaches them; a problem during the run is reported like other damaged data, and a
swapped slice of the right size is caught by the file's checksum. Nothing from the setup is
run, loaded or passed to a program; the helper DLLs inside the header block are skipped,
never written.

Written from a clean-room format specification.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import os
import re
import stat
import struct
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

# --- formats ---------------------------------------------------------------------------------

FORMATS = ("5.5.0", "5.5.6", "5.5.7", "6.0.0", "6.1.0", "6.3.0", "6.4.0.1", "6.4.2", "6.4.3",
           "6.5.0", "6.5.2", "6.6.0", "6.6.1", "6.7.0", "7.0.0.1", "7.0.0.3", "7.1.1")
_F = {name: i for i, name in enumerate(FORMATS)}
F550, F556, F557, F600, F610, F630, F6401, F642, F643, F650, F652, F660, F661, F670, \
    F7001, F7003, F711 = range(len(FORMATS))
VERIFIED_FORMATS = frozenset(["5.5.0", "5.5.7", "6.3.0", "6.7.0", "7.1.1"])  # 5.5.0: variant
# Slice files and setup-0.bin were checked against setups built with the official compilers
# (6.3.3, 6.7.4, 7.1.1) on 2026-11-23; other formats follow the specification only.
SLICES_VERIFIED_FORMATS = frozenset(["6.3.0", "6.7.0", "7.1.1"])

_ID_PREFIX = b"Inno Setup Setup Data ("
_IDS = {(f"Inno Setup Setup Data ({v})" + (" (u)" if _F[v] <= F610 else "")).encode(): v
        for v in FORMATS}
_ANSI = {b"Inno Setup Setup Data (5.5.0)", b"Inno Setup Setup Data (5.5.6)",
         b"Inno Setup Setup Data (5.5.7)"}
_DEV = {b"Inno Setup Setup Data (6.4.0)", b"Inno Setup Setup Data (5.6.0) (u)",
        b"Inno Setup Setup Data (5.6.1) (u)", b"Inno Setup Setup Data (5.6.2) (u)",
        b"Inno Setup Setup Data (6.3.0) (u)", b"Inno Setup Setup Data (6.6.2)",
        b"Inno Setup Setup Data (7.1.0)", b"Inno Setup Setup Data (7.2.0)"}
_UNOFFICIAL = {b"Inno Setup Setup Data (5.5.7) (U)", b"Inno Setup Setup Data (5.5.8) (u)"}

TABLE_MAGIC = bytes.fromhex("72446c507453cde6d77b0b2a")
CHUNK_MAGIC = b"zlb\x1a"
SLICE_SIG_OLD = b"idska32\x1a"  # slice file header, data formats 5.5.0 to 6.5.0 (32-bit size)
SLICE_SIG_NEW = b"idskb32\x1a"  # data formats 6.5.2 and later (64-bit size)
COMPRESSION = {0: "stored", 1: "zlib", 2: "bzip2", 3: "lzma", 4: "lzma2"}
METHOD_OTHER = "a method gitfurl does not handle"
PRIVILEGES = {0: "none", 1: "power user", 2: "admin", 3: "lowest"}

# --- limits ----------------------------------------------------------------------------------

MAX_FILE = 64 << 30  # setup.exe size
MAX_BLOCK1 = 128 << 20  # decoded bytes of block 1
MAX_BLOCK2 = 64 << 20  # decoded bytes of block 2
MAX_STRING = 1 << 20  # bytes of one text string
MAX_BYTESTR = 64 << 20  # bytes of one byte string
MAX_COUNT = 1_000_000  # entries in one list
MAX_ENTRIES = 300_000  # entries in all lists together, file locations included
MAX_CODE_CANDIDATES = 1_000_000  # possible string operands looked at in the [Code]
MAX_DECODE_RATIO = 2000  # decoded bytes thrown away per compressed byte of a chunk (see plan)
MAX_SUBFOLDERS = 2  # folder levels below the installer's folder the preflight looks into
MAX_SLICES_PER_DISK = 26  # slice files of one disk are lettered a to z
MAX_SLICES = 4096  # slice files one setup may name (the plan checks each one)
MAX_SECTIONS = 96
MAX_RES_ENTRIES = 4096
MAX_RESOURCE = 16 << 20  # bytes of the resource section that are read
DICT_CAP = 1 << 30  # LZMA/LZMA2 dictionary
MAX_CODE_COUNT = 100_000
MAX_CODE_STRINGS = 10_000
MAX_DIR_ENTRIES = 10_000  # names looked at in the installer's folder
MAX_SIBLINGS = 999
CHUNK = 1 << 20  # bytes read or decoded at a time (also how often Stop is checked)
FILTER_BLOCK = 1 << 16  # the call filter's block size
SHOW = 80  # characters of one name shown in the plan
LISTED = 12  # entries of one kind listed by name in the plan

REDIST_DIRS = frozenset(["_redist", "redist", "_commonredist", "commonredist", "directx",
                         "redistributables", "prerequisites", "_prerequisites"])
REDIST_PREFIXES = ("vcredist", "vc_redist", "dxsetup", "dxwebsetup", "directx", "dotnet",
                   "ndp4", "ndp", "netfx", "oalinst", "physx", "xnafx", "xna", "vcrun",
                   "windowsdesktop-runtime", "msxml", "ue4prereq", "ueprereq")


class InnoError(ValueError):
    """Not an Inno Setup installer gitfurl can read, refused, or damaged. Nothing was written."""


class _Damage(InnoError):
    """A check of the layout failed (used to try the next layout where the ID is ambiguous)."""


# --- records ---------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Conditions:
    components: str = ""
    tasks: str = ""
    languages: str = ""
    check: str = ""

    @property
    def conditional(self) -> bool:
        return bool(self.components or self.tasks or self.languages or self.check)


@dataclass(frozen=True, slots=True)
class Language:
    name: str
    language_id: int


@dataclass(frozen=True, slots=True)
class CustomMessage:
    name: str
    value: str
    language: int  # -1 = all


@dataclass(frozen=True, slots=True)
class SetupType:
    name: str
    description: str
    cond: Conditions
    custom: bool
    kind: int  # 0 user-defined, 1 default full, 2 default compact, 3 default custom


@dataclass(frozen=True, slots=True)
class Component:
    name: str
    description: str
    types: str
    cond: Conditions
    level: int
    options: int  # bit 0 fixed, 1 restart, 2 no-uninstall warning, 3 exclusive, 4 no inherit


@dataclass(frozen=True, slots=True)
class Task:
    name: str
    description: str
    cond: Conditions
    level: int
    options: int  # bit 0 exclusive, 1 unchecked, 2 restart, 3 checked once, 4 no inherit

    @property
    def unchecked(self) -> bool:
        return bool(self.options & 2)


@dataclass(frozen=True, slots=True)
class DirEntry:
    name: str
    cond: Conditions
    options: int


FILE_STORED, FILE_EXTERNAL, FILE_DOWNLOAD, FILE_UNINSTALLER = (
    "stored", "external", "download", "uninstaller")


@dataclass(frozen=True, slots=True)
class FileEntry:
    source: str
    dest: str
    cond: Conditions
    location: int  # index into Setup.locations, -1 for none
    external_size: int
    flags: frozenset  # names from FILE_FLAGS_*
    file_type: int  # 0 normal, 1 the uninstaller
    kind: str  # FILE_STORED, FILE_EXTERNAL, FILE_DOWNLOAD or FILE_UNINSTALLER


@dataclass(frozen=True, slots=True)
class Icon:
    name: str
    target: str
    parameters: str
    working_dir: str
    cond: Conditions


@dataclass(frozen=True, slots=True)
class IniEntry:
    file: str
    section: str
    key: str
    value: str
    cond: Conditions


ROOT_KEYS = {0x80000000: "HKCR", 0x80000001: "HKCU", 0x80000002: "HKLM", 0x80000003: "HKU",
             0x80000005: "HKCC", 1: "HKA"}


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    root: int
    subkey: str
    value_name: str
    value_data: str
    value_type: int
    cond: Conditions

    @property
    def root_name(self) -> str:
        return ROOT_KEYS.get(self.root & 0xFFFFFFFF, "an unknown root key")


@dataclass(frozen=True, slots=True)
class DeleteEntry:
    name: str
    kind: int  # 0 files, 1 files and/or subdirs, 2 dir if empty
    cond: Conditions


SHOW_COMMANDS = {0: "hidden", 1: "normal", 3: "maximized", 7: "minimized"}
WAITS = {0: "waits until it ends", 1: "does not wait", 2: "waits until it is idle"}


@dataclass(frozen=True, slots=True)
class RunEntry:
    filename: str
    parameters: str
    working_dir: str
    run_once_id: str
    status: str
    verb: str
    description: str
    cond: Conditions
    show: int
    wait: int  # 0 until terminated, 1 no wait, 2 until idle
    flags: frozenset  # names from RUN_FLAGS_*

    def silent_runs(self) -> bool | None:
        """Would a silent install run it? None when that depends on the user's choice."""
        if "skip if silent" in self.flags:
            return False
        if "post-install" in self.flags and "unchecked" in self.flags:
            return None
        return True


@dataclass(frozen=True, slots=True)
class Location:
    first_slice: int
    last_slice: int
    start: int  # chunk start, relative to setup-1 (or to the slice file)
    offset: int  # inside the decompressed chunk
    size: int
    chunk_size: int
    digest: bytes  # SHA-1 (20 bytes) or SHA-256 (32 bytes) of the original file
    filetime: int
    call_filter: bool
    encrypted: bool
    compressed: bool


@dataclass
class CodeInfo:
    present: bool = False
    walked: bool = False  # the container was understood (strings are checked constants)
    strings: list[str] = field(default_factory=list)
    imports: list[str] = field(default_factory=list)  # "dll name: function"
    note: str = ""


@dataclass
class Setup:
    path: Path
    size: int
    format: str
    format_id: str
    variant: bool
    table_version: int
    offset0: int
    offset1: int
    offset_exe: int
    encryption: int  # >= 6.5.0: 0 none, 1 files; older: 1 when the "encryption used" flag is set
    strings: dict
    privileges: int
    compression: int
    slices_per_disk: int
    password_required: bool
    code_version: int
    arch_allowed: str
    arch_64bit: str
    languages: list[Language] = field(default_factory=list)
    custom_messages: list[CustomMessage] = field(default_factory=list)
    types: list[SetupType] = field(default_factory=list)
    components: list[Component] = field(default_factory=list)
    tasks: list[Task] = field(default_factory=list)
    dirs: list[DirEntry] = field(default_factory=list)
    files: list[FileEntry] = field(default_factory=list)
    icons: list[Icon] = field(default_factory=list)
    ini: list[IniEntry] = field(default_factory=list)
    registry: list[RegistryEntry] = field(default_factory=list)
    install_delete: list[DeleteEntry] = field(default_factory=list)
    uninstall_delete: list[DeleteEntry] = field(default_factory=list)
    run: list[RunEntry] = field(default_factory=list)
    uninstall_run: list[RunEntry] = field(default_factory=list)
    locations: list[Location] = field(default_factory=list)
    code: CodeInfo = field(default_factory=CodeInfo)
    notes: list[str] = field(default_factory=list)
    data_file: Path | None = None  # setup-0.bin beside setup.exe (UseSetupLdr=no), else None
    data_size: int = 0  # its size
    spanning: bool = False  # disk spanning: the file data may run over several slice files

    @property
    def app_name(self) -> str:
        return self.strings.get("app name", "")

    @property
    def app_version(self) -> str:
        return self.strings.get("app version", "")

    @property
    def default_dir(self) -> str:
        return self.strings.get("default dir name", "")

    @property
    def loader(self) -> bool:
        """setup.exe is Inno's loader, holding the offset table and the setup data."""
        return self.data_file is None

    @property
    def slice_layout(self) -> bool:
        """The file data is in slice files beside setup.exe, not inside it."""
        return self.offset1 == 0

    @property
    def disk_spanning(self) -> bool:
        return self.spanning

    @property
    def where(self) -> str:
        """Where the setup data was read, for detect()."""
        if self.data_file is None:
            return f"byte offset {self.offset0}"
        return f"setup data in {_clean(self.data_file.name, 60)} beside the installer"

    @property
    def compression_name(self) -> str:
        return COMPRESSION.get(self.compression, METHOD_OTHER)

    @property
    def verified(self) -> bool:
        return self.format in VERIFIED_FORMATS and (self.format != "5.5.0" or self.variant)


# --- byte reading ----------------------------------------------------------------------------

class _Buf:
    """Bounded little-endian reads; every failure raises the given error class."""

    __slots__ = ("data", "end", "err", "pos")

    def __init__(self, data, pos: int = 0, end: int | None = None, err=_Damage):
        self.data = data
        self.pos = pos
        self.end = len(data) if end is None else end
        self.err = err

    def left(self) -> int:
        return self.end - self.pos

    def need(self, n: int, what: str = "a record") -> None:
        if n < 0 or self.end - self.pos < n:
            raise self.err(f"{what} runs past the end of its data")

    def skip(self, n: int, what: str = "a record") -> None:
        self.need(n, what)
        self.pos += n

    def take(self, n: int, what: str = "a record") -> bytes:
        self.need(n, what)
        b = bytes(self.data[self.pos:self.pos + n])
        self.pos += n
        return b

    def _unpack(self, fmt: str, n: int):
        self.need(n)
        v = struct.unpack_from(fmt, self.data, self.pos)[0]
        self.pos += n
        return v

    def u8(self) -> int:
        return self._unpack("<B", 1)

    def u16(self) -> int:
        return self._unpack("<H", 2)

    def i16(self) -> int:
        return self._unpack("<h", 2)

    def u32(self) -> int:
        return self._unpack("<I", 4)

    def i32(self) -> int:
        return self._unpack("<i", 4)

    def i64(self) -> int:
        return self._unpack("<q", 8)

    def u64(self) -> int:
        return self._unpack("<Q", 8)

    def text(self) -> str:
        n = self.u32()
        if n % 2:
            raise self.err("a text string has an odd length")
        if n > MAX_STRING:
            raise self.err("a text string is longer than 1 MiB")
        self.need(n, "a text string")
        s = bytes(self.data[self.pos:self.pos + n]).decode("utf-16-le", "replace")
        self.pos += n
        return s

    def blob(self, keep: bool = False) -> bytes:
        n = self.u32()
        if n > MAX_BYTESTR:
            raise self.err("a byte string is longer than 64 MiB")
        self.need(n, "a byte string")
        b = bytes(self.data[self.pos:self.pos + n]) if keep else b""
        self.pos += n
        return b


def _pread(fd: int, n: int, pos: int) -> bytes:
    out = bytearray()
    while len(out) < n:
        got = os.pread(fd, n - len(out), pos + len(out))
        if not got:
            break
        out += got
    return bytes(out)


def _bits(raw: bytes) -> int:
    return int.from_bytes(raw, "little")


# --- loader: PE resource and offset table ----------------------------------------------------

def _find_table(fd: int, size: int) -> tuple[int, int] | None:
    """(file offset, size) of the RCDATA 11111 resource, or None when the program has none."""
    head = _pread(fd, 64, 0)
    if len(head) < 64 or head[:2] != b"MZ":
        raise InnoError("not a Windows program (no 'MZ' header)")
    e = struct.unpack_from("<I", head, 0x3C)[0]
    if e + 24 > size:
        raise InnoError("the Windows program header lies outside the file")
    pe = _pread(fd, 24, e)
    if pe[:4] != b"PE\0\0":
        raise InnoError("not a Windows program (no 'PE' header)")
    nsec, = struct.unpack_from("<H", pe, 6)
    optsize, = struct.unpack_from("<H", pe, 20)
    if nsec > MAX_SECTIONS:
        raise InnoError("the Windows program header lists too many sections")
    opt = _pread(fd, optsize, e + 24)
    if len(opt) < optsize or optsize < 2:
        raise InnoError("the Windows program header is cut short")
    magic, = struct.unpack_from("<H", opt, 0)
    if magic not in (0x10B, 0x20B):
        raise InnoError("the Windows program header has an unknown kind")
    dd = 96 if magic == 0x10B else 112
    if optsize < dd + 24:
        return None
    rva, rsize = struct.unpack_from("<II", opt, dd + 16)
    secs = _pread(fd, 40 * nsec, e + 24 + optsize)
    if len(secs) < 40 * nsec:
        raise InnoError("the Windows program's section table is cut short")
    sections = [struct.unpack_from("<IIII", secs, 40 * i + 8) for i in range(nsec)]

    def to_off(r: int) -> tuple[int, int]:
        """File offset of an RVA and the raw bytes left in its section."""
        for vsize, va, rawsize, rawptr in sections:
            if va <= r < va + max(vsize, rawsize) and r - va < rawsize:
                off = rawptr + r - va
                if off >= size:
                    break
                return off, min(rawsize - (r - va), size - off)
        raise InnoError("a resource address lies outside the Windows program's sections")

    if not rva or not rsize:
        return None
    base, avail = to_off(rva)
    res = _pread(fd, min(rsize, avail, MAX_RESOURCE), base)

    def entries(off: int) -> list[tuple[int, int]]:
        if off + 16 > len(res):
            raise InnoError("the resource directory is cut short")
        named, ids = struct.unpack_from("<HH", res, off + 12)
        n = named + ids
        if n > MAX_RES_ENTRIES or off + 16 + 8 * n > len(res):
            raise InnoError("the resource directory is damaged")
        return [struct.unpack_from("<II", res, off + 16 + 8 * i) for i in range(n)]

    off = 0
    for depth, want in enumerate((10, 11111, None)):
        found = None
        for name, target in entries(off):
            if want is None or (not name & 0x80000000 and name == want):
                found = target
                break
        if found is None:
            return None
        last = depth == 2
        if bool(found & 0x80000000) == last:
            raise InnoError("the resource directory is damaged")
        off = found & 0x7FFFFFFF
    if off + 16 > len(res):
        raise InnoError("the resource data entry is cut short")
    drva, dsize = struct.unpack_from("<II", res, off)
    doff, davail = to_off(drva)
    if dsize > davail:
        raise InnoError("the offset table lies outside the file")
    return doff, dsize


@dataclass(frozen=True, slots=True)
class _Table:
    version: int
    min_size: int
    offset_exe: int
    offset0: int
    offset1: int


def _read_table(fd: int, size: int, off: int, tsize: int) -> _Table:
    if tsize not in (44, 64):
        raise InnoError("the setup's offset table has an unknown size")
    raw = _pread(fd, tsize, off)
    if len(raw) < tsize or raw[:12] != TABLE_MAGIC:
        raise InnoError("the setup's offset table has the wrong magic bytes")
    version, = struct.unpack_from("<I", raw, 12)
    if (version, tsize) not in ((1, 44), (2, 64)):
        raise InnoError("the setup's offset table has an unknown version")
    if zlib.crc32(raw[:-4]) != struct.unpack_from("<I", raw, tsize - 4)[0]:
        raise InnoError("the setup's offset table is damaged (CRC-32 mismatch)")
    if version == 1:
        min_size, exe, _, _, o0, o1 = struct.unpack_from("<IIIIII", raw, 16)
    else:
        min_size, exe = struct.unpack_from("<qq", raw, 16)
        o0, o1 = struct.unpack_from("<qq", raw, 40)
    if not (min_size <= size and 0 <= o1 <= o0 < exe <= min_size and (o1 == 0 or o1 >= 64)):
        raise InnoError("the setup's offset table points outside the file or out of order")
    return _Table(version, min_size, exe, o0, o1)


def _format_of(raw: bytes) -> str:
    """The format code for a 64-byte ID, or InnoError with the reason to show."""
    text = raw.rstrip(b"\0")
    if b"\0" not in text and text in _IDS:
        return _IDS[text]
    if not raw.startswith(_ID_PREFIX):
        raise InnoError("not an Inno Setup format gitfurl knows")
    if text in _ANSI:
        raise InnoError("built with the ANSI edition of Inno Setup 5, which gitfurl does not "
                        "read")
    if text in _DEV:
        raise InnoError("a development build of Inno Setup that gitfurl does not know")
    if text in _UNOFFICIAL:
        raise InnoError("a modified build of Inno Setup (unofficial format ID) that gitfurl "
                        "does not read")
    raise InnoError("not an Inno Setup format gitfurl knows")


# --- blocks ----------------------------------------------------------------------------------

class _LzmaSink:
    """Decodes a block's LZMA1 content as it arrives; the output is capped."""

    def __init__(self, cap: int, what: str):
        self.cap = cap
        self.what = what
        self.head = b""
        self.dec = None
        self.out = bytearray()

    def feed(self, data: bytes) -> None:
        if self.dec is None:
            self.head += data
            if len(self.head) < 5:
                return
            p = self.head[0]
            if p >= 225:
                raise _Damage(f"{self.what}: the LZMA properties byte is invalid")
            dsize, = struct.unpack_from("<I", self.head, 1)
            if dsize > DICT_CAP:
                raise _Damage(f"{self.what}: the LZMA dictionary is larger than 1 GiB")
            dsize = max(4096, min(dsize, -(-self.cap // 4096) * 4096))
            try:
                self.dec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{
                    "id": lzma.FILTER_LZMA1, "dict_size": dsize, "lc": p % 9,
                    "lp": (p // 9) % 5, "pb": p // 45}])
            except (lzma.LZMAError, ValueError) as exc:
                raise _Damage(f"{self.what}: the LZMA properties are not usable") from exc
            data, self.head = self.head[5:], b""
        while not self.dec.eof:
            try:
                got = self.dec.decompress(data, max_length=self.cap - len(self.out) + 1)
            except lzma.LZMAError as exc:
                raise _Damage(f"{self.what}: the LZMA data is damaged") from exc
            self.out += got
            if len(self.out) > self.cap:
                raise InnoError(f"{self.what} decodes to more than {self.cap >> 20} MiB")
            data = b""
            if self.dec.needs_input:
                break

    def result(self) -> bytes:
        if self.dec is None:
            raise _Damage(f"{self.what}: the LZMA data is cut short")
        return bytes(self.out)


def _read_block(fd: int, pos: int, size: int, fmt: int, cap: int, what: str) -> tuple[bytes, int]:
    """(content, end position) of one block at pos. Every CRC-32 is checked."""
    content, end, _ = _block(fd, pos, size, fmt, cap, what)
    return content, end


def _block(fd: int, pos: int, size: int, fmt: int, cap: int, what: str
           ) -> tuple[bytes, int, bool]:
    """As _read_block, and whether the block was LZMA-compressed (False: stored)."""
    hlen = 9 if fmt >= F670 else 5
    raw = _pread(fd, 4 + hlen, pos)
    if len(raw) < 4 + hlen:
        raise InnoError(f"{what} is cut short")
    if zlib.crc32(raw[4:]) != struct.unpack_from("<I", raw, 0)[0]:
        raise InnoError(f"{what} is damaged (header CRC-32 mismatch)")
    stored = struct.unpack_from("<q" if hlen == 9 else "<I", raw, 4)[0]
    packed = raw[-1]
    if packed not in (0, 1):
        raise InnoError(f"{what} has an unknown compression flag")
    start = pos + 4 + hlen
    end = start + stored
    if stored < 0 or end > size:
        raise InnoError(f"{what} runs past the end of the file")
    if stored > cap + cap // 1024 + 4096:
        raise InnoError(f"{what} is larger than {cap >> 20} MiB")
    sink = _LzmaSink(cap, what) if packed else None
    plain = bytearray()
    p = start
    while p < end:
        piece = _pread(fd, min(end - p, 4100 * 256), p)
        if not piece:
            raise InnoError(f"{what} could not be read")
        q = 0
        while q < len(piece):
            left = end - (p + q)
            if left < 5:
                raise InnoError(f"{what} is damaged (a chunk is cut short)")
            n = min(4096, left - 4)
            if q + 4 + n > len(piece):
                break
            data = piece[q + 4:q + 4 + n]
            if zlib.crc32(data) != struct.unpack_from("<I", piece, q)[0]:
                raise InnoError(f"{what} is damaged (chunk CRC-32 mismatch)")
            if sink is not None:
                sink.feed(data)
            else:
                plain += data
                if len(plain) > cap:
                    raise InnoError(f"{what} is larger than {cap >> 20} MiB")
            q += 4 + n
        if q == 0:
            raise InnoError(f"{what} could not be read")
        p += q
    return (sink.result() if sink is not None else bytes(plain)), end, bool(packed)


# --- block 1: header and entry lists ---------------------------------------------------------

_HEADER_STRINGS = (
    "app name", "app version name", "app id", "copyright", "publisher", "publisher url",
    "support phone", "support url", "updates url", "app version", "default dir name",
    "default group name", "output base file name", "uninstall files dir",
    "uninstall display name", "uninstall display icon", "app mutex", "default user name",
    "default organisation", "default serial", "readme file", "contact", "comments",
    "modify path", "create uninstall registry key", "uninstallable",
    "close applications filter", "setup mutex", "changes environment",
    "changes associations", "architectures allowed", "architectures in 64-bit mode",
    "close applications filter excludes", "7-zip library", "use previous app dir",
    "use previous group", "use previous setup type", "use previous tasks",
    "use previous user info")
_LIST_NAMES = ("languages", "custom messages", "permissions", "types", "components", "tasks",
               "dirs", "issig keys", "files", "file locations", "icons", "ini", "registry",
               "install delete", "uninstall delete", "run", "uninstall run")


def _header_string_count(f: int) -> int:
    if f == F550:
        return 27
    if f <= F557:
        return 28
    if f <= F610:
        return 30
    if f <= F6401:
        return 32
    if f <= F643:
        return 33
    if f <= F661:
        return 34
    if f <= F7003:
        return 39
    return 38


def _wizard_bytes(f: int) -> int:
    if f <= F556:
        return 40
    if f == F557:
        return 37
    if f <= F630:
        return 46
    if f <= F643:
        return 58
    return {F650: 10, F652: 18, F660: 26, F661: 27}.get(f, 37)


def _header_flags(f: int) -> tuple[int, int, int, int | None]:
    """(bytes stored, flags in use, password-required bit, encryption-used bit or None)."""
    if f <= F556:
        return 6, 46, 10, 37
    if f == F557:
        return 6, 47, 10, 37
    if f <= F610:
        return 6, 48, 10, 36
    if f == F630:
        return 7, 49, 10, 36
    if f <= F643:
        return 6, 44, 6, 31
    if f <= F652:
        return 6, 43, 6, None
    if f <= F661:
        return 6, 46, 6, None
    return 8, 42, 6, None


_FILE_FLAGS_COMMON = (
    "confirm overwrite", "uninstall never uninstall", "restart replace",
    "delete after install", "register server", "register type library", "shared file",
    "compare timestamp", "font is not TrueType", "skip if source does not exist",
    "overwrite read-only", "overwrite same version", "custom destination name",
    "only if destination file exists", "no register error", "uninstall restart delete",
    "only if does not exist", "ignore version", "prompt if older", "do not copy",
    "uninstall remove read-only", "recurse subdirs", "replace same version if contents differ",
    "do not verify checksum", "uninstall no shared-file prompt", "create all subdirs")
FILE_FLAGS_OLD = _FILE_FLAGS_COMMON + (
    "32-bit", "64-bit", "external size preset", "set NTFS compression",
    "unset NTFS compression", "GAC install", "download", "extract archive")
FILE_FLAGS_NEW = _FILE_FLAGS_COMMON + (
    "external size preset", "set NTFS compression", "unset NTFS compression", "GAC install",
    "download", "extract archive")
_RUN_FLAGS_COMMON = ("shell exec", "skip if does not exist", "post-install", "unchecked",
                     "skip if silent", "skip if not silent", "hide wizard")
RUN_FLAGS_OLD = _RUN_FLAGS_COMMON + ("32-bit", "64-bit", "run as original user",
                                     "do not log parameters", "log output")
RUN_FLAGS_NEW = _RUN_FLAGS_COMMON + ("run as original user", "do not log parameters",
                                     "log output")


def _names(value: int, names: tuple[str, ...], known: int) -> tuple[frozenset, int]:
    """(set flag names, unknown set bits) of a bit set."""
    out = {names[i] for i in range(min(known, len(names))) if value >> i & 1}
    return frozenset(out), value >> known


class _Parser:
    """Parses block 1 for one format and layout. Raises _Damage on any failed check."""

    def __init__(self, data: bytes, f: int, variant: bool, strict: bool):
        self.b = _Buf(data)
        self.f = f
        self.variant = variant
        self.strict = strict  # the ID is ambiguous: oddities count as a failed parse
        self.notes: list[str] = []

    def odd(self, text: str) -> None:
        if self.strict:
            raise _Damage(text)
        if len(self.notes) < 20 and text not in self.notes:
            self.notes.append(text)

    def strings(self, n: int) -> list[str]:
        return [self.b.text() for _ in range(n)]

    def ver(self) -> None:
        self.b.skip(10)  # one Windows version: u32, u32, u16

    def common(self, s: list[str], at: int) -> Conditions:
        return Conditions(s[at], s[at + 1], s[at + 2], s[at + 3])

    def header(self) -> dict:
        b, f = self.b, self.f
        s = self.strings(_header_string_count(f))
        if f >= F711:
            s.insert(12, "")
        strings = {name: (s[i] if i < len(s) else "") for i, name in enumerate(_HEADER_STRINGS)}
        b.blob()
        b.blob()
        b.blob()
        code = b.blob(keep=True)
        counts = {}
        for name in _LIST_NAMES:
            if name == "issig keys" and f < F650:
                counts[name] = 0
                continue
            n = b.i32()
            if not 0 <= n <= MAX_COUNT:
                raise _Damage(f"the setup header has an impossible {name} count")
            counts[name] = n
        if sum(counts.values()) > MAX_ENTRIES:
            raise InnoError(f"the setup has more than {MAX_ENTRIES:,} entries in its lists; "
                            "gitfurl does not read setups this large")
        code_version = b.u32() if f >= F7003 else 0
        self.ver()
        self.ver()
        b.skip(_wizard_bytes(f))
        b.i64()  # extra disk space
        slices = b.i32()
        log_mode, dir_warn, privileges = b.u8(), b.u8(), b.u8()
        if f >= F600:
            b.u8()  # allowed privilege overrides
        lang_dialog, lang_detect, compression = b.u8(), b.u8(), b.u8()
        arch = (b.u8(), b.u8()) if f <= F610 else None
        dir_page, group_page = b.u8(), b.u8()
        b.i64()  # uninstall display size
        if self.variant:
            b.u8()
        nbytes, used, pw_bit, enc_bit = _header_flags(f)
        flags = _bits(b.take(nbytes))
        if not 1 <= slices <= MAX_SLICES_PER_DISK:
            self.odd("the setup header has an impossible slices-per-disk value")
        if log_mode > 2 or dir_warn > 2 or privileges > 3 or lang_dialog > 2 or lang_detect > 2 \
                or dir_page > 2 or group_page > 2:
            self.odd("the setup header has a value out of its range")
        if compression not in COMPRESSION:
            self.odd(f"the setup header's file compression value names {METHOD_OTHER}")
        if flags >> used:
            self.odd("the setup header has flags set that gitfurl does not know")
        if arch is not None and (arch[0] | arch[1]) & 0xE0:
            self.odd("the setup header has architecture bits that gitfurl does not know")
        return {"strings": strings, "code": code, "counts": counts,
                "code_version": code_version, "slices": slices, "privileges": privileges,
                "compression": compression, "arch": arch,
                "password": bool(flags >> pw_bit & 1),
                "encryption_used": bool(enc_bit is not None and flags >> enc_bit & 1)}

    def count_ok(self, n: int, minimum: int, name: str) -> None:
        if n * minimum > self.b.left():
            raise _Damage(f"the {name} list is longer than the data that is left")

    def entries(self, h: dict) -> dict:
        b, f = self.b, self.f
        c = h["counts"]
        out: dict = {}

        def rec(t: int, nb: int = 0) -> list[str]:
            s = self.strings(t)
            for _ in range(nb):
                b.blob()
            return s

        def lst(name: str, t: int, nb: int, fixed: int, parse) -> list:
            n = c[name]
            self.count_ok(n, 4 * (t + nb) + fixed, name)
            return [parse() for _ in range(n)]

        def perm_ok(i: int) -> None:
            if not -1 <= i < c["permissions"]:
                self.odd("an entry points to a permission that does not exist")

        def language():
            s = rec(6 if f <= F652 else 4, 4)
            lid = b.u32() if f <= F652 else b.u16()
            b.skip(17)
            return Language(s[0], lid)

        def custom_message():
            s = rec(2)
            idx = b.i32()
            if not -1 <= idx < c["languages"]:
                self.odd("a custom message points to a language that does not exist")
            return CustomMessage(s[0], s[1], idx)

        def permission():
            rec(0, 1)

        def setup_type():
            s = rec(4)
            self.ver()
            self.ver()
            opts, kind = b.u8(), b.u8()
            b.i64()
            if kind > 3 or opts > 1:
                self.odd("a setup type has values gitfurl does not know")
            return SetupType(s[0], s[1], Conditions("", "", s[2], s[3]), bool(opts & 1), kind)

        def component():
            s = rec(5)
            b.i64()
            level = b.i32() if f < F670 else b.u8()
            b.u8()
            self.ver()
            self.ver()
            opts = b.u8()
            b.i64()
            if opts >> 5:
                self.odd("a component has options gitfurl does not know")
            return Component(s[0], s[1], s[2], Conditions("", "", s[3], s[4]), level, opts)

        def task():
            s = rec(6)
            level = b.i32() if f < F670 else b.u8()
            b.u8()
            self.ver()
            self.ver()
            opts = b.u8()
            if opts >> 5:
                self.odd("a task has options gitfurl does not know")
            return Task(s[0], s[1], Conditions(s[3], "", s[4], s[5]), level, opts)

        def dir_entry():
            s = rec(7)
            b.i32()
            self.ver()
            self.ver()
            perm_ok(b.i16())
            opts = b.u8()
            if opts >> 5:
                self.odd("a [Dirs] entry has options gitfurl does not know")
            return DirEntry(s[0], self.common(s, 1), opts)

        def issig():
            rec(3)

        file_flags = FILE_FLAGS_NEW if f >= F7003 else FILE_FLAGS_OLD
        file_known = 32 if f <= F643 or f >= F7003 else 34

        def file_entry():
            s = rec(10 if f <= F643 else 15, 0 if f <= F643 else 1)
            if f >= F650:
                b.skip(32)
                if b.u8() > 2:
                    self.odd("a [Files] entry has an unknown verification kind")
            self.ver()
            self.ver()
            loc = b.i32()
            b.i32()  # attributes
            ext_size = b.i64()
            perm_ok(b.i16())
            if f >= F7003 and b.u8() > 4:
                self.odd("a [Files] entry has an unknown bitness")
            nopt = 4 if f <= F643 else 5 if f <= F661 else 8
            flags, unknown = _names(_bits(b.take(nopt)), file_flags, file_known)
            ftype = b.u8()
            if unknown:
                self.odd("a [Files] entry has options gitfurl does not know")
            if ftype > 1:
                self.odd("a [Files] entry has an unknown file type")
            if not -1 <= loc < c["file locations"]:
                raise _Damage("a [Files] entry points to a file location that does not exist")
            if ftype == 1:
                kind = FILE_UNINSTALLER
            elif "download" in flags:
                kind = FILE_DOWNLOAD
            elif loc == -1:
                kind = FILE_EXTERNAL
            else:
                kind = FILE_STORED
            return FileEntry(s[0], s[1], self.common(s, 4), loc, ext_size, flags, ftype, kind)

        def icon():
            s = rec(13)
            if f >= F610:
                b.skip(16)
            self.ver()
            self.ver()
            b.skip(4 + 4 + 1 + 2 + 1)
            return Icon(s[0], s[1], s[2], s[3], self.common(s, 6))

        def ini():
            s = rec(10)
            self.ver()
            self.ver()
            b.u8()
            return IniEntry(s[0], s[1], s[2], s[3], self.common(s, 4))

        def registry():
            s = rec(9)
            self.ver()
            self.ver()
            root = b.u32()
            perm_ok(b.i16())
            vtype = b.u8()
            if f >= F7003 and b.u8() > 4:
                self.odd("a [Registry] entry has an unknown bitness")
            b.u16()
            if vtype > 6:
                self.odd("a [Registry] entry has an unknown value type")
            return RegistryEntry(root, s[0], s[1], s[2], vtype, self.common(s, 3))

        def delete():
            s = rec(7)
            self.ver()
            self.ver()
            kind = b.u8()
            if kind > 2:
                self.odd("a delete entry has an unknown kind")
            return DeleteEntry(s[0], kind, self.common(s, 1))

        run_flags = RUN_FLAGS_NEW if f >= F7003 else RUN_FLAGS_OLD
        run_known = 10 if f >= F7003 else 12 if f >= F630 else 11 if f >= F610 else 10

        def run():
            s = rec(14 if f >= F7001 else 13)
            self.ver()
            self.ver()
            show = b.i32()
            wait = b.u8()
            if f >= F7003 and b.u8() > 4:
                self.odd("a [Run] entry has an unknown bitness")
            flags, unknown = _names(b.u16(), run_flags, run_known)
            if wait > 2 or unknown:
                self.odd("a [Run] entry has values gitfurl does not know")
            return RunEntry(s[0], s[1], s[2], s[3], s[4], s[5], s[6], self.common(s, 7), show,
                            wait, flags)

        lang_fixed = 21 if f <= F652 else 19
        out["languages"] = lst("languages", 6 if f <= F652 else 4, 4, lang_fixed, language)
        out["custom messages"] = lst("custom messages", 2, 0, 4, custom_message)
        lst("permissions", 0, 1, 0, permission)
        out["types"] = lst("types", 4, 0, 30, setup_type)
        out["components"] = lst("components", 5, 0, 42 if f < F670 else 39, component)
        out["tasks"] = lst("tasks", 6, 0, 26 if f < F670 else 23, task)
        out["dirs"] = lst("dirs", 7, 0, 27, dir_entry)
        if f >= F650:
            lst("issig keys", 3, 0, 0, issig)
        file_fixed = 43 if f <= F643 else 77 if f <= F661 else 80 if f <= F7001 else 81
        out["files"] = lst("files", 10 if f <= F643 else 15, 0 if f <= F643 else 1, file_fixed,
                           file_entry)
        out["icons"] = lst("icons", 13, 0, 48 if f >= F610 else 32, icon)
        out["ini"] = lst("ini", 10, 0, 21, ini)
        out["registry"] = lst("registry", 9, 0, 30 if f >= F7003 else 29, registry)
        out["install delete"] = lst("install delete", 7, 0, 21, delete)
        out["uninstall delete"] = lst("uninstall delete", 7, 0, 21, delete)
        run_t, run_fixed = (14 if f >= F7001 else 13), (28 if f >= F7003 else 27)
        out["run"] = lst("run", run_t, 0, run_fixed, run)
        out["uninstall run"] = lst("uninstall run", run_t, 0, run_fixed, run)
        return out

    def tail(self, h: dict, two_images: bool, dec_dll: bool) -> None:
        """Wizard images and helper DLLs after the entry lists; must end the block exactly."""
        b, f = self.b, self.f

        def item():
            n = b.u32()
            if n >= 1 << 31:
                raise _Damage("an image or helper DLL in the setup header has an impossible size")
            b.skip(n, "an image or helper DLL")

        def image_list():
            n = b.i32()
            if n * 4 > b.left():
                raise _Damage("an image list is longer than the data that is left")
            for _ in range(max(n, 0)):
                item()

        if two_images:
            item()
            item()
        else:
            for _ in range(2 if f <= F652 else 4 if f <= F661 else 6):
                image_list()
        if h["compression"] in (1, 2):
            item()
        if dec_dll:
            item()
        if f >= F650 and h["strings"]["7-zip library"]:
            item()
        if b.pos != b.end:
            raise _Damage("the setup header block does not end where its contents end")


def _parse_block1(data: bytes, f: int, variant: bool, strict: bool):
    """(header, entries, notes) for one layout; raises _Damage when it does not fit exactly."""
    last: _Damage | None = None
    p = _Parser(data, f, variant, strict)
    h = p.header()
    entries = p.entries(h)
    mark = p.b.pos
    image_forms = [True] if f <= F556 else [True, False] if f == F557 else [False]
    if f <= F630 and h["encryption_used"]:
        dll_forms = [False, True] if variant else [True]
    else:
        dll_forms = [False]
    for two in image_forms:
        for dll in dll_forms:
            p.b.pos = mark
            try:
                p.tail(h, two, dll)
            except _Damage as exc:
                last = exc
                continue
            return h, entries, p.notes
    raise last or _Damage("the setup header block does not end where its contents end")


def _locations(data: bytes, f: int, count: int) -> list[Location]:
    size = 74 if f <= F610 else 75 if f == F630 else 87 if f <= F642 else 85 if f <= F650 else 89
    if len(data) != count * size:
        raise InnoError("the file location block does not hold exactly its entries")
    b = _Buf(data, err=InnoError)
    out = []
    hashlen = 20 if f <= F630 else 32
    for _ in range(count):
        first, last = b.i32(), b.i32()
        start = b.i64() if f >= F652 else b.i32()
        offset, osize, csize = b.i64(), b.i64(), b.i64()
        digest = b.take(hashlen)
        filetime = b.u64()
        b.u32()
        b.u32()
        if f <= F642:
            flags = b.u16()
            call, enc, comp, known = flags >> 4 & 1, flags >> 6 & 1, flags >> 7 & 1, 11
        else:
            flags = b.u8()
            call, enc, comp, known = flags >> 2 & 1, flags >> 3 & 1, flags >> 4 & 1, 5
        if F630 <= f <= F642:
            b.u8()  # sign mode
        if flags >> known:
            raise InnoError("a file location has flags gitfurl does not know")
        if min(first, last, start, offset, osize, csize) < 0 or first > last:
            raise InnoError("a file location has a negative or impossible value")
        out.append(Location(first, last, start, offset, osize, csize, digest, filetime,
                            bool(call), bool(enc), bool(comp)))
    return out


# --- [Code] ----------------------------------------------------------------------------------

class _CodeError(Exception):
    pass


_NO_EXTRA = frozenset([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 13, 14, 16, 17, 18, 19, 20, 24, 28, 29])
_ATTR_FIXED = {1: 1, 2: 1, 18: 1, 3: 2, 4: 2, 20: 2, 5: 4, 6: 4, 7: 4, 21: 4, 8: 8, 17: 8,
               24: 8, 29: 8, 9: 10}
_STRING_BASES = {10: 1, 14: 1, 19: 2, 28: 2}


def _printable(s: str) -> bool:
    return all(c.isprintable() or c in "\r\n\t" for c in s)


def _code_attrs(b: _Buf, types: list[tuple[int, int]]) -> None:
    n = b.u32()
    if n > MAX_CODE_COUNT:
        raise _CodeError
    for _ in range(n):
        b.skip(b.u32())
        fields = b.u32()
        if fields > MAX_CODE_COUNT:
            raise _CodeError
        for _ in range(fields):
            ti = b.u32()
            if ti >= len(types):
                raise _CodeError
            base, bits = types[ti]
            if base in _ATTR_FIXED:
                b.skip(_ATTR_FIXED[base])
            elif base in (10, 14):
                b.skip(b.u32())
            elif base in (19, 28):
                b.skip(2 * b.u32())
            elif base == 23:
                b.skip((bits + 7) // 8)
            else:
                raise _CodeError


def _walk_code(blob: bytes) -> CodeInfo:
    b = _Buf(blob, err=_CodeError)
    if b.u32() != 0x53504649:
        raise _CodeError
    build = b.u32()
    ntypes, nprocs, nvars = b.u32(), b.u32(), b.u32()
    b.u32()
    b.u32()
    if not 12 <= build <= 23 or max(ntypes, nprocs, nvars) > MAX_CODE_COUNT:
        raise _CodeError
    types: list[tuple[int, int]] = []

    def type_index() -> None:
        if b.u32() >= len(types):
            raise _CodeError

    for _ in range(ntypes):
        tb = b.u8()
        base, bits = tb & 0x7F, 0
        if base in _NO_EXTRA:
            pass
        elif base == 11:
            n = b.u32()
            if not 0 < n <= MAX_CODE_COUNT:
                raise _CodeError
            for _ in range(n):
                type_index()
        elif base == 12:
            type_index()
        elif base in (21, 25):
            n = b.u32()
            if n > 255:
                raise _CodeError
            b.skip(n)
        elif base == 22:
            type_index()
            b.skip(8)
        elif base == 23:
            bits = b.u32()
            if bits > 256:
                raise _CodeError
        elif base == 26:
            b.skip(16)
        else:
            raise _CodeError
        if tb & 0x80:
            b.skip(b.u32())
        if build >= 21:
            _code_attrs(b, types)
        types.append((base, bits))
    code_ranges: list[tuple[int, int]] = []
    imports: list[str] = []
    for _ in range(nprocs):
        fl = b.u8()
        if fl & 1:
            b.skip(b.u8())
            if fl & 2:
                decl = b.take(b.u32())
                if decl.startswith(b"dll:") and len(imports) < 1000:
                    parts = decl[4:].split(b"\0", 2)
                    if len(parts) >= 2:
                        dll = parts[0].decode("latin-1")
                        fn = parts[1].decode("latin-1")
                        imports.append(f"{dll}: {fn}")
        else:
            off, ln = b.u32(), b.u32()
            if ln == 0 or off + ln > len(blob):
                raise _CodeError
            code_ranges.append((off, ln))
            if fl & 2:
                b.skip(b.u32())
                b.skip(b.u32())
        if fl & 4:
            _code_attrs(b, types)
    for _ in range(nvars):
        if b.u32() >= len(types):
            raise _CodeError
        if b.u8() & 1:
            b.skip(b.u32())
    strings: list[str] = []
    budget = [MAX_CODE_CANDIDATES]
    note = ""
    for off, end in _merge_ranges(code_ranges):
        if not _scan_constants(blob, off, end, types, strings, budget):
            note = ("the [Code] scan stopped at its limit of "
                    f"{MAX_CODE_CANDIDATES:,} places looked at; later strings are not listed")
            break
        if len(strings) >= MAX_CODE_STRINGS:
            break
    return CodeInfo(True, True, strings[:MAX_CODE_STRINGS], imports, note)


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """(start, end) of the (offset, length) ranges with overlaps joined, so every byte is
    looked at once at most however many procedures share it."""
    out: list[list[int]] = []
    for off, ln in sorted(ranges):
        if out and off <= out[-1][1]:
            out[-1][1] = max(out[-1][1], off + ln)
        else:
            out.append([off, off + ln])
    return [(a, b) for a, b in out]


def _scan_constants(blob: bytes, i: int, end: int, types, out: list[str],
                    budget: list[int]) -> bool:
    """Append the string constants pushed in blob[i:end]; False when the budget ran out."""
    while len(out) < MAX_CODE_STRINGS:
        i = blob.find(b"\x01", i, end - 8)
        if i < 0:
            return True
        budget[0] -= 1
        if budget[0] < 0:
            return False
        ti, n = struct.unpack_from("<II", blob, i + 1)
        width = _STRING_BASES.get(types[ti][0]) if ti < len(types) else None
        if width and 0 < n <= 65535 and i + 9 + n * width <= end:
            raw = blob[i + 9:i + 9 + n * width]
            text = raw.decode("utf-16-le", "replace") if width == 2 else raw.decode("cp1252",
                                                                                     "replace")
            if _printable(text):
                out.append(text)
                i += 9 + n * width
                continue
        i += 1
    return True


_RUN16 = re.compile(rb"(?:[\x20-\x7e]\x00){4,}")
_RUN8 = re.compile(rb"[\x20-\x7e]{4,}")


def read_code(blob: bytes) -> CodeInfo:
    """String constants and DLL imports of a compiled [Code] blob. Never raises."""
    if not blob:
        return CodeInfo()
    try:
        return _walk_code(blob)
    except (_CodeError, struct.error, IndexError):
        pass
    found: list[str] = []
    for rx, enc in ((_RUN16, "utf-16-le"), (_RUN8, "ascii")):
        for m in rx.finditer(blob):
            s = m.group(0).decode(enc, "replace")
            if any(c in s for c in "{\\.:") and len(found) < MAX_CODE_STRINGS:
                found.append(s)
    return CodeInfo(True, False, found, [],
                    "the [Code] container was not understood; these are possible strings, "
                    "not checked")


# --- reading a setup -------------------------------------------------------------------------

def _open(path: Path) -> tuple[int, int]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise InnoError(f"the file could not be opened ({exc.strerror or exc})") from exc
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise InnoError("not a regular file")
        return fd, info.st_size
    except BaseException:
        os.close(fd)
        raise


def read_setup(path: Path) -> Setup:
    """Read an Inno Setup installer's headers and entry lists. Raises InnoError only."""
    fd, size = _open(Path(path))
    try:
        return read_fd(fd, Path(path), size)
    finally:
        os.close(fd)


def read_fd(fd: int, path: Path, size: int | None = None) -> Setup:
    """As read_setup, from an open file. Raises InnoError only (never another exception for a
    damaged or hostile file)."""
    try:
        if size is None:
            size = os.fstat(fd).st_size
        return _read(fd, Path(path), size)
    except InnoError:
        raise
    except MemoryError as exc:
        raise InnoError("there was not enough memory to read the setup data") from exc
    except OSError as exc:
        raise InnoError(f"the file could not be read ({exc.strerror or exc})") from exc
    except Exception as exc:  # a hostile file must never escape as a crash
        raise InnoError(f"the setup data could not be read ({type(exc).__name__})") from exc


NO_TABLE = ("no Inno Setup offset table was found (not an Inno Setup installer, or one gitfurl "
            "cannot read)")


def _read(fd: int, path: Path, size: int) -> Setup:
    if size > MAX_FILE:
        raise InnoError("the file is larger than 64 GiB")
    found = _find_table(fd, size)
    if found is not None:
        return _read_data(fd, size, path, size, _read_table(fd, size, *found), None)
    # UseSetupLdr=no: setup.exe is the Setup program itself, and the setup data is in
    # <stem>-0.bin beside it. Only a file that starts with a setup ID counts.
    side = _Beside(path.parent).find(f"{path.stem}-0.bin")
    if side is None:
        raise InnoError(NO_TABLE)
    name = _clean(side.name, 60)
    try:
        dfd, dsize = _open(side)
    except InnoError as exc:
        if not os.path.isfile(side) or os.path.islink(side):
            raise InnoError(NO_TABLE) from exc  # not a file: no evidence of a setup here
        raise InnoError(f"the setup data file {name} beside the installer could not be read: "
                        f"{exc}") from exc
    try:
        head = _pread(dfd, 64, 0)
        if not head.startswith(_ID_PREFIX):
            raise InnoError(NO_TABLE)
        if len(head) < 64:
            raise InnoError(f"setup data in {name} beside the installer: the file is shorter "
                            "than its 64-byte format ID")
        if dsize > MAX_FILE:
            raise InnoError(f"setup data in {name} beside the installer: the file is larger "
                            "than 64 GiB")
        try:
            return _read_data(dfd, dsize, path, size, None, side)
        except InnoError as exc:
            raise InnoError(f"setup data in {name} beside the installer: {exc}") from exc
    finally:
        os.close(dfd)


def _read_data(hfd: int, hsize: int, path: Path, size: int, t: _Table | None,
               side: Path | None) -> Setup:
    """The setup data from hfd: setup.exe at the table's Offset0 (t given), or setup-0.bin
    from offset 0 (side given, no loader)."""
    offset0 = t.offset0 if t is not None else 0
    raw_id = _pread(hfd, 64, offset0)
    if len(raw_id) < 64:
        raise InnoError("the setup data is cut short")
    fmt = _format_of(raw_id)
    f = _F[fmt]
    if t is not None and (t.version == 1) != (f <= F643):
        raise InnoError("the offset table's version does not match the setup data format")
    pos = offset0 + 64
    encryption = 0
    if f >= F650:
        enc = _pread(hfd, 53, pos)
        if len(enc) < 53 or zlib.crc32(enc[4:]) != struct.unpack_from("<I", enc, 0)[0]:
            raise InnoError("the encryption header is damaged (CRC-32 mismatch)")
        encryption = enc[4]
        if encryption == 2:
            raise InnoError("the installer is fully encrypted with a password; gitfurl does "
                            "not decrypt installers")
        if encryption > 2:
            raise InnoError("the encryption header has an unknown value")
        pos += 53
    block1, pos, _ = _block(hfd, pos, hsize, f, MAX_BLOCK1, "the setup header block")
    block2, pos, packed2 = _block(hfd, pos, hsize, f, MAX_BLOCK2, "the file location block")
    if t is not None and pos != t.offset_exe:
        raise InnoError("the setup data does not end where the offset table says")
    # The file data is in slice files when the loader's Offset1 is 0 or there is no loader.
    # With disk spanning the compiler stores the file location block; without, it compresses it.
    if t is not None and t.offset1 == 0 and packed2:
        raise InnoError("unexpected layout: the file data is in slice files (disk spanning), but "
                        "the file location block is compressed, which Inno Setup's compiler "
                        "does not write; gitfurl does not guess")
    spanning = (t is not None and t.offset1 == 0) or (t is None and not packed2)
    variant = False
    if f == F550:
        fits = []
        for v in (False, True):
            try:
                fits.append((v, _parse_block1(block1, f, v, strict=True)))
            except _Damage:
                pass
        if len(fits) != 1:
            raise InnoError("ambiguous or damaged 5.5 setup data")
        variant, (h, ent, notes) = fits[0]
    else:
        h, ent, notes = _parse_block1(block1, f, False, strict=f == F557)
    if t is None and not spanning and h["slices"] != 1:
        raise InnoError("unexpected layout: no disk spanning, but a slices-per-disk value other "
                        "than 1; gitfurl does not guess")
    locations = _locations(block2, f, h["counts"]["file locations"])
    if f < F650 and h["encryption_used"]:
        encryption = 1
    arch_allowed, arch_64 = h["strings"]["architectures allowed"], \
        h["strings"]["architectures in 64-bit mode"]
    if h["arch"] is not None:
        arch_allowed, arch_64 = _arch_bits(h["arch"][0], True), _arch_bits(h["arch"][1], False)
    setup = Setup(path, size, fmt, raw_id.rstrip(b"\0").decode("ascii", "replace"), variant,
                  t.version if t else 0, offset0, t.offset1 if t else 0,
                  t.offset_exe if t else 0, encryption, h["strings"],
                  h["privileges"], h["compression"], h["slices"], h["password"],
                  h["code_version"], arch_allowed, arch_64,
                  ent["languages"], ent["custom messages"], ent["types"], ent["components"],
                  ent["tasks"], ent["dirs"], ent["files"], ent["icons"], ent["ini"],
                  ent["registry"], ent["install delete"], ent["uninstall delete"], ent["run"],
                  ent["uninstall run"], locations, read_code(h["code"]), list(notes),
                  side, hsize if side is not None else 0, spanning)
    _check_locations(setup)
    if not setup.verified:
        setup.notes.append(f"data format {fmt} was never checked against a real installer; "
                           "it follows the specification only (unverified)")
    if setup.slice_layout and fmt not in SLICES_VERIFIED_FORMATS:
        setup.notes.append("reading slice files" + (" and setup-0.bin" if side else "")
                           + f" for data format {fmt} follows the specification only; it was "
                           "checked against real installers only for formats "
                           + ", ".join(sorted(SLICES_VERIFIED_FORMATS)) + " (unverified)")
    if side is not None and pos < hsize:
        setup.notes.append(f"{hsize - pos:,} bytes after the setup data in "
                           f"{_clean(side.name, 60)} were ignored")
    if variant:
        setup.notes.append("the 5.5.0 layout with one extra header byte (written by some "
                           "modified Inno Setup builds) was used")
    if setup.code.present and f >= F7003 and setup.code_version >> 31:
        setup.notes.append("the [Code] was compiled for the 64-bit Setup; its string scan was "
                           "never checked on such a blob")
    return setup


def _arch_bits(v: int, allowed: bool) -> str:
    names = [n for i, n in ((1, "x86"), (2, "x64"), (3, "ia64"), (4, "arm64")) if v >> i & 1]
    return " or ".join(names)


def _check_locations(s: Setup) -> None:
    chunks: dict[tuple[int, int], int] = {}
    head = _slice_header_len(_F[s.format])
    for loc in s.locations:
        key = (loc.first_slice, loc.start)
        if chunks.setdefault(key, loc.chunk_size) != loc.chunk_size:
            raise InnoError("two file locations in one chunk disagree on its size")
        if s.offset1:
            if loc.first_slice or loc.last_slice:
                raise InnoError("a file location points to a slice file in a single-file setup")
            if s.offset1 + loc.start + 4 + loc.chunk_size > s.offset0:
                raise InnoError("a file location points outside the setup's file data")
            continue
        if not s.spanning and (loc.first_slice or loc.last_slice):
            raise InnoError("a file location points to a second slice file in a setup without "
                            "disk spanning")
        if loc.start < head:
            raise InnoError("a file location points into a slice file's header")


# --- slice files ---------------------------------------------------------------------------

def _slice_header_len(f: int) -> int:
    """Bytes of a slice file's header: signature and size (12 up to 6.5.0, 16 from 6.5.2)."""
    return 16 if f >= F652 else 12


def slice_name(stem: str, per: int, n: int) -> str:
    """The file name of slice number n (0-based) with `per` slices per disk: stem-1.bin, or
    stem-1a.bin when a disk holds more than one slice."""
    disk = n // per + 1
    return f"{stem}-{disk}.bin" if per == 1 else f"{stem}-{disk}{chr(97 + n % per)}.bin"


def check_slice_header(raw: bytes, size: int, f: int) -> int:
    """Check a slice file's first bytes against its real size; the header length, or InnoError
    with the reason. The header has no slice number or checksum: a slice from another build
    or with another number passes when its own size field is right."""
    sig = SLICE_SIG_NEW if f >= F652 else SLICE_SIG_OLD
    head = _slice_header_len(f)
    if len(raw) < 8:
        raise InnoError("the file is shorter than its slice header")
    if raw[:8] != sig:
        if raw[:8] in (SLICE_SIG_OLD, SLICE_SIG_NEW):
            raise InnoError("its slice header belongs to another Inno Setup version")
        raise InnoError("it does not start with an Inno Setup slice header")
    if len(raw) < head:
        raise InnoError("the file is shorter than its slice header")
    total = struct.unpack_from("<q" if head == 16 else "<I", raw, 8)[0]
    if total != size:
        why = " (0: the compile that made it was interrupted)" if total == 0 else ""
        raise InnoError(f"its header gives a size of {total:,} bytes{why}, but the file has "
                        f"{size:,} bytes")
    return head


def _fold(name: str) -> str:
    """Letter case folded one character at a time, never changing the length (str.casefold
    turns 'ß' into 'ss', which Windows does not treat as the same name)."""
    return "".join(c.lower() if len(c.lower()) == 1 else c for c in name)


class _Beside:
    """Files in the installer's folder, found by name without regard to letter case (as
    Windows does). An exact match wins; names that differ only in letter case are not
    guessed between. Only names are looked at here; nothing is opened."""

    def __init__(self, folder: Path):
        self.folder = folder
        self.names: dict[str, list[str]] | None = None

    def find(self, name: str) -> Path | None:
        exact = self.folder / name
        if os.path.lexists(exact):
            return exact
        if self.names is None:
            self.names = {}
            try:
                with os.scandir(self.folder) as it:
                    for k, e in enumerate(it):
                        if k >= MAX_DIR_ENTRIES:
                            break
                        self.names.setdefault(_fold(e.name), []).append(e.name)
            except OSError:
                pass
        got = self.names.get(_fold(name), [])
        if len(got) > 1:
            raise InnoError(f"several files beside the installer are named {_clean(name, 60)} "
                            "in different letter case; gitfurl does not guess between them")
        return self.folder / got[0] if got else None


def _open_slice(path: Path, f: int) -> tuple[int, int]:
    """(fd, size) of a slice file whose header was checked. O_NOFOLLOW: a symbolic link is
    refused. Raises InnoError with a plain reason."""
    name = _clean(path.name, 60)
    try:
        fd, size = _open(path)
    except InnoError as exc:
        if os.path.islink(path):
            raise InnoError(f"slice file {name} is a symbolic link; gitfurl does not follow "
                            "links to slice files") from exc
        raise InnoError(f"slice file {name}: {exc}") from exc
    try:
        check_slice_header(_pread(fd, 16, 0), size, f)
    except InnoError as exc:
        os.close(fd)
        raise InnoError(f"slice file {name}: {exc}") from exc
    except OSError as exc:
        os.close(fd)
        raise InnoError(f"slice file {name} could not be read ({exc.strerror or exc})") \
            from exc
    except BaseException:
        os.close(fd)
        raise
    return fd, size


def _slice_numbers(locations, cap: int) -> tuple[list[int], bool]:
    """The slice numbers the locations name, ascending; True when there are more than cap."""
    out: list[int] = []
    end = -1
    for a, b in sorted({(loc.first_slice, loc.last_slice) for loc in locations}):
        for n in range(max(a, end + 1), b + 1):
            if len(out) >= cap:
                return out, True
            out.append(n)
        end = max(end, b)
    return out, False


def _stem_problem(stem: str) -> str | None:
    if not stem or stem in (".", "..") or "/" in stem or "\\" in stem:
        return "the installer's file name cannot be used to name its slice files"
    return None


def _resolve_slices(folder: Path, stem: str, per: int, numbers: list[int], f: int
                    ) -> dict[int, tuple[Path, int]]:
    """{slice number: (path, size)} for the numbers, each opened and its header checked.
    Raises InnoError when one is missing or wrong. Only the installer's own folder is used."""
    beside = _Beside(folder)
    found: dict[int, Path] = {}
    missing: list[str] = []
    for n in numbers:
        name = slice_name(stem, per, n)
        got = beside.find(name)
        if got is None:
            missing.append(name)
        else:
            found[n] = got
    if missing:
        raise InnoError(f"{_n(len(missing), 'slice file is', 'slice files are')} missing beside "
                        "the installer: " + ", ".join(_clean(m, 60) for m in missing[:5])
                        + (" ..." if len(missing) > 5 else "")
                        + " (gitfurl looks only in the installer's own folder)")
    out: dict[int, tuple[Path, int]] = {}
    for n, path in found.items():
        fd, size = _open_slice(path, f)
        os.close(fd)
        out[n] = (path, size)
    return out


def _span_problem(loc: Location, sizes: dict[int, int], f: int) -> str | None:
    """Why a chunk's bytes do not fit its slice files exactly (stricter than Setup), or None.
    The payload runs from just after the chunk marker to the end of slice first_slice, then
    from just after the header of each next slice, and must end in slice last_slice."""
    head = _slice_header_len(f)
    first = sizes[loc.first_slice]
    if loc.start < head or loc.start + 4 + 1 > first:
        return "a data chunk starts outside its slice file's data; the setup is damaged"
    left = loc.chunk_size
    for n in range(loc.first_slice, loc.last_slice + 1):
        avail = sizes[n] - (loc.start + 4 if n == loc.first_slice else head)
        if n < loc.last_slice:
            if left <= avail:
                return ("a data chunk ends before the last slice file its location names; the "
                        "setup or a slice file is damaged")
            left -= avail
        elif left > avail:
            return ("a data chunk runs past the end of the last slice file its location names; "
                    "the setup or a slice file is damaged")
    return None


# --- choices: conditions as Inno evaluates them by default -----------------------------------

_TOKEN = re.compile(r"\s*(?:(\()|(\))|([^\s()]+))")
_PREC = {"or": 1, "and": 2, "not": 3}


def evaluate(expr: str, true_names: set[str]) -> bool | None:
    """Evaluate a Components/Tasks/Languages condition (names joined by and, or, not,
    parentheses; a space-separated list means "or"). Names compare without letter case.
    None when the expression is not understood. No recursion."""
    if len(expr) > 4096:
        return None
    toks: list[str] = []
    pos = 0
    text = expr.strip()
    while pos < len(text):
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            return None
        pos = m.end()
        toks.append(m.group(1) or m.group(2) or m.group(3))
    if not toks:
        return True
    # implicit "or" between two operands
    full: list[str] = []
    for t in toks:
        low = t.lower()
        starts = low == "(" or low == "not" or low not in _PREC and low != ")"
        if full and starts:
            prev = full[-1].lower()
            if prev == ")" or (prev not in _PREC and prev != "("):
                full.append("or")
        full.append(t)
    out: list[str] = []
    ops: list[str] = []
    for t in full:
        low = t.lower()
        if low == "(":
            ops.append("(")
        elif low == ")":
            while ops and ops[-1] != "(":
                out.append(ops.pop())
            if not ops:
                return None
            ops.pop()
        elif low in _PREC:
            while ops and ops[-1] != "(" and low != "not" and _PREC[ops[-1]] >= _PREC[low]:
                out.append(ops.pop())
            ops.append(low)
        else:
            if any(c in t for c in "!*{}"):
                return None
            out.append("=" + t.casefold())
    while ops:
        if ops[-1] == "(":
            return None
        out.append(ops.pop())
    stack: list[bool] = []
    try:
        for t in out:
            if t.startswith("="):
                stack.append(t[1:] in true_names)
            elif t == "not":
                stack.append(not stack.pop())
            else:
                a, b = stack.pop(), stack.pop()
                stack.append(a and b if t == "and" else a or b)
    except IndexError:
        return None
    return stack[0] if len(stack) == 1 else None


@dataclass
class Choice:
    """The default choices of a silent install, as far as gitfurl can tell without running
    the script."""

    language: str
    setup_type: str
    components: set[str]
    tasks: set[str]
    notes: list[str] = field(default_factory=list)

    def decide(self, cond: Conditions) -> tuple[bool | None, str]:
        """(selected, reason). None: it depends on something gitfurl cannot evaluate."""
        if cond.check:
            return None, "depends on a [Code] check function"
        for expr, names, what in ((cond.components, self.components, "components"),
                                  (cond.tasks, self.tasks, "tasks"),
                                  (cond.languages, {self.language}, "languages")):
            if not expr:
                continue
            got = evaluate(expr, names)
            if got is None:
                return None, f"its {what} condition is not understood"
            if not got:
                return False, f"not part of the default {what}"
        return True, ""


def default_choice(s: Setup) -> Choice:
    lang = s.languages[0].name.casefold() if s.languages else ""
    notes = []
    if len(s.languages) > 1:
        notes.append(f"language: the first one ({s.languages[0].name}) is assumed; Setup may "
                     "pick another from the system language")
    stype = s.types[0].name.casefold() if s.types else ""
    comps: set[str] = set()
    for c in s.components:
        if c.cond.check or (c.cond.languages and not evaluate(c.cond.languages, {lang})):
            continue
        types = {t.casefold() for t in re.split(r"[,\s]+", c.types) if t}  # stored with commas
        if (stype and stype in types) or (not s.types and not c.types):
            comps.add(c.name.casefold())
    tasks: set[str] = set()
    for t in s.tasks:
        if t.unchecked or t.cond.check:
            continue
        ok = all(not expr or evaluate(expr, names)
                 for expr, names in ((t.cond.components, comps), (t.cond.languages, {lang})))
        if ok:
            tasks.add(t.name.casefold())
    if s.types:
        notes.append(f"setup type: the first one ({s.types[0].name})")
    return Choice(lang, stype, comps, tasks, notes)


# --- what the plan shows ---------------------------------------------------------------------

def _clean(text: str, limit: int = SHOW) -> str:
    from gitfurl.detect import clean

    return clean(text, limit)


def _n(count: int, one: str, many: str) -> str:
    return f"{count:,} {one if count == 1 else many}"


def _size(n: int) -> str:
    for unit, shift in (("GiB", 30), ("MiB", 20), ("KiB", 10)):
        if n >= 1 << shift:
            return f"{n / (1 << shift):.1f} {unit}"
    return f"{n} bytes" if n != 1 else "1 byte"


_CONST = re.compile(r"\{[^{}]*\}")


def looks_redistributable(filename: str) -> bool:
    """True when a file name looks like a runtime installer (Visual C++, DirectX, .NET, ...)."""
    plain = _CONST.sub("", filename.replace("{{", "")).casefold()
    parts = [p for p in re.split(r"[\\/]", plain) if p]
    if not parts:
        return False
    if any(p in REDIST_DIRS for p in parts[:-1]):
        return True
    return parts[-1].startswith(REDIST_PREFIXES)


def silent_run(s: Setup) -> list[tuple[RunEntry, bool | None, bool]]:
    """[Run] entries with (runs in a silent install: True/False/None, looks redistributable)."""
    return [(r, r.silent_runs(), looks_redistributable(r.filename)) for r in s.run]


@dataclass(frozen=True, slots=True)
class BesideFile:
    name: str  # as named by the setup (after {src}\)
    source: str  # "[Files] external entry" or "[Code] string"
    present: bool
    optional: bool  # Setup skips it when missing (skipifsourcedoesntexist)
    siblings: int = 0  # numbered files after it that are also present
    checked: bool = True  # False: in a folder deeper than MAX_SUBFOLDERS, not looked up


_NUMBERED = re.compile(r"^(.*?)(\d+)(\.[A-Za-z0-9]{1,8})$")
_ARCHIVE_EXT = (".bin", ".arc", ".7z", ".zip", ".rar", ".cab", ".pak", ".dat", ".001")


def _folder_names(folder: Path) -> tuple[dict[str, str], dict[str, str]]:
    """(files, folders) in a folder, each casefolded name -> name. Symbolic links count as
    what they point to (Wine follows them); only names and file types are looked at, nothing
    is opened."""
    files: dict[str, str] = {}
    dirs: dict[str, str] = {}
    try:
        with os.scandir(folder) as it:
            for k, e in enumerate(it):
                if k >= MAX_DIR_ENTRIES:
                    break
                try:
                    if e.is_file():
                        files.setdefault(e.name.casefold(), e.name)
                    elif e.is_dir():
                        dirs.setdefault(e.name.casefold(), e.name)
                except OSError:
                    continue
    except OSError:
        pass
    return files, dirs


_BAD_NAME_CHARS = set('{}*?<>|":')


def _name_parts(name: str) -> list[str] | None:
    """The folder and file parts of a name below {src}, or None when one is not a plain name."""
    if not name or len(name) > 255:
        return None
    parts = re.split(r"[\\/]", name)
    for p in parts:
        if p in ("", ".", "..") or any(c in _BAD_NAME_CHARS or ord(c) < 32 for c in p):
            return None
    return parts


def beside_files(s: Setup, folder: Path | None = None) -> list[BesideFile]:
    """Files the setup names beside itself ({src}\\...), checked against the folder.

    Sources: external [Files] entries (reliable) and [Code] string constants (a pattern scan;
    names built at run time are missed). Names with wildcards or other constants are left out.
    Names in subfolders are looked up up to MAX_SUBFOLDERS levels down; deeper ones are listed
    as not checked.
    """
    folder = Path(folder) if folder is not None else s.path.parent
    listings: dict[Path, tuple[dict[str, str], dict[str, str]]] = {}

    def listing(where: Path) -> tuple[dict[str, str], dict[str, str]]:
        got = listings.get(where)
        if got is None:
            got = _folder_names(where) if len(listings) < 32 else ({}, {})
            listings[where] = got
        return got

    out: dict[str, BesideFile] = {}

    def add(raw: str, source: str, optional: bool, bare: bool) -> None:
        if bare:
            name = raw.lstrip("\\")
        else:
            low = raw.casefold()
            if not low.startswith(("{src}\\", "{src}/")):
                return
            name = raw[6:]
        parts = _name_parts(name)
        if parts is None:
            return
        key = "\\".join(parts).casefold()
        if key in out or len(out) >= 200:
            return
        if len(parts) - 1 > MAX_SUBFOLDERS:
            out[key] = BesideFile("\\".join(parts), source, False, optional, checked=False)
            return
        where, shown = folder, []
        for part in parts[:-1]:
            sub = listing(where)[1].get(part.casefold())
            if sub is None:
                out[key] = BesideFile("\\".join(parts), source, False, optional)
                return
            where = where / sub
            shown.append(sub)
        present = listing(where)[0]
        last = parts[-1]
        here = last.casefold() in present
        siblings = 0
        m = _NUMBERED.match(last)
        if here and m:
            stem, num, ext = m.groups()
            for k in range(int(num) + 1, int(num) + 1 + MAX_SIBLINGS):
                cand = f"{stem}{k:0{len(num)}d}{ext}".casefold()
                if cand not in present:
                    break
                siblings += 1
        shown.append(present.get(last.casefold(), last))
        out[key] = BesideFile("\\".join(shown), source, here, optional, siblings)

    for fe in s.files:
        if fe.kind == FILE_EXTERNAL:
            add(fe.source, "[Files] external entry", "skip if source does not exist" in fe.flags,
                False)
    for text in s.code.strings:
        if text.casefold().startswith(("{src}\\", "{src}/")):
            add(text, "[Code] string", False, False)
        elif re.fullmatch(r"\\?[^\\/{}:*?]+", text) and text.casefold().endswith(_ARCHIVE_EXT):
            add(text, "[Code] string (a bare name)", True, True)
    return list(out.values())


def named_beside(s: Setup, folder: Path | None = None) -> set[str]:
    """Casefolded names of files directly in the folder (not in a subfolder) that the setup
    names and that are there, with the numbered files after a named one ("data-01.bin" also
    gives "data-02.bin" when both are present). From beside_files(); nothing is opened."""
    out: set[str] = set()
    for bf in beside_files(s, folder):
        if not bf.present or not bf.checked or "\\" in bf.name:
            continue
        out.add(bf.name.casefold())
        m = _NUMBERED.match(bf.name)
        if bf.siblings and m:
            stem, num, ext = m.groups()
            for k in range(int(num) + 1, int(num) + 1 + bf.siblings):
                out.add(f"{stem}{k:0{len(num)}d}{ext}".casefold())
    return out


def summarise(s: Setup, folder: Path | None = None) -> list[str]:
    """Plain-text lines for the install plan. Every string from the setup is cleaned."""
    lines = []
    ver = f" {_clean(s.app_version, 40)}" if s.app_version else ""
    lines.append(f"app: {_clean(s.app_name, 60)}{ver}; default folder: "
                 f"{_clean(s.default_dir)}; privileges required: "
                 f"{PRIVILEGES.get(s.privileges, 'unknown')}; compression: {s.compression_name}")
    if s.arch_allowed or s.arch_64bit:
        lines.append(f"architectures allowed: {_clean(s.arch_allowed or 'any')}; 64-bit mode: "
                     f"{_clean(s.arch_64bit or 'never')}")
    lines.extend(_layout_lines(s, folder))
    kinds = {k: 0 for k in (FILE_STORED, FILE_EXTERNAL, FILE_DOWNLOAD, FILE_UNINSTALLER)}
    for fe in s.files:
        kinds[fe.kind] += 1
    stored_size = sum(s.locations[fe.location].size for fe in s.files if fe.kind == FILE_STORED)
    held_in = "its slice files" if s.slice_layout else "setup.exe"
    parts = [(f"{_n(kinds[FILE_STORED], 'file', 'files')} stored in {held_in} "
              f"({_size(stored_size)})")]
    if kinds[FILE_EXTERNAL]:
        parts.append(f"{_n(kinds[FILE_EXTERNAL], 'external file', 'external files')} "
                     "(copied from the user's disk)")
    if kinds[FILE_DOWNLOAD]:
        parts.append(f"{_n(kinds[FILE_DOWNLOAD], 'download', 'downloads')} (gitfurl never "
                     "downloads)")
    if kinds[FILE_UNINSTALLER]:
        parts.append("the uninstaller")
    lines.append("what Inno itself writes: " + ", ".join(parts))
    lines.append(f"  also: {_n(len(s.dirs), 'folder', 'folders')}, "
                 f"{_n(len(s.icons), 'shortcut', 'shortcuts')}, "
                 f"{_n(len(s.ini), 'INI value', 'INI values')}, "
                 f"{_n(len(s.registry), 'registry entry', 'registry entries')}, "
                 f"{_n(len(s.install_delete), 'delete entry', 'delete entries')}; "
                 f"{_n(len(s.components), 'component', 'components')}, "
                 f"{_n(len(s.tasks), 'task', 'tasks')}")
    conditional = sum(1 for fe in s.files if fe.cond.conditional)
    if conditional:
        lines.append(f"  {_n(conditional, 'file entry depends', 'file entries depend')} on "
                     "components, tasks, languages or a [Code] check")
    runs = silent_run(s)
    if runs:
        lines.append("[Run] entries a silent install would run (in order):")
        shown = 0
        for r, runs_silent, redist in runs:
            if runs_silent is False:
                continue
            shown += 1
            if shown > LISTED:
                break
            what = _clean(r.filename) + (f" {_clean(r.parameters, 60)}" if r.parameters else "")
            how = [WAITS.get(r.wait, "unknown wait")]
            if r.show == 0:
                how.append("hidden")
            if "shell exec" in r.flags:
                how.append("opened by the shell" + (f" ({_clean(r.verb, 20)})" if r.verb else ""))
            if r.cond.conditional:
                how.append("conditional")
            if runs_silent is None:
                how.append("post-install and unchecked: probably not run (unverified)")
            line = f"  - {what} ({', '.join(how)})"
            if redist:
                line += ("; looks like a redistributable: gitfurl's own extraction skips it "
                         "(listed only, never run); Setup itself would run it")
            lines.append(line)
        if shown == 0:
            lines.append("  (none)")
        skipped = sum(1 for _, rs, _ in runs if rs is False)
        if skipped:
            lines.append(f"  {_n(skipped, 'entry is', 'entries are')} skipped in a silent "
                         "install (skipifsilent)")
    redist_files = [fe for fe in s.files if fe.kind == FILE_STORED
                    and looks_redistributable(fe.dest)]
    if redist_files:
        lines.append(f"redistributable files: {_n(len(redist_files), 'file', 'files')}, for "
                     f"example {_clean(redist_files[0].dest)}; skipped by gitfurl's extraction")
    beside = beside_files(s, folder)
    missing = [b for b in beside if b.checked and not b.present and not b.optional]
    for b in beside:
        state = "present" if b.present else "missing"
        extra = f" (and {b.siblings} numbered after it)" if b.siblings else ""
        if not b.checked:
            state = (f"not checked (more than {MAX_SUBFOLDERS} folder levels below the "
                     "installer)")
        elif not b.present and b.optional:
            state = "not found (Setup may skip it, or the name is built at run time)"
        lines.append(f"file beside the installer: {_clean(b.name)}: {state}{extra}; named by "
                     f"{b.source}")
    if missing:
        lines.append("warning: " + ", ".join(_clean(b.name, 60) for b in missing[:5])
                     + (" ..." if len(missing) > 5 else "")
                     + " not found beside the installer; a silent run is likely to stop on "
                     "the script's own error window")
    if s.code.present:
        lines.append(f"[Code]: {_n(len(s.code.strings), 'string constant', 'string constants')}"
                     + (f"; DLL imports: {', '.join(_clean(i, 60) for i in s.code.imports[:4])}"
                        if s.code.imports else "")
                     + (f" ({s.code.note})" if s.code.note else ""))
        lines.append("  names built at run time, obfuscated strings and paths read from files "
                     "or the registry are not seen by this scan")
    if s.password_required:
        lines.append("password: Setup asks for a password at startup")
    encrypted = sum(1 for loc in s.locations if loc.encrypted)
    if encrypted or s.encryption:
        lines.append(f"encryption: {_n(encrypted, 'file is', 'files are')} encrypted with the "
                     "installer password; gitfurl does not decrypt installers")
    if s.compression not in COMPRESSION:
        lines.append(f"file data: compressed with {METHOD_OTHER}")
    lines.extend(f"note: {n}" for n in s.notes)
    return lines


NAMES_NOT_KNOWN = "names not known: the slices-per-disk value is out of its range"


def slice_names(s: Setup, limit: int = 64) -> list[str]:
    """Names of the slice files the setup's file locations name, in slice order (at most
    limit); empty when the header's slices-per-disk value is out of its range (1 to 26)."""
    per = s.slices_per_disk
    if not 1 <= per <= MAX_SLICES_PER_DISK:
        return []
    return [slice_name(s.path.stem, per, n) for n in _slice_numbers(s.locations, limit)[0]]


def _layout_lines(s: Setup, folder: Path | None) -> list[str]:
    """Where the setup data and the file data are, and which slice files are present (by name
    only, as in the preflight of files beside the installer: nothing is opened here)."""
    if not s.slice_layout:
        return []
    if s.data_file is not None:
        head = (f"layout: setup data in {_clean(s.data_file.name, 60)} beside the installer "
                "(setup.exe has no loader)")
    else:
        head = "layout: setup data inside setup.exe"
    head += ("; file data in slice files beside the installer"
             + (" (disk spanning)" if s.disk_spanning else ""))
    lines = [head]
    if not s.locations:
        return lines
    if not 1 <= s.slices_per_disk <= MAX_SLICES_PER_DISK:
        return lines + [f"slice files: {NAMES_NOT_KNOWN}"]
    problem = _stem_problem(s.path.stem)
    if problem:
        return lines + [f"slice files: {problem}"]
    numbers, cut = _slice_numbers(s.locations, MAX_SLICES)
    beside = _Beside(Path(folder) if folder is not None else s.path.parent)
    names, missing = [], []
    for n in numbers:
        name = slice_name(s.path.stem, s.slices_per_disk, n)
        try:
            found = beside.find(name)
        except InnoError:
            found = None
        names.append(_clean(found.name if found else name, 60))
        if found is None:
            missing.append(names[-1])
    state = "all present" if not missing else f"{len(missing):,} missing"
    lines.append(f"slice files: {_n(len(names), 'file', 'files')} named by the file locations: "
                 + ", ".join(names[:LISTED]) + (" ..." if len(names) > LISTED else "")
                 + f" ({state})"
                 + (f"; the setup names more than {MAX_SLICES:,}, the rest were not looked "
                    "for" if cut else ""))
    if missing:
        lines.append("warning: " + ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
                     + " not found beside the installer; gitfurl's extraction refuses until "
                     "every slice file it needs is there")
    return lines


# --- extraction ------------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class Target:
    folder: int  # folder id in InnoExtractPlan.folders
    name: str
    shown: str


@dataclass
class _ChunkPlan:
    start: int  # chunk start, relative to Offset1 (or in slice file `first`)
    size: int  # compressed size
    compressed: bool
    dict_size: int  # LZMA/LZMA2 dictionary (0 for others)
    items: list = field(default_factory=list)  # (Location index, [Target]) by offset
    decoded: int = 0  # bytes of the chunk that are decoded: up to the end of its last file
    full: int = 0  # end of the chunk's last file in the file table, written or not: the
    # decoder never outputs more, so a dictionary of this size (up to dict_size) holds it all
    first: int = 0  # slice numbers of the chunk's first and last byte (slice layouts)
    last: int = 0


@dataclass
class InnoExtractPlan:
    installer: Path
    prefix_name: str
    prefix: Path
    dest: Path
    setup: Setup
    choice: Choice
    chunks: list[_ChunkPlan]
    total_size: int
    file_count: int
    memory: int
    skipped: list[tuple[str, str]]  # (destination, reason), shown cleaned
    notes: list[str] = field(default_factory=list)
    folders: list[tuple[int, str]] = field(default_factory=lambda: [(-1, "")])
    slices: dict = field(default_factory=dict)  # slice number -> (path, size), headers checked

    @property
    def target(self) -> str:
        return f"C:\\Games\\{self.prefix_name}"

    @property
    def decoded(self) -> int:
        """Bytes decoded in all, including the parts of solid chunks that are thrown away."""
        return sum(ch.decoded for ch in self.chunks)

    def disk_size(self, block: int) -> int:
        blocks = len(self.folders)
        for ch in self.chunks:
            for li, targets in ch.items:
                blocks += -(-self.setup.locations[li].size // block) * len(targets)
        return blocks * block

    def problem(self) -> str | None:
        from gitfurl.extract import ExtractPlan

        return ExtractPlan.problem(self)  # same checks: prefix, new folder, space, inodes

    def space_problem(self, at: Path, extra: int = 0) -> str | None:
        from gitfurl.extract import ExtractPlan

        return ExtractPlan.space_problem(self, at, extra)

    def describe(self) -> list[str]:
        s = self.setup
        what = "setup.exe and the files beside it" if s.slice_layout else "setup.exe"
        lines = [
            (f"NATIVE EXTRACTION (Inno Setup's own files): gitfurl reads {what} itself "
             f"(data format {s.format}, compression {s.compression_name})."),
            ("  This extracts the files Inno Setup holds for {app} only. Setup is not run: no "
             "registry entries, shortcuts, INI values, redistributables, [Run] entries or "
             "[Code] steps. The result may not work."),
            (f"  destination: {self.target} (in the prefix: {self.dest}), used as {{app}}; it "
             "must not exist yet, and no existing file is overwritten"),
            (f"  in total: {_n(self.file_count, 'file', 'files')}, {_size(self.total_size)} to "
             f"write, from {_n(len(self.chunks), 'data chunk', 'data chunks')}"),
        ]
        if s.data_file is not None:
            lines.append(f"  setup data: read from {_clean(s.data_file.name, 60)} beside the "
                         "installer (setup.exe has no loader)")
        if self.slices:
            names = [_clean(p.name, 60) for p, _ in self.slices.values()]
            lines.append(f"  file data: read from {_n(len(names), 'slice file', 'slice files')} "
                         "beside the installer: " + ", ".join(names[:LISTED])
                         + (" ..." if len(names) > LISTED else "")
                         + "; each one's header and size were checked"
                         + (" (disk spanning)" if s.disk_spanning else ""))
        if self.decoded > self.total_size:
            lines.append(f"  decoded: {_size(self.decoded)} in all; "
                         f"{_size(self.decoded - self.total_size)} of it only to reach later "
                         "files in a chunk, and thrown away")
        if self.memory:
            lines.append(f"  memory: up to {_size(self.memory)} for the LZMA dictionary of one "
                         "chunk")
        lines.append("  each file's " + ("SHA-1" if len(s.locations[0].digest) == 20 else
                                          "SHA-256") + " is checked; a file that does not "
                     "match, or is cut short, is removed again"
                     if s.locations else "  no stored files")
        lines.extend(f"  choice: {n}" for n in self.choice.notes)
        if self.skipped:
            lines.append(f"  skipped: {_n(len(self.skipped), 'entry', 'entries')}:")
            for dest, why in self.skipped[:LISTED]:
                lines.append(f"    {dest}: {why}")
            if len(self.skipped) > LISTED:
                lines.append(f"    ... and {len(self.skipped) - LISTED} more")
        lines.extend(f"  note: {n}" for n in self.notes)
        return lines


def _dest_parts(dest: str) -> list[str] | str:
    """The parts below {app} of a [Files] destination, or the reason it is skipped."""
    from gitfurl import farc

    low = dest.casefold()
    if low.startswith("{tmp}"):
        return "a temporary file for Setup (under {tmp})"
    if not (low == "{app}" or low.startswith("{app}\\")):
        return "outside {app}"
    rest = dest[5:]
    if "{" in rest.replace("{{", ""):
        return "the destination holds a constant gitfurl does not expand"
    rest = rest.replace("{{", "{")
    parts = farc.relative_parts(rest.lstrip("\\"))
    if not parts:
        return UNSAFE
    return parts


UNSAFE = "the destination is not a safe relative path"


def lzma2_dict_size(p: int) -> int:
    """The dictionary size an LZMA2 property byte names. Raises InnoError above 40."""
    if not 0 <= p <= 40:
        raise InnoError("a data chunk has an invalid LZMA2 property")
    return 0xFFFFFFFF if p == 40 else (2 | (p & 1)) << (p // 2 + 11)


def _dict_size(raw: bytes, method: int) -> int:
    """The LZMA/LZMA2 dictionary named by the first bytes of a chunk's data, 0 for other
    methods."""
    if method == 3:
        if len(raw) < 5 or raw[0] >= 225:
            raise InnoError("a data chunk has invalid LZMA properties")
        return struct.unpack_from("<I", raw, 1)[0]
    if method == 4:
        if len(raw) < 1:
            raise InnoError("a data chunk is cut short")
        return lzma2_dict_size(raw[0])
    return 0


def _lzma_dict(declared: int, full: int) -> int:
    """The dictionary a chunk's decoder gets: the declared size, but no more than the chunk
    decodes to in all (rounded up to 4 KiB). Nothing past `full` is ever decoded, so every
    match distance stays inside it."""
    return max(4096, min(declared, -(-full // 4096) * 4096))


def plan_inno_extract(installer: Path, prefix_name: str, env: dict[str, str] | None = None,
                      setup: Setup | None = None) -> InnoExtractPlan:
    """Read the setup and decide what is written where. Writes nothing. Raises ExtractRefused."""
    from gitfurl.extract import (
        FILE,
        MAX_FOLDERS,
        ExtractRefused,
        _check_length,
        _windows_problem,
    )
    from gitfurl.prefixes import prefix_path, validate_prefix_name

    validate_prefix_name(prefix_name)
    installer = Path(installer)
    if setup is None:
        try:
            setup = read_setup(installer)
        except InnoError as exc:
            raise ExtractRefused(_clean(str(exc), 200)) from exc
    s = setup
    if s.password_required:
        raise ExtractRefused("the installer is password-protected; gitfurl does not extract "
                             "password-protected installers")
    if s.compression not in COMPRESSION:
        raise ExtractRefused(f"the files are compressed with {METHOD_OTHER}")
    if s.slice_layout and not 1 <= s.slices_per_disk <= MAX_SLICES_PER_DISK:
        raise ExtractRefused("the installer keeps its files in slice files whose "
                             + NAMES_NOT_KNOWN)
    choice = default_choice(s)
    skipped: list[tuple[str, str]] = []
    notes: list[str] = []
    picked: dict[tuple[int, str], tuple[int, int, str]] = {}  # (folder, key) -> (file idx, ...)
    folders: list[tuple[int, str]] = [(-1, "")]
    names: dict[tuple[int, str], int] = {}
    merged = None
    for idx, fe in enumerate(s.files):
        shown = _clean(fe.dest or fe.source or "(no name)")
        if fe.kind != FILE_STORED:
            why = {FILE_EXTERNAL: "an external file (Setup copies it from the user's disk)",
                   FILE_DOWNLOAD: "a download (gitfurl never downloads)",
                   FILE_UNINSTALLER: "Setup's uninstaller"}[fe.kind]
            skipped.append((shown, why))
            continue
        if "do not copy" in fe.flags:
            skipped.append((shown, "not copied by Setup (used by its script only)"))
            continue
        if looks_redistributable(fe.dest):
            skipped.append((shown, "looks like a redistributable (never written or run)"))
            continue
        sel, why = choice.decide(fe.cond)
        if not sel:
            skipped.append((shown, why))
            continue
        parts = _dest_parts(fe.dest)
        if parts is UNSAFE:
            raise ExtractRefused(f"the setup holds a name that is not safe to extract: {shown!r}")
        if isinstance(parts, str):
            skipped.append((shown, parts))
            continue
        if "delete after install" in fe.flags:
            skipped.append((shown, "deleted by Setup after the install"))
            continue
        _check_length("the setup", fe.dest, parts)
        if any(_windows_problem(p) for p in parts):
            raise ExtractRefused(f"the setup holds a name Windows cannot use: {shown!r}")
        cur, last = 0, len(parts) - 1
        for i, part in enumerate(parts):
            key = (cur, part.casefold())
            got = names.get(key)
            if i == last:
                if got is not None and got != FILE:
                    raise ExtractRefused(f"{shown!r} has the same name as a folder")
                if got == FILE:
                    old = picked[key]
                    if "only if does not exist" in fe.flags:
                        notes.append(f"{shown}: written by two entries; the first is kept "
                                     "(the later one only writes a missing file)")
                        break
                    notes.append(f"{shown}: written by two entries; the later one is kept, "
                                 "as Setup would overwrite the first")
                    skipped.append((_clean(s.files[old[0]].dest), "replaced by a later entry"))
                names[key] = FILE
                picked[key] = (idx, cur, part)
                break
            if got == FILE:
                raise ExtractRefused(f"{shown!r} would go inside a file of the same name")
            if got is None:
                if len(folders) > MAX_FOLDERS:
                    raise ExtractRefused(f"more than {MAX_FOLDERS:,} folders")
                got = len(folders)
                folders.append((cur, part))
                names[key] = got
            elif merged is None and folders[got][1] != part:
                merged = ("folder names that differ only in letter case are one folder, as on "
                          "Windows (the first spelling is used)")
            cur = got
    if merged:
        notes.append(merged)
    if not picked:
        raise ExtractRefused("the installer holds no files for {app} that gitfurl can extract "
                             f"({_n(len(skipped), 'entry', 'entries')} skipped: temporary "
                             "helpers, external files or entries that depend on the script); "
                             "its payload may be in other files that its script unpacks")
    # group the chosen files by chunk
    by_chunk: dict[tuple[int, int], _ChunkPlan] = {}
    targets: dict[int, list[Target]] = {}
    for idx, cur, part in picked.values():
        fe = s.files[idx]
        targets.setdefault(fe.location, []).append(Target(cur, part, _clean(fe.dest)))
    total = count = memory = 0
    if any(s.locations[li].encrypted for li in targets):
        raise ExtractRefused("a file is encrypted with the installer password; gitfurl does not "
                             "decrypt installers")
    f = _F[s.format]
    slices: dict[int, tuple[Path, int]] = {}
    fd = None
    if s.slice_layout:
        problem = _stem_problem(installer.stem)
        if problem:
            raise ExtractRefused(problem)
        numbers, cut = _slice_numbers([s.locations[li] for li in targets], MAX_SLICES)
        if cut:
            raise ExtractRefused(f"the files are spread over more than {MAX_SLICES:,} slice "
                                 "files")
        try:
            slices = _resolve_slices(installer.parent, installer.stem, s.slices_per_disk,
                                     numbers, f)
        except InnoError as exc:
            raise ExtractRefused(str(exc)) from exc
        source = _Slices(slices, f)
        sizes = {n: size for n, (_, size) in slices.items()}
    else:
        try:
            fd, _ = _open(installer)
        except InnoError as exc:
            raise ExtractRefused(str(exc)) from exc
    try:
        for li in sorted(targets):
            loc = s.locations[li]
            key = (loc.first_slice, loc.start)
            ch = by_chunk.get(key)
            if ch is None:
                if fd is not None:
                    pos = s.offset1 + loc.start
                    if _pread(fd, 4, pos) != CHUNK_MAGIC:
                        raise ExtractRefused("a data chunk does not start with its magic bytes; "
                                             "the setup is damaged")
                    head = _pread(fd, 5, pos + 4)
                else:
                    problem = _span_problem(loc, sizes, f)
                    if problem:
                        raise ExtractRefused(problem)
                    try:
                        head = _first_bytes(_SliceInput(source, loc.first_slice, loc.last_slice,
                                                        loc.start, loc.chunk_size), 5)
                    except _DataError as exc:
                        raise ExtractRefused(str(exc)) from exc
                try:
                    dsize = _dict_size(head, s.compression) if loc.compressed else 0
                except InnoError as exc:
                    raise ExtractRefused(str(exc)) from exc
                if dsize > DICT_CAP:
                    raise ExtractRefused("a data chunk uses an LZMA dictionary larger than "
                                         "1 GiB")
                ch = by_chunk[key] = _ChunkPlan(loc.start, loc.chunk_size, loc.compressed, dsize,
                                                first=loc.first_slice, last=loc.last_slice)
            if ch.compressed != loc.compressed:
                raise ExtractRefused("two files in one data chunk disagree on its compression")
            if ch.last != loc.last_slice:
                raise ExtractRefused("two files in one data chunk disagree on its last slice "
                                     "file; the setup is damaged")
            ch.items.append((li, targets[li]))
            total += loc.size * len(targets[li])
            count += len(targets[li])
    finally:
        if fd is not None:
            os.close(fd)
    held: dict[tuple[int, int], int] = {}  # bytes all of a chunk's files hold together
    ends: dict[tuple[int, int], int] = {}  # end of a chunk's last file, written or not
    for loc in s.locations:
        k = (loc.first_slice, loc.start)
        held[k] = held.get(k, 0) + loc.size
        ends[k] = max(ends.get(k, 0), loc.offset + loc.size)
    for key, ch in by_chunk.items():
        ch.items.sort(key=lambda it: (s.locations[it[0]].offset, s.locations[it[0]].size))
        end = 0
        for li, _ in ch.items:
            loc = s.locations[li]
            if loc.offset < end:
                raise ExtractRefused("two files overlap inside one data chunk; the setup is "
                                     "damaged")
            end = loc.offset + loc.size
        ch.decoded = end
        if end > max(ch.size, 1) * MAX_DECODE_RATIO and end > held.get(key, 0):
            raise ExtractRefused(f"a data chunk of {_size(ch.size)} would have to be decoded to "
                                 f"{_size(end)}, more than its files account for; the setup "
                                 "is damaged")
        ch.full = max(end, ends.get(key, 0))
        memory = max(memory, _lzma_dict(ch.dict_size, ch.full) if ch.dict_size else 0)
    chunks = sorted(by_chunk.values(), key=lambda c: (c.first, c.start))
    prefix = prefix_path(prefix_name, env)
    dest = prefix / "drive_c" / "Games" / prefix_name
    return InnoExtractPlan(installer, prefix_name, prefix, dest, s, choice, chunks, total, count,
                           memory, skipped, notes, folders, slices)


class _Stopped(Exception):
    pass


class _DataError(Exception):
    pass


class _FileInput:
    """A chunk's stored bytes inside setup.exe (from just after its marker); stops at size."""

    def __init__(self, fd: int, pos: int, size: int):
        self.fd = fd
        self.pos = pos
        self.left = size

    def read(self, n: int) -> bytes:
        if self.left <= 0:
            return b""
        data = _pread(self.fd, min(n, self.left), self.pos)
        if not data:
            raise _DataError("the setup file ended inside a data chunk")
        self.pos += len(data)
        self.left -= len(data)
        return data

    def close(self) -> None:
        pass


class _Slices:
    """The slice files the plan checked; each is opened again (O_NOFOLLOW) and its header
    checked again when a chunk reaches it, and must still have the planned size."""

    def __init__(self, files: dict[int, tuple[Path, int]], f: int):
        self.files = files
        self.f = f

    def open(self, n: int) -> tuple[int, int]:
        got = self.files.get(n)
        if got is None:
            raise _DataError("a data chunk reaches a slice file the plan did not check; the "
                             "setup is damaged")
        path, size = got
        try:
            fd, real = _open_slice(path, self.f)
        except InnoError as exc:
            raise _DataError(str(exc)) from exc
        if real != size:
            os.close(fd)
            raise _DataError(f"slice file {_clean(path.name, 60)} changed after the plan was "
                             "made")
        return fd, real


class _SliceInput:
    """A chunk's stored bytes read across slice files: from just after the chunk marker in
    slice `first` to the end of that file, then from just after the header of each next
    slice. Exactly size bytes, ending in slice `last`, or _DataError."""

    def __init__(self, slices: _Slices, first: int, last: int, start: int, size: int):
        self.slices = slices
        self.head = _slice_header_len(slices.f)
        self.cur = first
        self.last = last
        self.left = size
        self.fd, self.end = slices.open(first)
        try:
            if start < self.head or start + 4 + 1 > self.end:
                raise _DataError("a data chunk starts outside its slice file's data; the setup "
                                 "is damaged")
            if _pread(self.fd, 4, start) != CHUNK_MAGIC:
                raise _DataError("a data chunk does not start with its magic bytes; the setup "
                                 "or a slice file is damaged")
        except BaseException:
            self.close()
            raise
        self.pos = start + 4

    def read(self, n: int) -> bytes:
        if self.left <= 0:
            return b""
        while self.pos >= self.end:
            if self.cur >= self.last:
                raise _DataError("a data chunk runs past the end of the last slice file its "
                                 "location names; the setup or a slice file is damaged")
            self.close()
            self.cur += 1
            self.fd, self.end = self.slices.open(self.cur)
            self.pos = self.head
        data = _pread(self.fd, min(n, self.left, self.end - self.pos), self.pos)
        if not data:
            raise _DataError("a slice file ended early; it is damaged")
        self.pos += len(data)
        self.left -= len(data)
        if self.left == 0 and self.cur != self.last:
            raise _DataError("a data chunk ends before the last slice file its location names; "
                             "the setup or a slice file is damaged")
        return data

    def close(self) -> None:
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None


def _first_bytes(inp, n: int) -> bytes:
    """Up to n bytes from the start of an input (fewer only when the chunk is shorter); the
    input is closed."""
    try:
        out = b""
        while len(out) < n:
            got = inp.read(n - len(out))
            if not got:
                break
            out += got
        return out
    finally:
        inp.close()


class _ChunkReader:
    """A chunk's decoded bytes, read from its input CHUNK at a time; input stops at its size.
    An LZMA/LZMA2 chunk is decoded only up to `full` (the end of its last file), which its
    dictionary covers; a damaged chunk can then fail, but never with a match the dictionary
    lost."""

    def __init__(self, inp, method: int, compressed: bool, full: int):
        self.inp = inp
        self.method = method if compressed else 0
        self.tail = b""
        self.left = full
        if self.method == 0:
            self.dec = None
        elif self.method == 1:
            self.dec = zlib.decompressobj()
        elif self.method == 2:
            self.dec = bz2.BZ2Decompressor()
        else:
            self.dec = self._lzma(full)

    def _input(self, n: int | None = None) -> bytes:
        return self.inp.read(CHUNK if n is None else n)

    def _head(self, n: int) -> bytes:
        """n bytes (fewer only at the end of the chunk), across slice files if need be."""
        out = b""
        while len(out) < n:
            got = self._input(n - len(out))
            if not got:
                break
            out += got
        return out

    def _lzma(self, full: int):
        if self.method == 3:
            head = self._head(5)
            if len(head) < 5:
                raise _DataError("a data chunk is cut short")
            p = head[0]
            dsize = struct.unpack_from("<I", head, 1)[0]
            filt = {"id": lzma.FILTER_LZMA1, "dict_size": _lzma_dict(dsize, full),
                    "lc": p % 9, "lp": (p // 9) % 5, "pb": p // 45}
        else:
            head = self._head(1)
            if len(head) < 1:
                raise _DataError("a data chunk is cut short")
            try:
                dsize = lzma2_dict_size(head[0])
            except InnoError as exc:
                raise _DataError(str(exc)) from exc
            filt = {"id": lzma.FILTER_LZMA2, "dict_size": _lzma_dict(dsize, full)}
        try:
            return lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[filt])
        except (lzma.LZMAError, ValueError) as exc:
            raise _DataError("a data chunk has LZMA settings that cannot be used") from exc

    def read(self, k: int) -> bytes:
        """Up to k decoded bytes; b"" at the end of the chunk's data."""
        if self.dec is None:
            return self._input(k)
        try:
            if self.method == 1:
                while not self.dec.eof:
                    data = self.tail or self._input()
                    if not data:
                        return self.dec.flush()
                    out = self.dec.decompress(data, k)
                    self.tail = self.dec.unconsumed_tail
                    if out:
                        return out
                return b""
            idle = 0
            k = min(k, self.left)
            while not self.dec.eof and k > 0:
                data = self._input() if self.dec.needs_input else b""
                if self.dec.needs_input and not data:
                    return b""
                out = self.dec.decompress(data, max_length=k)
                if out:
                    self.left -= len(out)
                    return out
                idle = idle + 1 if not data else 0
                if idle > 2:
                    return b""
            return b""
        except (zlib.error, OSError, lzma.LZMAError, EOFError, ValueError) as exc:
            raise _DataError("a data chunk could not be decompressed; the setup is "
                             "damaged") from exc


_E8 = re.compile(b"[\xe8\xe9]")


def unfilter_calls(buf: bytearray, start: int) -> None:
    """Undo the x86 call/jump filter on one block (at most 64 KiB) of a file's bytes, in
    place; start is the block's offset inside the file."""
    lim = len(buf) - 4
    i = 0
    while i < lim:
        m = _E8.search(buf, i, lim)
        if m is None:
            return
        i = m.start()
        if buf[i + 4] in (0, 0xFF):
            addr = (start + i + 5) & 0xFFFFFF
            v = (buf[i + 1] | buf[i + 2] << 8 | buf[i + 3] << 16) - addr
            v &= 0xFFFFFFFF
            if v & 0x800000:
                buf[i + 4] ^= 0xFF
            buf[i + 1] = v & 0xFF
            buf[i + 2] = v >> 8 & 0xFF
            buf[i + 3] = v >> 16 & 0xFF
        i += 5


@dataclass
class InnoExtractResult:
    dest: Path
    files: int = 0
    folders: int = 0
    bytes_written: int = 0
    hash_errors: list[str] = field(default_factory=list)  # removed again
    removed: str | None = None  # a file cut short by Stop or an error (removed again)
    stopped: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not (self.stopped or self.error or self.hash_errors)

    def lines(self) -> list[str]:
        head = "stopped" if self.stopped else "ended with an error" if self.error else "finished"
        out = [(f"Extraction {head}: {_n(self.files, 'file', 'files')} "
                f"({_size(self.bytes_written)}) written to {self.dest}. Files extracted; "
                "nothing was installed or run.")]
        if self.error:
            out.append(f"error: {self.error}")
        if self.removed:
            out.append(f"incomplete file (removed again): {self.removed}")
        if self.hash_errors:
            out.append(f"{_n(len(self.hash_errors), 'file', 'files')} did not match their "
                       "checksum and were removed again: " + ", ".join(self.hash_errors[:5])
                       + (", ..." if len(self.hash_errors) > 5 else ""))
        if self.stopped or self.error:
            out.append("Files already written and checked were kept.")
        return out


def extract_inno_files(plan: InnoExtractPlan, cancel: threading.Event | None = None,
                       on_progress=None) -> InnoExtractResult:
    """Write the planned files. Raises ExtractRefused before anything is written; once the
    destination exists, every problem is reported in the result instead."""
    from gitfurl.extract import ExtractRefused, _make_dest

    problem = plan.problem()
    if problem:
        raise ExtractRefused(problem)
    try:
        fd, size = _open(plan.installer)
    except InnoError as exc:
        raise ExtractRefused(str(exc)) from exc
    try:
        try:
            again = read_fd(fd, plan.installer, size)
        except InnoError as exc:
            raise ExtractRefused(_clean(str(exc), 200)) from exc
        if (again.size, again.offset0, again.data_size, again.locations, again.files) != \
                (plan.setup.size, plan.setup.offset0, plan.setup.data_size,
                 plan.setup.locations, plan.setup.files):
            raise ExtractRefused("the installer changed after the plan was made; make the plan "
                                 "again")
        for path, planned in plan.slices.values():  # before anything is created
            try:
                sfd, real = _open_slice(path, _F[again.format])
            except InnoError as exc:
                raise ExtractRefused(_clean(str(exc), 200)) from exc
            os.close(sfd)
            if real != planned:
                raise ExtractRefused(f"slice file {_clean(path.name, 60)} changed after the plan "
                                     "was made; make the plan again")
        root = _make_dest(plan)
        return _write_all(plan, fd, root, cancel, on_progress)
    finally:
        os.close(fd)


class _Stream:
    """One chunk's decoded bytes, handed out in pieces; pos is the chunk offset reached."""

    def __init__(self, reader: _ChunkReader):
        self.reader = reader
        self.buf = b""
        self.at = 0
        self.pos = 0

    def pull(self, n: int) -> bytes:
        if self.at == len(self.buf):
            self.buf, self.at = self.reader.read(CHUNK), 0
            if not self.buf:
                raise _DataError("a data chunk ended before all of its files were complete")
        piece = self.buf[self.at:self.at + n]
        self.at += len(piece)
        self.pos += len(piece)
        return piece


def _emit(data, h, fds: list[int]) -> None:
    h.update(data)
    for out in fds:
        view = memoryview(data)
        while view:
            view = view[os.write(out, view):]


def _write_file(stream: _Stream, loc: Location, fds: list[int], stop) -> bytes:
    """Copy one file's bytes (undoing the call filter if needed); returns its digest."""
    h = hashlib.sha1() if len(loc.digest) == 20 else hashlib.sha256()
    left, block, done = loc.size, bytearray(), 0
    while left:
        stop()
        piece = stream.pull(min(CHUNK, left))
        left -= len(piece)
        if not loc.call_filter:
            _emit(piece, h, fds)
            continue
        block += piece
        while len(block) >= FILTER_BLOCK or (not left and block):
            part = block[:FILTER_BLOCK]
            del block[:FILTER_BLOCK]
            unfilter_calls(part, done)
            done += len(part)
            _emit(part, h, fds)
    secs = (loc.filetime - 116444736000000000) / 10_000_000
    for out in fds:
        try:  # through the open file: never another file of that name
            os.utime(out, (secs, secs))
        except (OSError, OverflowError, ValueError):
            pass
    return h.digest()


def _write_all(plan: InnoExtractPlan, fd: int, root: int, cancel, on_progress
               ) -> InnoExtractResult:
    from gitfurl.extract import _Tree

    s = plan.setup
    res = InnoExtractResult(plan.dest)
    tree = _Tree(root, plan.folders)

    def stop():
        if cancel is not None and cancel.is_set():
            raise _Stopped

    def remove(targets) -> None:
        """Remove files this run created (O_EXCL) and did not check, or that failed."""
        for t in targets:
            try:
                os.unlink(t.name, dir_fd=tree.fd(t.folder))
            except OSError:
                pass

    current: list[Target] = []  # files created by this run and not checked yet
    source = _Slices(plan.slices, _F[s.format]) if s.slice_layout else None
    try:
        for ch in plan.chunks:
            stop()
            if source is None:
                inp = _FileInput(fd, s.offset1 + ch.start + 4, ch.size)
            else:
                inp = _SliceInput(source, ch.first, ch.last, ch.start, ch.size)
            try:
                stream = _Stream(_ChunkReader(inp, s.compression, ch.compressed, ch.full))
                for li, targets in ch.items:
                    stop()
                    loc = s.locations[li]
                    while stream.pos < loc.offset:
                        stop()
                        stream.pull(min(CHUNK, loc.offset - stream.pos))
                    fds: list[int] = []
                    try:
                        for t in targets:
                            fds.append(os.open(t.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                               | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                                               dir_fd=tree.fd(t.folder)))
                            current.append(t)
                        digest = _write_file(stream, loc, fds, stop)
                    finally:
                        for out in fds:
                            os.close(out)
                    current.clear()
                    if digest != loc.digest:
                        remove(targets)
                        res.hash_errors.extend(t.shown for t in targets)
                    else:
                        res.files += len(targets)
                        res.bytes_written += loc.size * len(targets)
                    if on_progress is not None:
                        on_progress(res.bytes_written, plan.total_size)
            finally:
                inp.close()
    except _Stopped:
        res.stopped = True
    except KeyboardInterrupt:
        res.stopped = True
    except _DataError as exc:
        res.error = str(exc)
    except OSError as exc:
        res.error = f"writing failed ({exc.strerror or exc})"
    except MemoryError:
        res.error = "not enough memory"
    except Exception as exc:  # noqa: BLE001 - once the destination exists, report, never raise
        res.error = f"unexpected error ({type(exc).__name__})"
    finally:
        if current:
            res.removed = ", ".join(t.shown for t in current)
            remove(current)
        res.folders = tree.made
        tree.close()
        os.close(root)
    return res
