"""A small Inno Setup setup.exe writer for the tests: installers are built in memory, as bytes.

It writes a minimal Windows program (one resource section holding the loader's offset
table as RCDATA 11111), the file data (setup-1: chunks of stored, zlib, bzip2, LZMA1 or LZMA2
data, solid or one chunk per file, with the x86 call filter where asked) and the setup data
(setup-0: format ID, encryption header from 6.5.0, the header block and the file location
block, each LZMA1-compressed or stored), followed by a few bytes that stand in for the
compressed Setup program. No real Inno Setup program or installer bytes are used.

Written from the same clean-room specification as gitfurl/inno.py, so agreement between the
two does not show agreement with Inno Setup itself; tests/test_inno.py also checks the
reader against hand-made vectors. The bytes are written to a temporary folder only for a
test; no installer file is kept in the repository.
"""

from __future__ import annotations

import bz2
import hashlib
import lzma
import re
import struct
import zlib
from dataclasses import dataclass, field

FORMATS = ("5.5.0", "5.5.6", "5.5.7", "6.0.0", "6.1.0", "6.3.0", "6.4.0.1", "6.4.2", "6.4.3",
           "6.5.0", "6.5.2", "6.6.0", "6.6.1", "6.7.0", "7.0.0.1", "7.0.0.3", "7.1.1")
F = {name: i for i, name in enumerate(FORMATS)}
MAGIC = bytes.fromhex("72446c507453cde6d77b0b2a")

FILE_BITS_OLD = {"delete after install": 3, "skip if source does not exist": 9,
                 "custom destination name": 12, "only if does not exist": 16, "do not copy": 19,
                 "download": 32, "extract archive": 33}
FILE_BITS_NEW = dict(FILE_BITS_OLD, download=30, **{"extract archive": 31})
RUN_BITS = {"shell exec": 0, "post-install": 2, "unchecked": 3, "skip if silent": 4,
            "skip if not silent": 5}


def u8(v: int) -> bytes:
    return struct.pack("<B", v)


def u16(v: int) -> bytes:
    return struct.pack("<H", v & 0xFFFF)


def u32(v: int) -> bytes:
    return struct.pack("<I", v & 0xFFFFFFFF)


def i32(v: int) -> bytes:
    return struct.pack("<i", v)


def i64(v: int) -> bytes:
    return struct.pack("<q", v)


def text(s: str) -> bytes:
    raw = s.encode("utf-16-le")
    return u32(len(raw)) + raw


def blob(b: bytes) -> bytes:
    return u32(len(b)) + b


def bits(names, table: dict, nbytes: int) -> bytes:
    v = 0
    for n in names:
        v |= 1 << table[n]
    return v.to_bytes(nbytes, "little")


def crc(b: bytes) -> int:
    return zlib.crc32(b)


# --- the x86 call filter, encoding direction ---------------------------------------------

_E8 = re.compile(b"[\xe8\xe9]")


def filter_calls(data: bytes) -> bytes:
    """Apply the call filter per 64 KiB block (what the compiler does before compression)."""
    out = bytearray(data)
    for start in range(0, len(out), 1 << 16):
        end = min(start + (1 << 16), len(out))
        buf = out[start:end]
        lim = len(buf) - 4
        i = 0
        while i < lim:
            m = _E8.search(buf, i, lim)
            if m is None:
                break
            i = m.start()
            if buf[i + 4] in (0, 0xFF):
                v = buf[i + 1] | buf[i + 2] << 8 | buf[i + 3] << 16
                if v & 0x800000:
                    buf[i + 4] ^= 0xFF
                v = (v + ((start + i + 5) & 0xFFFFFF)) & 0xFFFFFF
                buf[i + 1:i + 4] = bytes((v & 0xFF, v >> 8 & 0xFF, v >> 16))
            i += 5
        out[start:end] = buf
    return bytes(out)


# --- compression ----------------------------------------------------------------------------

