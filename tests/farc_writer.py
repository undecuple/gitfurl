"""A small FreeArc writer for the tests: archives are built in memory, as bytes.

Storing, raw LZMA1 from Python's own lzma module, and test-only encoders for exe, x86
(dispack070), delta, rep, 4x4 and srep (ENCODERS), written from the same clean-room
specifications as gitfurl/farc_codecs.py, so agreement between the two does not show agreement
with FreeArc or srep themselves. The srep encoder borrows gitfurl's own VHASH and SipHash for those two
checksum kinds; those are checked against published values in the tests. Chains are encoded
left to right. The bytes are written to a temporary folder only for a test; no archive file
is kept in the repository.
"""

import hashlib
import lzma
import random
import zlib

from gitfurl.farc_codecs import Vhash, siphash24

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


# --- 4x4 (pieces compressed on their own), encode direction --------------------------------
def x4_piece(data: bytes, opts: dict, store: bool = False) -> bytes:
    """One piece: kept as it is when asked, or when lzma does not make it smaller."""
    if not store:
        comp = lzma.compress(data, format=lzma.FORMAT_RAW,
                             filters=[{"id": lzma.FILTER_LZMA1, **opts}])
        if len(comp) < len(data):
            return u32(len(data)) + u32(len(comp)) + comp
    return u32(0xFFFFFFFF) + u32(len(data)) + data


def x4_encode(data: bytes, method: str = "4x4:lzma", sizes=None) -> bytes:
    """A version word, then pieces. sizes: the pieces' sizes in turn (repeated; default one
    size of 64 KiB). The inner method is the fields from "lzma" on."""
    fields = method.split(":")
    inner = ":".join(fields[fields.index("lzma"):]) if "lzma" in fields else "lzma"
    opts = lzma_opts(inner)
    sizes = list(sizes or [1 << 16])
    out, at, k = bytearray(u32(0)), 0, 0
    while at < len(data):
        n = sizes[k % len(sizes)]
        k += 1
        out += x4_piece(data[at:at + n], opts)
        at += n
    return bytes(out)


# --- srep (long-range matches), encode direction -------------------------------------------
SREP_KINDS = {"md5": (0, 0, 16), "none": (1, 0, 16), "sha1": (2, 0, 20), "sha512": (3, 0, 64),
              "vmac": (4, 32, 16), "siphash": (5, 16, 8)}


def srep_header(version: int, kind: str = "none", base: int = 0, seed: bytes | None = None):
    """The 16-byte header and the seed. Returns (bytes, checksum function or None, H)."""
    num, slen, hlen = SREP_KINDS[kind]
    seed = bytes(range(slen)) if seed is None else seed
    head = (bytes.fromhex("1718352653524550") + bytes([version, num, slen, (hlen - 16) % 256])
            + u32(base) + seed)
    funcs = {"md5": lambda d: hashlib.md5(d).digest(),
             "sha1": lambda d: hashlib.sha1(d).digest(),
             "sha512": lambda d: hashlib.sha512(d).digest(),
             "vmac": lambda d: Vhash(seed).digest(d),
             "siphash": lambda d: siphash24(seed, d).to_bytes(8, "little")}
    return head, funcs.get(kind), hlen


def find_matches(data: bytes, min_len: int = 16) -> list[tuple[int, int, int]]:
    """Greedy backward matches (dest, source, length) through 4-byte keys; a match may
    overlap its own destination."""
    last: dict[bytes, int] = {}
    out, i = [], 0
    while i + 4 <= len(data):
        key = data[i:i + 4]
        j = last.get(key)
        last[key] = i
        n = 0
        if j is not None:
            while i + n < len(data) and data[j + n] == data[i + n]:
                n += 1
        if n >= min_len:
            out.append((i, j, n))
            i += n
        else:
            i += 1
    return out


def _split(matches, block: int):
    """Split matches so that each piece's destination, and source, lie in one block."""
    out = []
    for d, s, n in matches:
        while n:
            k = min(n, block - d % block, block - s % block)
            out.append((d, s, k))
            d, s, n = d + k, s + k, n - k
    return out


