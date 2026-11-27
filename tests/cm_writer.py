"""Test-only writer of cm-stage streams made of stored segments (no compression), for the
stage's wiring tests: the 11-byte header, the job container, and each job's segment headers
coded with the same adaptive models and rANS layer the decoder uses (a small rANS encoder,
written from the same behaviour-only specification). The decoder's coded segments are tested
in its own Rust tests.
"""

from __future__ import annotations

RANS_LOW = 1 << 23
STORED = 0x3F
STORED_SYMBOL = 13  # the stored type's place in the first type table
JOB_UNIT = 2 << 20  # a job is (field + 1) units


def _adapt(c: list[int], n: int, s: int, k: int, sh: int) -> None:
    for i in range(n):
        t = (k * i if i <= s else 0x8000 + k * i - 1) & 0xFFFF
        d = (t - c[i]) & 0xFFFF
        if d >= 0x8000:
            d -= 0x10000
        c[i] = (c[i] + (d >> sh)) & 0xFFFF


def _uniform16() -> list[int]:
    return [2048 * i for i in range(17)]


class _Recorder:
    """Records the interval of each coded item, then encodes them (see encode())."""

    def __init__(self):
        self.rec: list[tuple[int, int, int]] = []

    def bit15(self, probs: list[int], i: int, sh: int, v: int) -> None:
        p = probs[i]
        self.rec.append((0, p, 15) if v == 0 else (p, 32768 - p, 15))
        probs[i] = p + ((32768 - p) >> sh) if v == 0 else p - (p >> sh)

    def sym16(self, c: list[int], s: int, k: int, sh: int) -> None:
        self.rec.append((c[s], c[s + 1] - c[s], 15))
        _adapt(c, 16, s, k, sh)

    def encode(self) -> bytes:
        x = RANS_LOW
        emitted = bytearray()
        for start, f, bits in reversed(self.rec):
            xmax = ((RANS_LOW >> bits) << 8) * f
            while x >= xmax:
                emitted.append(x & 0xFF)
                x >>= 8
            x = ((x // f) << bits) + (x % f) + start
        return x.to_bytes(4, "little") + bytes(reversed(emitted))


class _Number:
    """The stream-A number model."""

    def __init__(self):
        self.hist, self.ctx = 0, 0
        self.z = [0x4000] * 4
        self.c1 = [_uniform16() for _ in range(32)]
        self.c2 = [_uniform16() for _ in range(32)]
        self.bt = [0x4000] * (31 * 64)

    def put(self, r: _Recorder, v: int) -> None:
        b = 1 if v else 0
        r.bit15(self.z, self.hist, 4, b)
        self.hist = (2 * self.hist + b) & 3
        if not b:
            self.ctx = 0
            return
        c = v.bit_length() - 1
        if c >= 15:
            r.sym16(self.c1[self.ctx], 15, 2, 5)
            r.sym16(self.c2[self.ctx], c - 15, 2, 5)
        else:
            r.sym16(self.c1[self.ctx], c, 2, 5)
        t = 1
        for i in range(c - 1, -1, -1):
            e = (v >> i) & 1
            r.bit15(self.bt, c * 64 + t, 4, e)
            t = (2 * t + e) & 63
        self.ctx = c + 1


def stream_a(lengths: list[int]) -> bytes:
    """Segment headers of stored segments of these lengths, then the end-of-job header."""
    r = _Recorder()
    flag, fctx, prev = [0x4000, 0x4000], 0, 0
    t1 = _uniform16()
    num = _Number()
    for n in [*lengths, 0]:
        same = 1 if prev == STORED else 0
        r.bit15(flag, fctx, 5, same)
        fctx = same
        if not same:
            r.sym16(t1, STORED_SYMBOL, 2, 5)
        prev = STORED
        num.put(r, n)
    return r.encode()


def header(jobsz: int = 0, single: bool = False) -> bytes:
    """A = 0xEE284844 (a typical stream's settings), window 16 MiB, jobs of (jobsz + 1) * 2
    MiB; `single` clears the multi-job bit (a form gitfurl refuses)."""
    a = 0xEE284844 & (0x7FFFFFFF if single else 0xFFFFFFFF)
    b = (jobsz << 24) | (1 << 13) | 0x1F
    return a.to_bytes(4, "little") + b.to_bytes(4, "little") + b"\0" + (0x40).to_bytes(2, "little")


def job(data: bytes, piece: int = 1 << 20) -> bytes:
    """One job holding `data` as stored segments of at most `piece` bytes."""
    lengths = [min(piece, len(data) - i) for i in range(0, len(data), piece)]
    a = stream_a(lengths)
    return len(a).to_bytes(4, "little") + a + len(data).to_bytes(4, "little") + data


def encode(data: bytes, jobsz: int = 0, piece: int = 1 << 20) -> bytes:
    """A whole stream: `data` cut into jobs of (jobsz + 1) * 2 MiB, the last one shorter."""
    size = (jobsz + 1) * JOB_UNIT
    jobs = b"".join(job(data[i:i + size], piece) for i in range(0, len(data), size))
    return header(jobsz) + jobs + b"\0\0\0\0"