def lzma1(data: bytes, dict_size: int = 1 << 16) -> bytes:
    """5-byte properties (lc 3, lp 0, pb 2) and a raw LZMA1 stream."""
    raw = lzma.compress(data, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA1, "dict_size": dict_size, "lc": 3, "lp": 0, "pb": 2}])
    return bytes([93]) + u32(dict_size) + raw


def lzma2(data: bytes) -> bytes:
    """1 property byte (16: 1 MiB) and a raw LZMA2 stream."""
    return bytes([16]) + lzma.compress(data, format=lzma.FORMAT_RAW, filters=[
        {"id": lzma.FILTER_LZMA2, "dict_size": 1 << 20}])


def pack(method: int, data: bytes) -> bytes:
    if method == 1:
        return zlib.compress(data)
    if method == 2:
        return bz2.compress(data)
    if method == 3:
        return lzma1(data, 1 << 20)
    if method == 4:
        return lzma2(data)
    raise ValueError(method)


def frame_block(content: bytes, f: int, compressed: bool) -> bytes:
    """A header block: CRC, size, compressed flag, then 4096-byte chunks with CRCs."""
    data = lzma1(content) if compressed else content
    chunks = b"".join(u32(crc(data[i:i + 4096])) + data[i:i + 4096]
                      for i in range(0, len(data), 4096))
    head = (i64(len(chunks)) if f >= F["6.7.0"] else u32(len(chunks))) + u8(int(compressed))
    return u32(crc(head)) + head + chunks


# --- records --------------------------------------------------------------------------------

@dataclass
class WFile:
    dest: str
    data: bytes = b""
    kind: str = "stored"  # stored, external, download, uninstaller
    source: str = ""
    components: str = ""
    tasks: str = ""
    languages: str = ""
    check: str = ""
    flags: tuple = ()
    call_filter: bool = False
    encrypted: bool = False
    location: int | None = None  # share another file's location (index into the file list)
    bad_hash: bool = False


@dataclass
class WRun:
    filename: str
    parameters: str = ""
    flags: tuple = ()
    wait: int = 0
    show: int = 1
    components: str = ""
    tasks: str = ""
    languages: str = ""
    check: str = ""
    verb: str = ""
    description: str = ""


@dataclass
class Spec:
    fmt: str = "6.7.0"
    variant: bool = False  # the 5.5.0 layout with one extra header byte
    compression: int = 3  # file data: 0 stored, 1 zlib, 2 bzip2, 3 lzma, 4 lzma2
    solid: bool = False
    header_compressed: bool = True
    password: bool = False
    encryption_flag: bool = False  # the "encryption used" header flag (up to 6.4.3)
    decrypt_dll: bool = True  # with encryption_flag up to 6.3.0: the DLL item in the tail
    encryption_header: int = 0  # 6.5.0 and later
    spanning: bool = False
    slices_per_disk: int = 1
    first_slice: int = 0  # of every file location (with spanning)
    app_name: str = "Test App"
    app_version: str = "1.0"
    default_dir: str = "{autopf}\\Test App"
    privileges: int = 3
    arch_allowed: str = ""
    arch_64: str = ""
    languages: list = field(default_factory=lambda: [("english", 0x0409)])
    types: list = field(default_factory=list)  # (name, kind, custom)
    components: list = field(default_factory=list)  # (name, types, options)
    tasks: list = field(default_factory=list)  # (name, options)
    dirs: list = field(default_factory=list)  # names
    files: list = field(default_factory=list)  # WFile
    icons: int = 0
    ini: int = 0
    registry: int = 0
    run: list = field(default_factory=list)  # WRun
    uninstall_run: list = field(default_factory=list)  # WRun
    code: bytes = b""
    sevenzip: bool = False  # 6.5.0 and later: a 7-Zip library name and DLL item
    overlay: bytes = b""  # bytes after the setup data (e.g. a signature)
    unknown_header_flag: bool = False


def _ver() -> bytes:
    return bytes(10)


