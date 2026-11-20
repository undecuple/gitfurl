"""A small FreeArc writer for the tests: archives are built in memory, as bytes.

Storing, raw LZMA1 from Python's own lzma module, and test-only encoders for exe, delta and
rep (ENCODERS), written from the same clean-room specifications as gitfurl/farc_codecs.py,
so agreement between the two does not show agreement with FreeArc itself. Chains are encoded
left to right. The bytes are written to a temporary folder only for a test; no archive file
is kept in the repository.
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
    """Encode data with a chain of known methods, left to right. A method the writer does
    not know leaves the data as it is (the reader only names it; its bytes are never
    decoded)."""
    stages = method.split("+")
    if not all(st.split(":")[0] in ENCODERS for st in stages):
        return data
    for st in stages:
        data = ENCODERS[st.split(":")[0]](data, st)
    return data


def _lzma(data: bytes, method: str) -> bytes:
    f = [{"id": lzma.FILTER_LZMA1, **lzma_opts(method)}]
    return lzma.compress(data, format=lzma.FORMAT_RAW, filters=f)


# --- exe (x86 branch converter), encode direction ------------------------------------------
EXE_ROUND = 262144


def x86_walk(buf: bytearray, base: int, rejected: set, decode: bool) -> int:
    """One piece of the x86 walk, a byte at a time. rejected: stream positions of rejected
    opcodes (updated). Returns where the walk stopped."""
    n, i = len(buf), 0
    while i + 4 < n:
        if buf[i] not in (0xE8, 0xE9):
            i += 1
            continue
        at = base + i
        hist = [t for t in (1, 2, 3) if at - t in rejected]
        ok = len(hist) < 2
        if ok and hist:
            ok = buf[i + 1 + 3 - hist[0]] not in (0, 0xFF)
        if ok:
            ok = buf[i + 4] in (0, 0xFF)
        if not ok:
            rejected.add(at)
            i += 1
            continue
        v = int.from_bytes(buf[i + 1:i + 5], "little")
        a = (at + 5) % (1 << 32)
        w = (v - a if decode else v + a) % (1 << 32)
        if hist:
            q = 3 - hist[0]
            while ((w >> (8 * q)) & 0xFF) in (0, 0xFF):
                v = w ^ ((1 << (8 * (q + 1))) - 1)
                w = (v - a if decode else v + a) % (1 << 32)
        buf[i + 1:i + 4] = (w % (1 << 24)).to_bytes(3, "little")
        buf[i + 4] = 0xFF if (w >> 24) & 1 else 0
        i += 5
    return max(i, n - 4)


def exe_encode(data: bytes, method: str = "exe", decode: bool = False) -> bytes:
    """FreeArc's call schedule: rounds of EXE_ROUND bytes with 0-4 bytes carried; a round of
    5 bytes or fewer passes unchanged."""
    out, carry, base, at, rejected = bytearray(), b"", 0, 0, set()
    while True:
        new = data[at:at + EXE_ROUND - len(carry)]
        at += len(new)
        buf = bytearray(carry + new)
        if not buf:
            return bytes(out)
        if len(buf) <= 5:
            out += buf
            base += len(buf)
            carry = b""
            continue
        done = x86_walk(buf, base, rejected, decode)
        out += buf[:done]
        carry = bytes(buf[done:])
        base += done


# --- delta (binary tables), encode direction -----------------------------------------------
def delta_block(data: bytes, tables=()) -> bytes:
    """One delta block. tables: [(gap, shape, rows)], in order, as in the stream."""
    buf = bytearray(data)
    pos = 0
    for gap, shape, rows in tables:
        width = shape.bit_length() - 1
        start = pos + gap
        end = start + width * rows
        kept = [c for c in range(width) if shape >> c & 1]
        diffed = [c for c in range(width) if not shape >> c & 1]
        orig = bytes(buf[start:end])
        runs = []
        for c in diffed:
            if runs and runs[-1][0] + runs[-1][1] == c:
                runs[-1][1] += 1
            else:
                runs.append([c, 1])
        for r in range(1, rows):
            for c0, n in runs:
                a = start + r * width + c0
                cur = int.from_bytes(orig[a - start:a - start + n], "little")
                prev = int.from_bytes(orig[a - start - width:a - start - width + n], "little")
                buf[a:a + n] = ((cur - prev) % (1 << (8 * n))).to_bytes(n, "little")
        if 0 < len(kept) < width:
            t = bytes(buf[start:end])
            front = bytes(t[r * width + c] for r in range(rows) for c in kept)
            back = bytes(t[r * width + c] for r in range(rows) for c in diffed)
            buf[start:end] = front + back
        pos = end
    count = len(tables)
    return (u32(len(data)) + u32(4 * count)
            + b"".join(u32(t[0]) for t in tables) + b"".join(u32(t[1]) for t in tables)
            + b"".join(u32(t[2]) for t in tables) + bytes(buf))


def delta_encode(data: bytes, method: str = "delta", block: int = 8 << 20, find=None) -> bytes:
    """Blocks of up to `block` bytes; find(chunk) -> tables for that chunk (default: one
    table of 4-byte records over the whole chunk, if it holds at least 2 records)."""
    def default(chunk):
        rows = len(chunk) // 4
        return [(0, 16, rows)] if rows >= 2 else []
    find = find or default
    return b"".join(delta_block(data[i:i + block], find(data[i:i + block]))
                    for i in range(0, len(data), block))


# --- rep (long repeats), encode direction --------------------------------------------------
def rep_stream(window: int, blocks) -> bytes:
    """A rep stream from explicit blocks: [(lits: [bytes] (N+1 runs), matches: [(len, dist)])].
    Nothing is checked, so damaged streams can be made too."""
    out = u32(window)
    for lits, matches in blocks:
        body = u32(len(matches)) + b"".join(u32(m[0]) for m in matches)
        body += b"".join(u32(m[1]) for m in matches)
        body += b"".join(u32(len(x)) for x in lits) + b"".join(lits)
        out += u32(len(body)) + body
    return out + u32(0)


def rep_encode(data: bytes, method: str = "rep", window: int | None = None,
               block: int = 1 << 16, min_len: int = 8) -> bytes:
    """A simple greedy encoder: blocks of `block` input bytes, matches of at least min_len
    bytes found through 4-byte keys, distances up to the window."""
    if window is None:
        window = 64 << 20
        for p in method.split(":")[1:]:
            if p.endswith("kb"):
                window = int(p[:-2]) << 10
            elif p.endswith("mb"):
                window = int(p[:-2]) << 20
            elif p.endswith("b") and p[:-1].isdigit():
                window = int(p[:-1])
    last: dict[bytes, int] = {}
    blocks, i = [], 0
    while i < len(data) or not blocks:
        end = min(len(data), i + block)
        lits, matches, run = [], [], bytearray()
        while i < end:
            key = data[i:i + 4]
            j = last.get(key) if len(key) == 4 else None
            if len(key) == 4:
                last[key] = i
            n = 0
            if j is not None and 0 < i - j <= window:
                while i + n < len(data) and data[j + n] == data[i + n] and n < 1 << 20:
                    n += 1
            if n >= min_len:
                lits.append(bytes(run))
                run = bytearray()
                matches.append((n, i - j))
                i += n
            else:
                run.append(data[i])
                i += 1
        lits.append(bytes(run))
        blocks.append((lits, matches))
        if i >= len(data):
            break
    return rep_stream(window, blocks)


ENCODERS = {
    "storing": lambda data, method: data,
    "lzma": _lzma,
    "exe": exe_encode,
    "delta": delta_encode,
    "rep": rep_encode,
}


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
