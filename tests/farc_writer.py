"""A small FreeArc writer for the tests: archives are built in memory, as bytes.

Storing, and raw LZMA1 from Python's own lzma module. The bytes are written to a temporary
folder only for a test; no archive file is kept in the repository. It is written from the
same reading of the format as gitfurl/farc.py, so agreement between the two does not show
agreement with FreeArc itself.
"""

import lzma
import random
import zlib

SIG = b"ArC\x01"


# --- in-test writer -------------------------------------------------------------------------
def varint(v: int, wide: bool = False) -> bytes:
    """FreeArc integer: n bytes carry n-1 low 1-bits, a 0-bit, then 7n bits of value."""
    if wide or v >= 1 << 56:
        return b"\xff" + v.to_bytes(8, "little")
    n = 1
    while v >= 1 << (7 * n):
        n += 1
    return ((v << n) | ((1 << (n - 1)) - 1)).to_bytes(n, "little")


def cstr(s: str | bytes) -> bytes:
    return (s if isinstance(s, bytes) else s.encode("utf-8")) + b"\0"


def u32(v: int) -> bytes:
    return (v & 0xFFFFFFFF).to_bytes(4, "little")


def lzma_opts(method: str) -> dict:
    opts = {"dict_size": 1 << 20, "lc": 3, "lp": 0, "pb": 2}
    for p in method.split(":")[1:]:
        if p[:2] in ("lc", "lp", "pb"):
            opts[p[:2]] = int(p[2:])
        elif p.endswith("mb"):
            opts["dict_size"] = min(int(p[:-2]) << 20, 64 << 20)
    return opts


def pack(method: str, data: bytes) -> bytes:
    if method.startswith("lzma") and "+" not in method:
        f = [{"id": lzma.FILTER_LZMA1, **lzma_opts(method)}]
        return lzma.compress(data, format=lzma.FORMAT_RAW, filters=f)
    return data  # storing, or a method the reader only names (its bytes are never decoded)


def local_descriptor(btype: int, method: str, orig: int, comp: int, crc: int,
                     good_crc: bool = True) -> bytes:
    d = SIG + varint(btype) + cstr(method) + varint(orig) + varint(comp) + u32(crc)
    return d + u32(zlib.crc32(d) ^ (0 if good_crc else 1))


def encode_dir(blocks, folders, offsets, compsizes, wide=False) -> bytes:
    """blocks: [(method, [(dirnum, name, data, is_dir, declared_size_or_None)])]."""
    files = [f for _, fs in blocks for f in fs]
    out = varint(len(blocks))
    out += b"".join(varint(len(fs)) for _, fs in blocks)
    out += b"".join(cstr(m) for m, _ in blocks)
    out += b"".join(varint(o) for o in offsets)
    out += b"".join(varint(c) for c in compsizes)
    out += varint(len(folders)) + b"".join(cstr(d) for d in folders)
    out += b"".join(cstr(f[1]) for f in files)
    out += b"".join(varint(f[0]) for f in files)
    out += b"".join(varint(len(f[2]) if f[4] is None else f[4], wide) for f in files)
    out += b"".join(u32(1_700_000_000) for _ in files)
    out += b"".join(bytes([1 if f[3] else 0]) for f in files)
    out += b"".join(u32(zlib.crc32(f[2])) for f in files)
    return out


def F(name, data=b"", dirnum=0, is_dir=False, size=None):
    return (dirnum, name, data, is_dir, size)


def make_archive(blocks, folders=("",), sfx=b"", locked=False, index="storing",
                 dir_index=None, dir_raw=None, footer_raw=None, offsets=None,
                 dir_orig=None, footer_orig=None, wide=False, dir_mutator=None, omit_dir=False,
                 bad_dir_crc=False, bad_desc_crc=False, footer_tail=b"") -> bytes:
    out = bytearray(sfx)
    header_pos = len(out)
    header = SIG + b"\0\0\0\0"
    out += header
    positions, compsizes = [], []
    for method, fs in blocks:
        comp = pack(method, b"".join(f[2] for f in fs))
        positions.append(len(out))
        compsizes.append(len(comp))
        out += comp
    dir_pos = len(out)
    rel = offsets or [dir_pos - p for p in positions]
    dplain = dir_raw if dir_raw is not None else encode_dir(blocks, folders, rel, compsizes,
                                                            wide)
    if dir_mutator is not None:
        dplain = dir_mutator(dplain)
    dmethod = dir_index or index
    dcomp = pack(dmethod, dplain)
    out += dcomp
    dcrc = zlib.crc32(dplain) ^ (1 if bad_dir_crc else 0)
    out += local_descriptor(3, dmethod, len(dplain), len(dcomp), dcrc)
    fpos = len(out)
    if footer_raw is None:
        fplain = varint(1 if omit_dir else 2)
        fplain += varint(1) + cstr("storing") + varint(fpos - header_pos) + varint(len(header))
        fplain += varint(len(header)) + u32(zlib.crc32(header))
        if not omit_dir:
            fplain += varint(3) + cstr(dmethod) + varint(fpos - dir_pos)
            fplain += varint(dir_orig if dir_orig is not None else len(dplain))
            fplain += varint(len(dcomp)) + u32(dcrc)
        fplain += bytes([1 if locked else 0]) + varint(0)
    else:
        fplain = footer_raw
    fcomp = pack(index, fplain)
    out += fcomp + footer_tail
    forig = footer_orig if footer_orig is not None else len(fplain)
    out += local_descriptor(4, index, forig, len(fcomp), zlib.crc32(fplain),
                            good_crc=not bad_desc_crc)
    return bytes(out)


def sample_blocks(method="storing"):
    rnd = random.Random(7)
    a = bytes(rnd.getrandbits(8) for _ in range(3000))
    b = b"hello, world\n" * 50
    return [(method, [F(".", is_dir=True), F("a.bin", a), F("b.txt", b, dirnum=1),
                      F("sub", is_dir=True)])], ("", "sub")