def _hdr_strings(sp: Spec, f: int) -> list[str]:
    n = {F["5.5.0"]: 27}.get(f)
    if n is None:
        n = 28 if f <= F["5.5.7"] else 30 if f <= F["6.1.0"] else 32 if f <= F["6.4.0.1"] \
            else 33 if f <= F["6.4.3"] else 34 if f <= F["6.6.1"] else 39  # 7.1.1: one removed
    s = [""] * max(n, 39)
    s[0], s[9], s[10], s[11], s[13] = sp.app_name, sp.app_version, sp.default_dir, sp.app_name, \
        "{app}"
    s[24] = s[25] = "yes"
    if f >= F["6.3.0"]:
        s[30], s[31] = sp.arch_allowed, sp.arch_64
    if f >= F["6.5.0"] and sp.sevenzip:
        s[33] = "7z.dll"
    if f >= F["6.7.0"]:
        s[34:39] = ["yes"] * 5
    s = s[:n]
    if f >= F["7.1.1"]:
        del s[12]
    return s


def _wizard(f: int) -> int:
    if f <= F["5.5.6"]:
        return 40
    if f == F["5.5.7"]:
        return 37
    if f <= F["6.3.0"]:
        return 46
    if f <= F["6.4.3"]:
        return 58
    return {F["6.5.0"]: 10, F["6.5.2"]: 18, F["6.6.0"]: 26, F["6.6.1"]: 27}.get(f, 37)


def _flags(sp: Spec, f: int) -> bytes:
    if f <= F["6.1.0"]:
        nbytes, pw, enc = 6, 10, (37 if f <= F["5.5.7"] else 36)
    elif f == F["6.3.0"]:
        nbytes, pw, enc = 7, 10, 36
    elif f <= F["6.4.3"]:
        nbytes, pw, enc = 6, 6, 31
    elif f <= F["6.6.1"]:
        nbytes, pw, enc = 6, 6, None
    else:
        nbytes, pw, enc = 8, 6, None
    v = 1 << 1  # create app dir
    if sp.password:
        v |= 1 << pw
    if sp.encryption_flag and enc is not None:
        v |= 1 << enc
    if sp.unknown_header_flag:
        v |= 1 << (nbytes * 8 - 1)
    return v.to_bytes(nbytes, "little")


def _six(components="", tasks="", languages="", check="") -> list[str]:
    return [components, tasks, languages, check, "", ""]


