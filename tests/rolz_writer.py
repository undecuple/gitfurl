"""Test-only builders for rolz data (see src/gitfurl/farc_rolz.py).

There is no rolz encoder here. Script is a stand-in for the decoder's bit reader: it answers
each request of the real decoder (a bit, raw bits, a symbol) with the next value of a list the
test wrote by hand, applies the same model updates, and records the request. encode() then
turns the recorded requests into a stream with a plain two-state rANS encoder, and the real
reader must give the same values back. So these streams check the reader, the container and
the pipeline's layout and limits against hand-made token lists; the model logic itself is the
decoder's own (checked against real data, see docs/testing.md).
"""

import zlib

from gitfurl import farc_rolz as r

MAGIC = r.MAGIC


class Script(r.Coder):
    """Answers the decoder's requests from `values`, in order, and records them."""

    def __init__(self, values):
        super().__init__(None, [])
        self.values = list(values)
        self.at = 0
        self.ops = []

    def _next(self, what):
        if self.at >= len(self.values):
            raise AssertionError(f"the script ran out at a {what} request")
        v = self.values[self.at]
        self.at += 1
        return v

    def bit(self, p):
        b = self._next("bit")
        assert b in (0, 1)
        assert b == 0 or p > 0
        self.ops.append(("bit", p, b))
        return b

    def raw(self, n):
        if n > 16:
            hi = self.raw(n - 16)
            return hi << 16 | self.raw(16)
        if not n:
            return 0
        v = self._next(f"raw({n})")
        assert 0 <= v < 1 << n, (n, v)
        self.ops.append(("raw", n, v))
        return v

    def sym(self, cdf):
        s = self._next("symbol")
        assert 0 <= s < len(cdf) - 1
        self.ops.append(("sym", cdf[s], cdf[s + 1] - cdf[s], s))
        r.update(cdf, s)
        return s

    def done(self) -> bool:
        return self.at == len(self.values)