def srep_encode(data: bytes, version: int = 3, kind: str = "none", block: int = 1 << 16,
                base: int = 0, min_len: int = 16, flag: bytes = b"") -> bytes:
    """An srep stream of format 2 (I/O-LZ), 3 (Future-LZ) or 4 (Index-LZ) with blocks of
    `block` bytes. `base` is the header's base length (every match must be at least that
    long, so it is raised to min_len). flag: an optional flag byte put first."""
    min_len = max(min_len, base)
    head, check, hlen = srep_header(version, kind, base)
    matches = _split(find_matches(data, min_len), block)
    matches = [m for m in matches if m[2] >= max(base, 1)]
    covered = bytearray(len(data))
    for d, _, n in matches:
        covered[d:d + n] = b"\1" * n
    nblocks = -(-len(data) // block)
    by_dest: list[list] = [[] for _ in range(nblocks)]
    by_src: list[list] = [[] for _ in range(nblocks)]
    for m in matches:
        by_dest[m[0] // block].append(m)
        by_src[m[1] // block].append(m)
    out, index, sizes = bytearray(flag + head), bytearray(), []
    for b in range(nblocks):
        start, chunk = b * block, data[b * block:(b + 1) * block]
        lits = bytes(c for c, used in zip(chunk, covered[start:start + len(chunk)]) if not used)
        recs = bytearray()
        if version == 2:
            q = start
            for d, s, n in sorted(by_dest[b]):
                recs += u32(d - q) + (d - s).to_bytes(8, "little") + u32(n - base)
                q = d + n
        else:
            anchor = start
            for d, s, n in sorted(by_src[b], key=lambda m: m[1]):
                recs += u32(s - anchor) + (d - s).to_bytes(8, "little") + u32(n - base)
                anchor = s
        csum = check(chunk)[:hlen] if check else bytes(hlen)
        if version == 4:
            index += recs
            sizes.append(len(recs))
            recs = bytearray()
        out += u32(len(lits)) + u32(len(chunk)) + u32(len(recs)) + csum + recs + lits
    if version == 4:
        out += index + b"".join(u32(n) for n in sizes)
        out += u32(len(index)) + u32(len(index) >> 32) + u32(24 + 4 * len(sizes)) + u32(1)
        out += u32(0xAFBAADAC) + u32(0xD9CAE7E8)
    return bytes(out)


def srep_method(data: bytes, method: str = "srep") -> bytes:
    """For chains: srep (format 3 unless the options say m...o / a bare m..., then format 2 or
    4), checksum by `hash=`."""
    version, kind = 3, "vmac"
    for p in method.split(":")[1:]:
        if p.startswith("m") and p[1:2].isdigit():
            version = 2 if p.endswith("o") else 3 if p.endswith("f") else 4
        elif p.startswith("hash="):
            kind = p[5:]
        elif p == "hash-":
            kind = "none"
    return srep_encode(data, version, kind, block=1 << 16)


# --- x86 (FreeArc's dispack070 code filter), encode direction -----------------------------
# A test-only encoder that mirrors gitfurl's decoder step for step, with the decoder's own
# opcode tables (so a round trip checks the stage's mechanics, not the tables; the
# specification's vectors made by FreeArc's encoder check those). Unlike FreeArc's encoder it
# treats every chunk as code unless told otherwise, and it escapes any byte it cannot parse.

X86_CHUNK = 16384
_X86_ITEM = {13: 2, 14: 4, 15: 4, 16: 4, 17: 4, 18: 4}


def _x86_target(ft: list, v: int, streams) -> None:
    """Encode one call or jump-table target through the function table (T() reversed)."""
    if v in ft[:255]:
        k = ft.index(v)
        streams[2].append(k + 1)
        ft.insert(0, ft.pop(k))
    else:
        streams[2].append(0)
        streams[17] += v.to_bytes(4, "big")
        ft.insert(0, v)
        ft.pop()


def _x86_parse(pad: bytes, i: int, base: int):
    """One instruction at i: (length, OP bytes, [(stream, item bytes as in the input)],
    call target or None, sets NEXTFUNC), or None when the decoder would refuse it."""
    from gitfurl.farc_codecs import X86_GROUP, X86_MODRM, X86_ONE, X86_TWO

    j = i
    c = pad[j]
    j += 1
    ops, items, prefix = [c], [], False
    p = X86_ONE[c]
    if p == 0x200:
        prefix = True
        c = pad[j]
        j += 1
        ops.append(c)
        p = X86_ONE[c]
        if p == 0x200:
            return j - i, ops, items, None, False
        if p >= 0x1000:
            return None
    nextfunc = c in (0xC2, 0xC3, 0xCC) and p != 0x400
    if p == 0x400:
        d = pad[j]
        j += 1
        ops.append(d)
        p = X86_TWO[d]
        c = -1  # not a one-byte opcode
    elif p >= 0x80:
        if p & 0x100:
            items.append((13, pad[j:j + 2]))
            j += 2
        if p & 0x800:
            rel = int.from_bytes(pad[j:j + 4], "little")
            j += 4
            return j - i, ops, items, (base + j + rel) & 0xFFFFFFFF, nextfunc
        p &= 0x7F
    if p < 0:
        return None
    if p & 0x20:
        m = pad[j]
        j += 1
        ops.append(m)
        if p & 0x40:
            p = X86_GROUP[(m >> 3 & 7) | (c & 1) << 3 | (c & 8) << 1]
            if p < 0:
                return None
        f = X86_MODRM[m]
        if f == 2:
            items.append((3 + (m & 7), pad[j:j + 1]))
            j += 1
        elif f == 3:
            items.append((15, pad[j:j + 4]))
            j += 4
        elif f == 4:
            items.append((16, pad[j:j + 4]))
            j += 4
        elif f:
            s = pad[j]
            items.append((1, pad[j:j + 1]))
            j += 1
            if f == 5:
                items.append((7, pad[j:j + 1]))
                j += 1
            elif f == 6 or s & 7 == 5:
                items.append((15, pad[j:j + 4]))
                j += 4
        p &= 3
    width = {1: 1, 2: 2, 3: 2 if prefix else 4, 8: 4, 9: 4, 10: 1, 11: 4}.get(p, 0)
    stream = {1: 12, 2: 13, 3: 13 if prefix else 14, 8: 16, 9: 18, 10: 11, 11: 18}.get(p)
    if p == 11:  # a rel32 jump: stored as its absolute target
        rel = int.from_bytes(pad[j:j + 4], "little")
        items.append((18, ((base + j + 4 + rel) & 0xFFFFFFFF).to_bytes(4, "little")))
        j += 4
    elif width:
        items.append((stream, pad[j:j + width]))
        j += width
    return j - i, ops, items, None, nextfunc


def x86_block(data: bytes, base: int, tables: bool = True) -> bytes:
    """One filtered block (the 19-dword size table and the streams) for `data` at `base`.
    With `tables`, three or more dwords in a row that point into the block are written as a
    jump table."""
    streams = [bytearray() for _ in range(19)]
    ft, nextfunc, n = [0] * 256, True, len(data)
    pad = bytes(data) + bytes(16)
    i = 0
    while i < n:
        count = 0
        while tables and count < 256 and i + 4 * count + 4 <= n and \
                0 <= int.from_bytes(data[i + 4 * count:i + 4 * count + 4], "little") - base < n:
            count += 1
        if count >= 3:
            streams[0] += bytes([0xCE, count - 1])
            for k in range(count):
                _x86_target(ft, int.from_bytes(data[i + 4 * k:i + 4 * k + 4], "little"),
                            streams)
            i += 4 * count
            continue
        c = data[i]
        if nextfunc and c != 0xCC:
            ft.insert(0, (base + i) & 0xFFFFFFFF)
            ft.pop()
            nextfunc = False
        got = _x86_parse(pad, i, base)
        if got is not None and i + got[0] > n:  # cut by the block's end: the rest escaped
            for b in data[i:]:
                streams[0] += bytes([0xF1, b])
            break
        if got is None or c == 0xF1 or c == 0xCE:
            streams[0] += bytes([0xF1, c])
            i += 1
            continue
        length, ops, items, call, sets = got
        streams[0] += bytes(ops)
        for s, raw in items:
            streams[s] += raw[::-1] if s in _X86_ITEM else raw
        if call is not None:
            _x86_target(ft, call, streams)
        if sets:
            nextfunc = True
        i += length
    sizes = b"".join(len(s).to_bytes(4, "little") for s in streams)
    return sizes + b"".join(streams)


def x86_encode(data: bytes, method: str = "dispack070", code=None, tables: bool = True,
               chunk: int = X86_CHUNK, base: int = 0x40000000) -> bytes:
    """The framed stage stream: the chunk size, then code blocks (runs of code chunks up to
    the method's block size) and data or raw frames. code(piece) says whether a chunk is code
    (default: every chunk)."""
    from gitfurl import farc

    block = farc.parse_method(method).stages[0].size
    code = code or (lambda piece: True)
    if not data:
        return b""
    out = bytearray(u32(chunk))
    run = bytearray()

    def flush():
        nonlocal base, run
        if run:
            enc = x86_block(run, base, tables)
            out.extend(u32(0xC71B3AE2) + u32(len(run)) + u32(len(enc)) + enc)
            base = _x86_advance(base, len(run))
            run = bytearray()

    for at in range(0, len(data), chunk):
        piece = data[at:at + chunk]
        if code(piece):
            if len(run) + len(piece) > block:
                flush()
            run += piece
            continue
        flush()
        first = int.from_bytes(piece[:4], "little")
        if len(piece) == chunk and not 0 <= first - 0xC71B3AE0 < 16:
            out += piece
        else:
            out += u32(0xC71B3AE1) + u32(len(piece)) + piece
        base = _x86_advance(base, len(piece))
    flush()
    return bytes(out)


def _x86_advance(base: int, n: int) -> int:
    base += n
    return base - 0x80000000 if base >= 0xC0000000 else base


ENCODERS = {
    "storing": lambda data, method: data,
    "dispack070": x86_encode,
    "dispack": x86_encode,
    "lzma": _lzma,
    "exe": exe_encode,
    "delta": delta_encode,
    "rep": rep_encode,
    "4x4": x4_encode,
    "srep": srep_method,
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