def build_block1(sp: Spec, nlocations: int) -> bytes:
    f = F[sp.fmt]
    out = bytearray()
    for s in _hdr_strings(sp, f):
        out += text(s)
    out += blob(b"") + blob(b"") + blob(b"") + blob(sp.code)
    counts = [len(sp.languages), 0, 0, len(sp.types), len(sp.components), len(sp.tasks),
              len(sp.dirs)]
    if f >= F["6.5.0"]:
        counts.append(0)
    counts += [len(sp.files), nlocations, sp.icons, sp.ini, sp.registry, 0, 0, len(sp.run),
               len(sp.uninstall_run)]
    out += b"".join(i32(c) for c in counts)
    if f >= F["7.0.0.3"]:
        out += u32(0)
    out += _ver() + _ver() + bytes(_wizard(f))
    out += i64(0) + i32(sp.slices_per_disk) + u8(0) + u8(0) + u8(sp.privileges)
    if f >= F["6.0.0"]:
        out += u8(0)
    out += u8(0) + u8(0) + u8(sp.compression)
    if f <= F["6.1.0"]:
        out += u8(0) + u8(0)
    out += u8(0) + u8(0) + i64(0)
    if sp.variant:
        out += u8(0)
    out += _flags(sp, f)
    # entry lists
    for name, lid in sp.languages:
        n = 6 if f <= F["6.5.2"] else 4
        out += b"".join(text(s) for s in [name, name.title()] + [""] * (n - 2))
        out += blob(b"messages") + blob(b"") + blob(b"") + blob(b"")
        out += (u32(lid) if f <= F["6.5.2"] else u16(lid)) + bytes(17)
    for name, kind, custom in sp.types:
        out += text(name) + text(name.title()) + text("") + text("")
        out += _ver() + _ver() + u8(int(custom)) + u8(kind) + i64(0)
    for name, types, options in sp.components:
        out += text(name) + text(name.title()) + text(types) + text("") + text("")
        out += i64(0) + (i32(name.count("\\")) if f < F["6.7.0"] else u8(name.count("\\")))
        out += u8(0) + _ver() + _ver() + u8(options) + i64(0)
    for name, options in sp.tasks:
        out += text(name) + text(name.title()) + text("") + text("") + text("") + text("")
        out += (i32(0) if f < F["6.7.0"] else u8(0)) + u8(0) + _ver() + _ver() + u8(options)
    for name in sp.dirs:
        out += b"".join(text(s) for s in [name] + _six()) + i32(0) + _ver() + _ver() + \
            struct.pack("<h", -1) + u8(0)
    file_bits = FILE_BITS_NEW if f >= F["7.0.0.3"] else FILE_BITS_OLD
    nopt = 4 if f <= F["6.4.3"] else 5 if f <= F["6.6.1"] else 8
    for k, wf in enumerate(sp.files):
        strings = [wf.source, wf.dest, "", "",
                   *_six(wf.components, wf.tasks, wf.languages, wf.check)[:6]]
        if f >= F["6.5.0"]:
            strings += [""] * 5
        out += b"".join(text(s) for s in strings)
        if f >= F["6.5.0"]:
            out += blob(b"")
            out += bytes(32) + u8(0)
        out += _ver() + _ver()
        loc = -1 if wf.kind != "stored" else _location_of(sp, k)
        flags = set(wf.flags) | ({"download"} if wf.kind == "download" else set())
        out += i32(loc) + i32(0) + i64(len(wf.data)) + struct.pack("<h", -1)
        if f >= F["7.0.0.3"]:
            out += u8(0)
        out += bits(flags, file_bits, nopt) + u8(1 if wf.kind == "uninstaller" else 0)
    for k in range(sp.icons):
        out += b"".join(text(s) for s in [f"{{group}}\\Icon{k}", "{app}\\a.exe", "", "", "", "",
                                          *_six(), ""])
        if f >= F["6.1.0"]:
            out += bytes(16)
        out += _ver() + _ver() + i32(0) + i32(1) + u8(0) + u16(0) + u8(0)
    for k in range(sp.ini):
        out += b"".join(text(s) for s in ["{app}\\a.ini", "S", f"K{k}", "V", *_six()])
        out += _ver() + _ver() + u8(16)
    for k in range(sp.registry):
        out += b"".join(text(s) for s in ["Software\\Test", f"V{k}", "{app}", *_six()])
        out += _ver() + _ver() + u32(0x80000001) + struct.pack("<h", -1) + u8(1)
        if f >= F["7.0.0.3"]:
            out += u8(0)
        out += u16(0)
    for r in [*sp.run, *sp.uninstall_run]:
        strings = [r.filename, r.parameters, "", "", "", r.verb, r.description,
                   *_six(r.components, r.tasks, r.languages, r.check)]
        if f >= F["7.0.0.1"]:
            strings.append("")
        out += b"".join(text(s) for s in strings)
        out += _ver() + _ver() + i32(r.show) + u8(r.wait)
        if f >= F["7.0.0.3"]:
            out += u8(0)
        out += bits(r.flags, RUN_BITS, 2)
    # tail: wizard images, helper DLLs
    image = blob(b"BM-image")
    if f <= F["5.5.7"]:
        out += image + image
    else:
        lists = 2 if f <= F["6.5.2"] else 4 if f <= F["6.6.1"] else 6
        out += (i32(1) + image) * lists
    if sp.compression in (1, 2):
        out += blob(b"decompressor DLL stand-in")
    if sp.encryption_flag and f <= F["6.3.0"] and sp.decrypt_dll:
        out += blob(b"decryption DLL stand-in")
    if f >= F["6.5.0"] and sp.sevenzip:
        out += blob(b"7-zip DLL stand-in")
    return bytes(out)