def _encode_one(op, x):
    kind = op[0]
    if kind == "bit":
        _, p, b = op
        if b:
            return ((x // p) << 12) + x % p
        f = 4096 - p
        return ((x // f) << 12) + p + x % f
    if kind == "raw":
        _, n, v = op
        return x << n | v
    _, start, f, _s = op
    return ((x // f) << 14) + start + x % f


def _bound(op) -> int:
    """Largest state + 1 that op can be encoded from without passing 32 bits."""
    kind = op[0]
    if kind == "bit":
        f = op[1] if op[2] else 4096 - op[1]
        return f << 20
    if kind == "raw":
        return 1 << (32 - op[1])
    return op[2] << 18


def encode(ops, final=r.FINAL) -> bytes:
    """One packet payload that the reader decodes as `ops` (in decode order), ending with both
    states at `final`."""
    state = [final, final]
    words = []
    for i in range(len(ops) - 1, -1, -1):
        x = _encode_one(ops[i], state[i & 1])
        assert x < 1 << 32
        if i >= 1:
            # the decoder pulls a word before op i when the state is below 2^16; make it so
            # when the state is too big for the op before it on this state (or below 2^16)
            limit = _bound(ops[i - 2]) if i >= 2 else 1 << 32
            if x < 1 << 16 or x >= limit:
                words.append(x & 0xFFFF)
                x >>= 16
        state[i & 1] = x
    s0, s1 = state
    out = words + [s1 & 0xFFFF, s1 >> 16, s0 & 0xFFFF, s0 >> 16]
    return b"".join(w.to_bytes(2, "little") for w in out)


def encode_packets(ops, cuts=()) -> list[bytes]:
    """Payloads of several packets, the stream cut before each op number in `cuts`. encode()
    ends every packet with both states at 0xFFFF and no words left, so the reader loads the
    next packet exactly at the cut."""
    bounds = [0, *cuts, len(ops)]
    return [encode(ops[a:b]) for a, b in zip(bounds, bounds[1:])]


def packet(payload: bytes) -> bytes:
    head = len(payload).to_bytes(3, "little")
    return head + zlib.crc32(payload, zlib.crc32(head)).to_bytes(4, "little") + payload


def varint(v: int) -> bytes:
    if v < 128:
        return bytes([v << 1])
    b = (2 * v + 1) & 0xFF
    return bytes([b]) + varint((2 * v + 1) >> 8)


def container(chunks: list[tuple[int, bytes]], index_packet: int = 4096) -> bytes:
    """A container with these (type, chunk bytes) in file order."""
    body = b"".join(c for _, c in chunks)
    index_at = 4 + 13 + len(body)
    out = MAGIC + packet(index_at.to_bytes(6, "little")) + body
    last = [0] * 8
    idx = varint(len(chunks))
    for t, c in reversed(chunks):
        d = len(c) - last[t]
        last[t] = len(c)
        idx += varint(t | (8 | (~d) << 4 if d < 0 else d << 4))
    for i in range(0, len(idx), index_packet):
        out += packet(idx[i:i + index_packet])
    return out


def record(archive: bytes) -> bytes:
    return (len(archive) + 4).to_bytes(4, "little") + archive


def literals(data: bytes) -> list[int]:
    """Script values for literal tokens only, from LZ state 0 (which literals keep)."""
    out = []
    for b in data:
        out += [1, b >> 4, b & 15]
    return out


def gamma(v: int) -> list[int]:
    """Script values for one gamma-coded v: six tree bits, then the raw part (high first)."""
    n = (v + 1).bit_length() - 1
    out = [(n >> (5 - i)) & 1 for i in range(6)]
    rest = (v + 1) - (1 << n)
    widths, m = [], n
    while m > 16:
        widths.insert(0, 16)
        m -= 16
    widths.insert(0, m)
    shift = n
    for w in widths:
        shift -= w
        if w:
            out.append((rest >> shift) & ((1 << w) - 1))
    return out


def directory(window: int, size: int, crc: int, names=(b"tmp\0", b"f1\0"),
              attrs=(0x10, 0x20), sizes=None) -> bytes:
    count = len(names)
    sizes = sizes if sizes is not None else [0] * (count - 1) + [size]
    out = window.to_bytes(4, "little") + count.to_bytes(4, "little") + b"".join(names)
    out += b"".join(s.to_bytes(8, "little") for s in sizes)
    out += bytes(8 * count)
    out += b"".join((crc if s else 0).to_bytes(4, "little") for s in sizes)
    out += b"".join(a.to_bytes(4, "little") for a in attrs)
    out += bytes(4 * count)
    return out


def lz_ops(values, n: int, window: int = r.SMALL_WINDOW, rolz: bool = False):
    """Run a real LZ on a script until it has given n bytes; the ops and the bytes."""
    sc = Script(values)
    lz = r.LZ(sc, window, rolz, trim=not rolz)
    data = lz.take(n) if n else b""
    assert sc.done(), f"script not used up: {sc.at} of {len(sc.values)}"
    assert not lz.pending()
    return sc.ops, data


def stored_file(data: bytes, window: int | None = None) -> bytes:
    """A whole record whose file is `data`, written as literals only (no long-range copies,
    no call tables), with its directory, copy parameters (none) and flags (all 0)."""
    window = window if window is not None else max(1024, -(-len(data) // 1024) * 1024)
    d = directory(window, len(data), zlib.crc32(data))
    dops, _ = lz_ops(literals(d), len(d))
    requests = -(-len(data) // r.REQUEST)
    fl = Script([0] * requests)
    calls_flag = [128]
    for _ in range(requests):
        fl.model_bit(calls_flag, 0)
    s1 = Script(gamma(0) + gamma(len(data)))
    g0 = [128] * 64
    s1.gamma(g0)
    s1.gamma(g0)
    mops, got = lz_ops(literals(data), len(data), window, True)
    assert got == data
    chunks = [(4, packet(encode(mops)))] if mops else []
    chunks += [(3, packet(encode(fl.ops)))] if fl.ops else []
    chunks += [(1, packet(encode(s1.ops))), (0, packet(encode(dops)))]
    return record(container(chunks))