def _location_of(sp: Spec, k: int) -> int:
    """Location index of stored file k (files sharing a location reuse it)."""
    idx = -1
    for j, wf in enumerate(sp.files[:k + 1]):
        if wf.kind != "stored":
            continue
        if wf.location is None:
            idx += 1
            if j == k:
                return idx
        elif j == k:
            return _location_of(sp, wf.location)
    return idx


def build(sp: Spec, block1_hook=None, block2_hook=None) -> tuple[bytes, dict]:
    """The setup.exe bytes and a dict of facts the tests use (offsets, locations).

    The hooks, when given, change the content of the header block or the file location block
    before it is framed, so the CRC-32 values written are those of the changed bytes."""
    f = F[sp.fmt]
    stored = [wf for wf in sp.files if wf.kind == "stored" and wf.location is None]
    # file data: one chunk per file, or one solid chunk
    groups = [stored] if sp.solid else [[wf] for wf in stored]
    setup1 = bytearray()
    locs = []
    for group in groups:
        if not group:
            continue
        raw = b"".join(filter_calls(wf.data) if wf.call_filter and sp.compression else wf.data
                       for wf in group)
        packed = pack(sp.compression, raw) if sp.compression else raw
        start = len(setup1)
        setup1 += b"zlb\x1a" + packed
        off = 0
        for wf in group:
            locs.append((start, off, len(wf.data), len(packed), wf))
            off += len(wf.data)
    loc_bytes = bytearray()
    for start, off, size, csize, wf in locs:
        digest = (hashlib.sha1 if f <= F["6.3.0"] else hashlib.sha256)(wf.data).digest()
        if wf.bad_hash:
            digest = bytes(len(digest))
        loc_bytes += i32(sp.first_slice) + i32(sp.first_slice)
        loc_bytes += i64(start) if f >= F["6.5.2"] else i32(start)
        loc_bytes += i64(off) + i64(size) + i64(csize) + digest
        loc_bytes += struct.pack("<Q", 0x01D9000000000000) + u32(0) + u32(0)
        compressed = bool(sp.compression)
        if f <= F["6.4.2"]:
            fl = (wf.call_filter and compressed) << 4 | wf.encrypted << 6 | compressed << 7
            loc_bytes += u16(fl)
        else:
            fl = (wf.call_filter and compressed) << 2 | wf.encrypted << 3 | compressed << 4
            loc_bytes += u8(fl)
        if F["6.3.0"] <= f <= F["6.4.2"]:
            loc_bytes += u8(0)
    block1 = build_block1(sp, len(locs))
    if block1_hook is not None:
        block1 = bytes(block1_hook(block1))
    if block2_hook is not None:
        loc_bytes = bytearray(block2_hook(bytes(loc_bytes)))
    ident = (f"Inno Setup Setup Data ({sp.fmt})" + (" (u)" if f <= F["6.1.0"] else "")).encode()
    setup0 = bytearray(ident.ljust(64, b"\0"))
    if f >= F["6.5.0"]:
        enc = u8(sp.encryption_header) + bytes(48)
        setup0 += u32(crc(enc)) + enc
    setup0 += frame_block(block1, f, sp.header_compressed)
    setup0 += frame_block(bytes(loc_bytes), f, sp.header_compressed)
    table_v = 1 if f <= F["6.4.3"] else 2
    tsize = 44 if table_v == 1 else 64
    loader_len = _loader_len(tsize)
    if sp.spanning:
        setup1 = bytearray()
    offset1 = 0 if sp.spanning else loader_len
    offset0 = loader_len + len(setup1)
    offset_exe = offset0 + len(setup0)
    exe = b"compressed Setup program stand-in" * 4
    total = offset_exe + len(exe)
    if table_v == 1:
        body = MAGIC + u32(1) + struct.pack("<IIIIII", total, offset_exe, len(exe), crc(exe),
                                            offset0, offset1)
    else:
        body = MAGIC + u32(2) + struct.pack("<qqIIqqI", total, offset_exe, len(exe), crc(exe),
                                            offset0, offset1, 0)
    table = body + u32(crc(body))
    loader = pe_with_table(table)
    assert len(loader) == loader_len
    data = loader + bytes(setup1) + bytes(setup0) + exe + sp.overlay
    facts = {"offset0": offset0, "offset1": offset1, "offset_exe": offset_exe,
             "loader": loader_len, "block1": block1, "locations": len(locs)}
    return data, facts


def _loader_len(tsize: int) -> int:
    return len(pe_with_table(bytes(tsize)))


def pe_with_table(table: bytes, resource_id: int = 11111) -> bytes:
    """A minimal 32-bit Windows program whose only section holds the resource tree
    root -> RCDATA (10) -> resource_id -> language 1033 -> the table."""
    rva = 0x1000
    res = bytearray()

    def directory(entry_id: int, target: int) -> bytes:
        return bytes(12) + u16(0) + u16(1) + u32(entry_id) + u32(target)

    res += directory(10, 0x80000000 | 24)
    res += directory(resource_id, 0x80000000 | 48)
    res += directory(1033, 72)
    res += u32(rva + 88) + u32(len(table)) + u32(0) + u32(0)
    res += table
    raw = bytes(res) + bytes(-len(res) % 512)
    dos = bytearray(64)
    dos[0:2] = b"MZ"
    struct.pack_into("<I", dos, 0x3C, 64)
    coff = b"PE\0\0" + struct.pack("<HHIIIHH", 0x14C, 1, 0, 0, 0, 224, 0x102)
    opt = bytearray(224)
    struct.pack_into("<H", opt, 0, 0x10B)
    struct.pack_into("<I", opt, 92, 16)
    struct.pack_into("<II", opt, 96 + 16, rva, len(res))
    sec = struct.pack("<8sIIIIIIHHI", b".rsrc", len(res), rva, len(raw), 0x200, 0, 0, 0, 0,
                      0x40000040)
    head = bytes(dos) + coff + bytes(opt) + sec
    head += bytes(0x200 - len(head))
    return head + raw


# --- a compiled [Code] container -----------------------------------------------------------

def code_blob(strings: list[str], imports: list[tuple[str, str]] = (), build_no: int = 23,
              ansi: list[str] = ()) -> bytes:
    """A Pascal Script container with a Unicode string type, an integer type, an ANSI string
    type, one internal procedure whose code pushes each string as a constant operand, and
    DLL imports ("files:helper.dll", "Function")."""
    types = [28, 6, 10]
    body = bytearray()
    for t in types:
        body += u8(t)
        if build_no >= 21:
            body += u32(0)
    code = bytearray()
    for s in strings:
        raw = s.encode("utf-16-le")
        code += b"\x01" + u32(0) + u32(len(raw) // 2) + raw + b"\x09"
    for s in ansi:
        raw = s.encode("cp1252")
        code += b"\x01" + u32(2) + u32(len(raw)) + raw + b"\x09"
    code += b"\x09"
    procs = bytearray()
    for dll, fn in imports:
        decl = b"dll:" + dll.encode() + b"\0" + fn.encode() + b"\0" + b"\x00\x00"
        procs += u8(3) + u8(len(fn)) + fn.encode() + u32(len(decl)) + decl
    head_len = 28
    code_off = head_len + len(body) + len(procs) + 9  # the internal procedure record
    procs += u8(0) + u32(code_off) + u32(len(code))
    header = u32(0x53504649) + u32(build_no) + u32(len(types)) + u32(len(imports) + 1) + \
        u32(0) + u32(0) + u32(0)
    return header + bytes(body) + bytes(procs) + bytes(code)
