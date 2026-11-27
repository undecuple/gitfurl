"""The "rolz" stage of FreeArc chains: an external method whose block holds one or more records,
each a small container with an entropy-coded LZ inside (gitfurl's own name for the method; the
archive's text for it is only in farc.py's parser, and is never shown).

Layers, outermost first: records (a u32 length, then a container); the container (a magic, then
CRC-checked packets: an index offset, the data chunks, the index); streams (the chunks of one
type joined) read by a two-state rANS bit reader; adaptive models (bit states with an APM,
14-bit CDFs, gamma codes, bucketed integers); an LZ with reps, matches, reduced-offset matches
(ROLZ) and special blocks; and the data pipeline: the main LZ, then an x86 call/jump stage, then
long-range copies. A directory, decoded first, gives the window, the file's size and its CRC.

Bounded memory: the window is read from the directory and checked against the stage's plan
(memory_for()) before any buffer of that size is made; a window over the plan is refused.
Every size and index read from the stream is checked before it is used. Every stream must be
used up exactly, with both coder states at their end value, and every LZ must have given
exactly what was read from it, or the block is refused.

Written from a behaviour-only specification under the clean-room rule of docs/scope.md: the
person who wrote this did not see the original program, its code or notes about it. The
tables below are data from that specification.

Nothing here writes files, runs anything or uses the network.
"""

from __future__ import annotations

import sys
import zlib
from array import array
from bisect import bisect_right

M32 = 0xFFFFFFFF
MAGIC = b"\x43\x4d\x28\x05"
HIST = 0xFF0  # zero bytes of history in front of every LZ's output
SMALL_WINDOW = 1 << 20  # the directory's and the table LZ's window
WINDOW_CAP = 512 << 20  # a larger window (from the directory) is refused
DIR_CAP = 1 << 20  # the directory's decoded size
MAX_CHUNKS = 1 << 16  # chunks in one container
MAX_INDEX = 1 << 20  # bytes of one container's index
PACKET_MAX = (1 << 24) - 1  # a packet's payload (u24)
REQUEST = 65536  # the x86 stage's request size
TRIM_SLACK = 1 << 20  # a small-window LZ drops its oldest output this much past its window
STOP_TOKENS = 1 << 15  # LZ tokens between two Stop checks
MODELS = 24 << 20  # models, tables and interpreter overhead of one record, counted in the plan
FINAL = 0xFFFF  # both coder states end here

_LITTLE = sys.byteorder == "little"


class RolzError(Exception):
    """The data is damaged, or needs more than gitfurl allows. The text is gitfurl's own."""


# --- tables from the specification ----------------------------------------------------------

def _buckets(nbits):
    base, b = [], 0
    for n in nbits:
        base.append(b)
        b = (b + (1 << n)) & M32
    base.append(b)
    return tuple(nbits), tuple(base)


TA, TAB = _buckets([0] * 9 + [1, 2, 3, 4, 5, 6, 0])  # rep length
TB, TBB = _buckets([0] * 10 + [1, 2, 3, 4, 5, 6])  # match length
TC, TCB = _buckets([6, 5, 4, 3, 2, 2, 2, 1] + [0] * 6 + [1, 1, 2, 2, 3, 4, 5, 6])  # ROLZ length
TD, TDB = _buckets(range(8, 30))  # match distance bucket
TE, TEB = _buckets(range(27))  # ROLZ index
TG, TGB = _buckets([0, 0, 1, 2, 3, 4, 5, 6, 6, 5, 4, 3, 2, 1, 0, 0])  # byte deltas
TI, TIB = _buckets([0, 0, *range(1, 15), *range(14, 0, -1), 0, 0])  # word deltas
TK, TKB = _buckets([31, 28, *range(26, 0, -2), 0, *range(2, 27, 2), 28, 31])  # dword deltas
TL, TLB = _buckets([7, 6, 5, 4, 3, 2, 1, 0, 0, 0, 1, 2, 3, 4, 5, 6, 7])  # pixel channel 0
TM, TMB = _buckets([8, 7, 6, 5, 4, 3, 2, 1, 0, 1, 2, 3, 4, 5, 6, 7, 8])  # pixel channels 1, 2
TN, TNB = _buckets([0] * 5 + list(range(1, 16)))  # residuals of specials 9-13
TC_BIAS = TCB[10]
TK_BIAS = TKB[15]
TL_BIAS = TLB[8]
TM_BIAS = TMB[8]

# previous byte -> literal class (13 classes), from the specification's ranges
_LITRANGES = [(0x00, 0x0F, 0), (0x10, 0x20, 1), (0x21, 0x2F, 2), (0x30, 0x39, 3),
              (0x3A, 0x40, 2), (0x41, 0x41, 4), (0x42, 0x44, 5), (0x45, 0x45, 4),
              (0x46, 0x48, 5), (0x49, 0x49, 4), (0x4A, 0x4E, 5), (0x4F, 0x4F, 4),
              (0x50, 0x54, 5), (0x55, 0x55, 4), (0x56, 0x5A, 5), (0x5B, 0x60, 6),
              (0x61, 0x61, 7), (0x62, 0x64, 8), (0x65, 0x65, 7), (0x66, 0x68, 8),
              (0x69, 0x69, 7), (0x6A, 0x6E, 8), (0x6F, 0x6F, 7), (0x70, 0x74, 8),
              (0x75, 0x75, 7), (0x76, 0x7A, 8), (0x7B, 0x9F, 9), (0xA0, 0xBF, 10),
              (0xC0, 0xDF, 11), (0xE0, 0xFF, 12)]
LITCLS = bytes(cls for lo, hi, cls in _LITRANGES for _ in range(lo, hi + 1))
LITMODE = (0, 1, 1, 1, 1, 1, 1, 2, 2, 2, 2, 2, 2, 0)
NEXT_LIT = (0, 0, 0, 0, 1, 2, 3, 4, 5, 6, 4, 5, 6, 0)
REP_DELTA = (0, 0, 0, 0, -1, 1, -2, 2, -3, 3, -1, 1, -2, 2, -3, 3)
REP_SRC = (0, 1, 2, 3, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 1, 1)
QB = (0, 1, 1, 2, 2, 2, 2, 2) + (3,) * 8 + (4,) * 8 + (5,) * 5 + (6, 6, 7)

# frequency starts of the CDFs that do not start uniform
F_REP = (64, 1, 1, 1, 8, 8, 8, 8, 4, 4, 1, 1, 1, 1, 1, 1)
F_LEN = (1,) + (4,) * 15  # rep length and match length
F_ROLZLEN = (1,) * 10 + (8,) + (1,) * 11
F_ROLZIDX = (8,) * 8 + (4, 4, 2, 2, 2, 2) + (1,) * 13
F_DIST = (4, 4, 2, 2, 2, 2) + (1,) * 16


def _bit_states():
    """P[1..255], next-state tables and the derived p12 and quantiser values."""
    P = [0] * 257
    P[128] = p = 1 << 31
    for k in range(127, 0, -1):
        p -= p >> 5
        P[k] = p
        P[256 - k] = (1 << 32) - p
    n0, n1 = [128] * 256, [128] * 256
    for k in range(1, 128):
        q = P[k] + (((1 << 32) - P[k]) >> 5)
        t = q - (q >> 6)
        n1[k] = next((j for j in range(k + 1, 256) if P[j] >= t), 255)
        n0[k] = max(1, k - 1)
    for k in range(1, 128):
        n1[256 - k] = 256 - n0[k]
        n0[256 - k] = 256 - n1[k]
    n1[128], n0[128] = 129, 127
    p12 = [P[s] >> 20 for s in range(256)]
    qs = [QB[P[s] >> 27] if s else 0 for s in range(256)]
    p12[0], qs[0] = p12[128], qs[128]
    return tuple(P[:256]), tuple(n0), tuple(n1), tuple(p12), tuple(qs)


PROB, NEXT0, NEXT1, P12, QS = _bit_states()


def _targets(n: int):
    """Update targets for an n-symbol CDF, one list per decoded symbol (with the 0x4000 end)."""
    return tuple([i if i <= s else 0x407F - n + i for i in range(n)] + [0x4000]
                 for s in range(n))


_T = {n: _targets(n) for n in (16, 17, 20, 22, 27, 31, 32)}


def uniform(n: int) -> list[int]:
    return [i * 0x4000 // n for i in range(n)] + [0x4000]


def from_freq(f) -> list[int]:
    total, acc, out = sum(f), 0, []
    for x in f:
        out.append(acc * 0x4000 // total)
        acc += x
    return out + [0x4000]


def update(cdf: list[int], s: int) -> None:
    cdf[:] = [a + ((b - a) >> 7) for a, b in zip(cdf, _T[len(cdf) - 1][s])]


# --- container ------------------------------------------------------------------------------

def _varints(data: bytes):
    at = 0
    while True:
        start = at
        while True:
            if at >= len(data):
                raise RolzError("damaged index (it ends inside a number)")
            b = data[at]
            at += 1
            if not b & 1:
                break
            if at - start >= 10:
                raise RolzError("damaged index (a number that is too long)")
        v = data[at - 1] >> 1
        for i in range(at - 2, start - 1, -1):
            v = ((v << 8) | data[i]) >> 1
        yield v, at == len(data)


class Container:
    """One container inside a record: `read(off, n)` reads n bytes at archive offset off; the
    archive has `size` bytes. Reads the index and lists each type's chunks."""

    def __init__(self, read, size: int):
        self.read, self.size = read, size
        if size < 4 + 7 + 6 or read(0, 4) != MAGIC:
            raise RolzError("bad header (not the expected magic)")
        payload, end = self.packet(4, size)
        if len(payload) != 6:
            raise RolzError("bad header (the first packet is not an index offset)")
        index_at = int.from_bytes(payload, "little")
        if not end <= index_at < size:
            raise RolzError("bad header (the index offset lies outside the data)")
        idx, at, short = bytearray(), index_at, False
        while at < size:
            if len(idx) > MAX_INDEX:
                raise RolzError("an index larger than gitfurl reads")
            payload, at = self.packet(at, size)
            if len(payload) > 4096 or short:
                raise RolzError("damaged index (packets of an unexpected size)")
            short = len(payload) < 4096
            idx += payload
        values = _varints(bytes(idx))
        count, done = next(values)
        if not 1 <= count <= MAX_CHUNKS or done:
            raise RolzError("damaged index (a chunk count gitfurl does not accept)")
        last = [0] * 8
        sizes = []
        for k in range(count):
            try:
                v, done = next(values)
            except StopIteration:  # pragma: no cover - _varints raises first
                raise RolzError("damaged index (it ends early)") from None
            if done != (k == count - 1):
                raise RolzError("damaged index (it is not used up exactly)")
            t, mag = v & 7, v >> 4
            s = last[t] + (~mag if v & 8 else mag)
            last[t] = s
            if t > 4 or s < 15:
                raise RolzError("damaged index (a chunk of a type or size gitfurl does not "
                                "accept)")
            sizes.append((t, s))
        sizes.reverse()  # listed from the last chunk down to the first
        self.chunks: list[list[tuple[int, int]]] = [[] for _ in range(5)]
        at = end
        for t, s in sizes:
            self.chunks[t].append((at, s))
            at += s
        if at != index_at:
            raise RolzError("damaged index (the chunk sizes do not add up to the index offset)")

    def packet(self, at: int, end: int) -> tuple[bytes, int]:
        """The payload of the packet at `at` (CRC checked) and where the next one starts."""
        if end - at < 7:
            raise RolzError("damaged data (a packet header is cut off)")
        head = self.read(at, 7)
        n = int.from_bytes(head[:3], "little")
        if not 0 < n <= end - at - 7:
            raise RolzError("damaged data (a packet length is zero or runs past its chunk)")
        payload = self.read(at + 7, n)
        if zlib.crc32(payload, zlib.crc32(head[:3])) != int.from_bytes(head[3:], "little"):
            raise RolzError("damaged data (a packet's CRC does not match)")
        return payload, at + 7 + n


# --- the bit reader ---------------------------------------------------------------------------

def _words(payload: bytes):
    if _LITTLE:
        return memoryview(payload).cast("H")
    a = array("H")  # pragma: no cover - no big-endian machine in use
    a.frombytes(payload)  # pragma: no cover
    a.byteswap()  # pragma: no cover
    return a  # pragma: no cover


class Coder:
    """The two-state rANS reader of one stream. `c` is the state the last operation worked on,
    `o` the other one; every operation first swaps them."""

    __slots__ = ("box", "chunks", "ci", "at", "end", "w", "wi", "c", "o")

    def __init__(self, box: Container | None, chunks):
        self.box, self.chunks, self.ci = box, list(chunks), 0
        self.at = self.end = 0
        self.w, self.wi, self.c, self.o = (), 0, 0, 0

    def _load(self) -> int:
        while self.at == self.end:
            if self.ci == len(self.chunks):
                raise RolzError("damaged data (a stream runs out of packets)")
            self.at, size = self.chunks[self.ci]
            self.end = self.at + size
            self.ci += 1
        payload, self.at = self.box.packet(self.at, self.end)
        n = len(payload) >> 1
        if len(payload) & 1 or n < 4:
            raise RolzError("damaged data (a packet that does not hold two coder states)")
        w = self.w = _words(payload)
        self.o = w[n - 3] << 16 | w[n - 4]
        self.wi = n - 4
        return w[n - 1] << 16 | w[n - 2]

    def bit(self, p: int) -> int:
        x = self.o
        self.o = self.c
        if x < 65536:
            if self.wi:
                self.wi -= 1
                x = x << 16 | self.w[self.wi]
            else:
                x = self._load()
        lo = x & 0xFFF
        if lo < p:
            self.c = p * (x >> 12) + lo
            return 1
        self.c = (x - p * ((x >> 12) + 1)) & M32
        return 0

    def raw(self, n: int) -> int:
        """n raw bits; raw(0) is no operation (no swap): the directory's first special block
        decodes as the specification's example only that way."""
        if n > 16:
            hi = self.raw(n - 16)
            return hi << 16 | self.raw(16)
        if not n:
            return 0
        x = self.o
        self.o = self.c
        if x < 65536:
            if self.wi:
                self.wi -= 1
                x = x << 16 | self.w[self.wi]
            else:
                x = self._load()
        self.c = x >> n
        return x & ((1 << n) - 1)

    def sym(self, cdf: list[int]) -> int:
        x = self.o
        self.o = self.c
        if x < 65536:
            if self.wi:
                self.wi -= 1
                x = x << 16 | self.w[self.wi]
            else:
                x = self._load()
        lo = x & 0x3FFF
        s = bisect_right(cdf, lo) - 1
        c = cdf[s]
        self.c = (x >> 14) * (cdf[s + 1] - c) + lo - c
        cdf[:] = [a + ((b - a) >> 7) for a, b in zip(cdf, _T[len(cdf) - 1][s])]
        return s

    def model_bit(self, models: list[int], i: int) -> int:
        """A bit with bit-state model models[i], which then steps."""
        s = models[i]
        b = self.bit(P12[s])
        models[i] = NEXT1[s] if b else NEXT0[s]
        return b

    def gamma(self, models: list[int]) -> int:
        node = 1
        for _ in range(6):
            node = node << 1 | self.model_bit(models, node)
        n = node - 64
        return ((1 << n) | self.raw(n)) - 1

    def used_up(self) -> bool:
        if not self.chunks and not self.c and not self.o:
            return True  # a stream with no chunks that was never read
        return (self.wi == 0 and self.at == self.end and self.ci == len(self.chunks)
                and self.c == FINAL and self.o == FINAL)


# --- the LZ -------------------------------------------------------------------------------------

def _log2(x: int) -> int:
    return x.bit_length() - 1 if x > 0 else 0


class _Filter:
    """One adaptive filter stage of specials 10-13."""

    __slots__ = ("h", "ws", "v", "q", "half", "d")

    def __init__(self, q: int, d: int, vectors: int = 1):
        self.h = [0] * 16
        self.ws = [[0] * 16 for _ in range(vectors)]
        self.v, self.q, self.half, self.d = 0, q, 1 << (q - 1), d

    def predict(self) -> int:
        s = (sum(a * b for a, b in zip(self.h, self.ws[self.v])) + self.half) & M32
        return (s - (1 << 32) if s >> 31 else s) >> self.q

    def learn(self, r: int) -> None:
        if r:
            neg = r < 0
            w = self.ws[self.v]
            for i, a in enumerate(self.h):
                if a:
                    w[i] = ((w[i] + (1 if (a < 0) == neg else -1) + 0x8000) & 0xFFFF) - 0x8000

    def push(self, x: int) -> None:
        h = self.h
        del h[0]
        h.append(x)
        d = self.d
        h[d] = ((h[d] - x + 0x8000) & 0xFFFF) - 0x8000

    def step(self) -> None:
        """Switch to the other weight vector (stages with two of them)."""
        if len(self.ws) > 1:
            self.v ^= 1


class _Group:
    """A residual group: CDFs of the TN bucket and a shift k (rule A: pairs by k; B: one by k)."""

    __slots__ = ("cdfs", "k", "acc", "rule_a")

    def __init__(self, rule_a: bool):
        self.rule_a = rule_a
        self.cdfs = [uniform(20) for _ in range(16)]
        self.k = self.acc = 0

    def read(self, cd: Coder, b: int = 0) -> tuple[int, int]:
        k = self.k
        if self.rule_a:
            if k >= 8:
                raise RolzError("damaged data (a residual shift beyond the models)")
            cdf = self.cdfs[k * 2 + b]
        else:
            if k >= 16:
                raise RolzError("damaged data (a residual shift beyond the models)")
            cdf = self.cdfs[k]
        s = cd.sym(cdf)
        v = TNB[s]
        if TN[s]:
            v += cd.raw(TN[s])
        if k:
            v = ((v << k) | cd.raw(k)) & M32
        if self.rule_a:
            if v > 1 << (k + 1):
                self.k = k + 1
            elif v < 1 << k and k:
                self.k = k - 1
        else:
            self.acc = acc = (self.acc - (self.acc >> 3) + v) & M32
            t = acc >> k
            if t > 16:
                self.k = k + 1
            elif t < 8 and k:
                self.k = k - 1
        return v, (~(v >> 1)) & M32 if v & 1 else v >> 1


def _s8(x: int) -> int:
    return ((x + 0x80) & 0xFF) - 0x80


def _s16(x: int) -> int:
    return ((x + 0x8000) & 0xFFFF) - 0x8000


def _s32(x: int) -> int:
    x &= M32
    return x - (1 << 32) if x >> 31 else x


class _Rolz:
    """The reduced-offset tables: per context byte, entries in pages of 512 slots, found
    through mid-blocks; filled pages go back to a FIFO free list (so the oldest are reused)."""

    def __init__(self, window: int):
        self.npages = window // 0x1400 + 0x201
        self.nmids = window // 0x280000 + 0x201
        slots = self.npages * 512
        self.pos = array("I", bytes(4 * slots))
        self.len = bytearray(slots)
        self.last = bytearray(slots)
        self.mid = array("I", bytes(4 * self.nmids * 512))
        self.top = array("I", bytes(4 * 256 * 512))
        self.count = [0] * 256
        self.curpage = list(range(256))
        self.curmid = list(range(256))
        for c in range(256):
            self.top[c * 512] = c
            self.mid[c * 512] = c
        # FIFO free lists as rings that hold every number once
        self.fpage = array("I", range(0x100, self.npages)) + array("I", [0])
        self.fp_head, self.fp_n = 0, self.npages - 0x100
        self.fmid = array("I", range(0x100, self.nmids)) + array("I", [0])
        self.fm_head, self.fm_n = 0, self.nmids - 0x100

    def _swap_page(self, old: int) -> int:
        ring, size = self.fpage, len(self.fpage)
        ring[(self.fp_head + self.fp_n) % size] = old
        g = ring[self.fp_head]
        self.fp_head = (self.fp_head + 1) % size
        return g

    def _swap_mid(self, old: int) -> int:
        ring, size = self.fmid, len(self.fmid)
        ring[(self.fm_head + self.fm_n) % size] = old
        m = ring[self.fm_head]
        self.fm_head = (self.fm_head + 1) % size
        return m

    def add(self, c: int, pos: int, length: int) -> None:
        n = self.count[c]
        slot = self.curpage[c] * 512 + (n & 511)
        self.pos[slot] = pos & M32
        self.len[slot] = length & 0xFF
        self.last[slot] = 0
        n += 1
        self.count[c] = n
        if not n & 511:
            if not n & 0x3FFFF:
                m = self._swap_mid(self.curmid[c])
                self.top[c * 512 + ((n >> 18) & 511)] = m
                self.curmid[c] = m
            g = self._swap_page(self.curpage[c])
            self.mid[self.curmid[c] * 512 + ((n >> 9) & 511)] = g
            self.curpage[c] = g

    def slot(self, c: int, idx: int) -> int:
        n = self.count[c]
        if idx >= n:
            raise RolzError("damaged data (a reduced-offset index past its list)")
        e = n - 1 - idx
        x = n ^ e
        if x < 512:
            g = self.curpage[c]
        elif x < 0x40000:
            g = self.mid[self.curmid[c] * 512 + ((e >> 9) & 511)]
        else:
            g = self.mid[self.top[c * 512 + ((e >> 18) & 511)] * 512 + ((e >> 9) & 511)]
        return g * 512 + (e & 511)


class LZ:
    """One LZ decoder over its own stream, with its own models. Output is read in order with
    take(); `buf` keeps HIST zero bytes in front of position 0 (a small-window LZ drops its
    oldest bytes past its window, `drop` counts them)."""

    def __init__(self, cd: Coder, window: int, rolz: bool, stop=None, trim: bool = True):
        self.cd, self.window, self.stop = cd, window, stop
        self.trim = trim
        self.buf = bytearray(HIST)
        self.drop = 0
        self.rd = HIST
        self.state = 0
        self.reps = [1, 2, 3, 4]
        self.st1 = [128] * (256 * 4)  # decisions 0, 1, then 2 or 3
        self.st2 = [128] * (14 * 4 * 4)
        self.apm = [(r + c + 1) * 0x1000 for r in range(8) for c in range(8)]
        self.lit = [[uniform(16) for _ in range(81)] for _ in range(13)]
        self.dcdf = from_freq(F_DIST)
        self.dlow = [uniform(16) for _ in range(22)]
        self.dmid = [uniform(16) for _ in range(22)]
        self.mlen = [from_freq(F_LEN) for _ in range(56)]
        self.repsym = [from_freq(F_REP) for _ in range(14)]
        self.replen = [from_freq(F_LEN) for _ in range(28)]
        self.rolz = _Rolz(window) if rolz else None
        if rolz:
            self.ridx = [from_freq(F_ROLZIDX) for _ in range(256)]
            self.rlen = [from_freq(F_ROLZLEN) for _ in range(48)]
        self.sel = uniform(16)
        # each selector has its own CDFs: a pair (by sign) per lane, or one per channel
        self.tg = [[uniform(16) for _ in range(2 * (sel + 1))] for sel in range(4)]
        self.ti = [[uniform(32) for _ in range(2)], [uniform(32) for _ in range(4)]]
        self.tk = [uniform(31) for _ in range(2)]
        self.px = [[uniform(17) for _ in range(3)] for _ in range(2)]
        self.alpha = uniform(16)  # special 8's fourth byte
        self.s9p, self.s9w = 0x8000, 0
        self.s9 = [([0] * 16, _Group(True)) for _ in range(3)]
        self.s10 = (_Filter(12, 14), _Filter(10, 14), _Group(False))
        self.s11 = (_Filter(12, 13, 2), _Filter(10, 13, 2), (_Group(False), _Group(False)))
        self.s11c = 0
        self.s12 = (_Filter(12, 14), _Filter(10, 14), _Filter(10, 14), _Group(False))
        self.s13 = (_Filter(12, 13, 2), _Filter(10, 13, 2), (_Filter(10, 14), _Filter(10, 14)),
                    (_Group(False), _Group(False)))
        self.s13c = 0
        self.s14 = (uniform(16), [uniform(16) for _ in range(16)])
        self.last_end = [0] * 16  # specials 10-14

    # -- reading ---------------------------------------------------------------------------

    def take(self, n: int) -> bytes:
        if len(self.buf) - self.rd < n:
            self._run(self.rd + n)
        out = bytes(self.buf[self.rd:self.rd + n])
        self.rd += n
        if self.trim and self.rd > self.window + HIST + TRIM_SLACK:
            cut = self.rd - self.window - HIST
            del self.buf[:cut]
            self.rd -= cut
            self.drop += cut
        return out

    def pending(self) -> int:
        """Bytes decoded (by a token that ran past the last request) and not read."""
        return len(self.buf) - self.rd

    def _o(self, i: int) -> int:
        """The byte at buffer index i (0 before the kept history)."""
        return self.buf[i] if i >= 0 else 0

    # -- the token loop ----------------------------------------------------------------------

    def _run(self, need: int) -> None:
        cd = self.cd
        bit, sym, raw = cd.bit, cd.sym, cd.raw
        buf = self.buf
        st1, st2, apm = self.st1, self.st2, self.apm
        n0, n1, qs = NEXT0, NEXT1, QS
        lit, reps = self.lit, self.reps
        rolz = self.rolz
        window = self.window
        state = self.state
        tokens = 0
        base_pos = self.drop - HIST  # absolute position = buffer index + base_pos
        try:
            while len(buf) < need:
                tokens += 1
                if tokens & (STOP_TOKENS - 1) == 0 and self.stop:
                    self.stop()
                n = len(buf)
                c1 = buf[n - 1]
                i1 = c1 * 4
                i2 = (state * 4 + ((n + base_pos) & 3)) * 4
                a = st1[i1]
                b = st2[i2]
                ci = qs[a] * 8 + qs[b]
                p = apm[ci]
                if bit(p >> 4):
                    apm[ci] = p + ((65536 - p) >> 7)
                    st1[i1] = n1[a]
                    st2[i2] = n1[b]
                    k = lit[LITCLS[c1]]
                    mode = LITMODE[state]
                    if mode:
                        m = buf[n - reps[0]]
                        mh = m >> 4
                        t = 16 * mode
                        hi = sym(k[1 + t + mh])
                        lo = sym(k[33 + t + (m & 15)] if hi == mh else k[1 + hi])
                    else:
                        hi = sym(k[0])
                        lo = sym(k[1 + hi])
                    buf.append(hi << 4 | lo)
                    state = NEXT_LIT[state]
                    continue
                apm[ci] = p - (p >> 7)
                st1[i1] = n0[a]
                st2[i2] = n0[b]
                # decision 1
                a = st1[i1 + 1]
                b = st2[i2 + 1]
                ci = qs[a] * 8 + qs[b]
                p = apm[ci]
                if bit(p >> 4):
                    apm[ci] = p + ((65536 - p) >> 7)
                    st1[i1 + 1] = n1[a]
                    st2[i2 + 1] = n1[b]
                    j = 3
                    kind = 2
                else:
                    apm[ci] = p - (p >> 7)
                    st1[i1 + 1] = n0[a]
                    st2[i2 + 1] = n0[b]
                    j = 2
                    kind = 0
                a = st1[i1 + j]
                b = st2[i2 + j]
                ci = qs[a] * 8 + qs[b]
                p = apm[ci]
                if bit(p >> 4):
                    apm[ci] = p + ((65536 - p) >> 7)
                    st1[i1 + j] = n1[a]
                    st2[i2 + j] = n1[b]
                    kind += 1
                else:
                    apm[ci] = p - (p >> 7)
                    st1[i1 + j] = n0[a]
                    st2[i2 + j] = n0[b]
                lim = n if n < window else window
                if kind == 3:  # match
                    d = sym(self.dcdf)
                    lo4 = sym(self.dlow[d])
                    mid = sym(self.dmid[d]) << d
                    if d > 16:
                        mid |= raw(d - 16) << 16 | raw(16)
                    else:
                        mid |= raw(d)
                    dist = TDB[d] + 1 + (lo4 | mid << 4)
                    if dist > lim:
                        raise RolzError("damaged data (a distance that points before the "
                                        "history)")
                    reps.insert(0, dist)
                    del reps[4]
                    kk = sym(self.mlen[((dist.bit_length() - 1) >> 3) + 4 * state])
                    length = 2 + TBB[kk] + raw(TB[kk])
                    self._copy(dist, length)
                    if rolz is not None:
                        pos = n + base_pos
                        rolz.add(c1, pos, length)
                        rolz.add(buf[n], pos + 1, length - 1)
                    state = 9 if state < 7 else 12
                elif kind == 1:  # rep
                    r = sym(self.repsym[state])
                    if r >= 4:
                        nd = reps[REP_SRC[r]] + REP_DELTA[r]
                        if not 0 < nd <= lim:
                            raise RolzError("damaged data (a distance that points before "
                                            "the history)")
                        reps.insert(0, nd)
                        del reps[4]
                    elif r == 1:
                        reps[0], reps[1] = reps[1], reps[0]
                    elif r:
                        reps.insert(0, reps.pop(r))
                    kk = sym(self.replen[(r != 0) + 2 * state])
                    self._copy(reps[0], 1 + TAB[kk] + raw(TA[kk]))
                    state = 7 if state < 7 else 10
                elif kind == 2:  # reduced-offset match
                    if rolz is None:
                        raise RolzError("damaged data (a reduced-offset match in a stream "
                                        "that has none)")
                    s = sym(self.ridx[c1])
                    nb = TE[s]
                    idx = TEB[s] + (raw(nb - 16) << 16 | raw(16) if nb > 16 else raw(nb))
                    slot = rolz.slot(c1, idx)
                    pos = n + base_pos
                    dist = (pos - rolz.pos[slot]) & M32
                    if not 0 < dist <= lim:
                        raise RolzError("damaged data (a distance that points before the "
                                        "history)")
                    reps.insert(0, dist)
                    del reps[4]
                    elen, elast = rolz.len[slot], rolz.last[slot]
                    if elast == 0:
                        ctx = (((idx + 1).bit_length() - 1) & ~7) | _log2(elen)
                        base = elen
                    elif elast < elen:
                        ctx = _log2(elen - elast) | 0x20
                        base = elen
                    else:
                        ctx = _log2(elast - elen + 1) | 0x28
                        base = elast
                    kk = sym(self.rlen[ctx])
                    length = base + TCB[kk] + raw(TC[kk]) - TC_BIAS
                    if length < 1:
                        raise RolzError("damaged data (a reduced-offset match of length 0)")
                    rolz.last[slot] = (length + 1) & 0xFF
                    self._copy(dist, length)
                    rolz.add(c1, pos, length)
                    state = 8 if state < 7 else 11
                else:  # special block
                    self.state = state
                    self._special(sym(self.sel))
                    state = 13
        finally:
            self.state = state

    def _copy(self, d: int, length: int) -> None:
        buf = self.buf
        s = len(buf) - d
        if d >= length:
            buf += buf[s:s + length]
        else:
            buf += (buf[s:] * (length // d + 1))[:length]

    # -- special blocks ----------------------------------------------------------------------

    def _special(self, sel: int) -> None:
        cd, buf, o = self.cd, self.buf, self._o
        if sel <= 3:
            stride = sel + 1
            lanes = ((0,) * 8, (1, 0) * 4, (2, 1, 0) * 3, (3, 2, 1, 0) * 2)[sel]
            for lane in lanes:
                n = len(buf)
                pred = o(n - stride)
                sg = ((pred - o(n - 2 * stride)) >> 7) & 1
                k = cd.sym(self.tg[sel][lane * 2 + sg])
                buf.append((pred + TGB[k] + cd.raw(TG[k])) & 0xFF)
        elif sel <= 5:
            def word(i):
                return o(i) | o(i + 1) << 8
            for lane in ((0, 0, 0, 0), (1, 0, 1, 0))[sel - 4]:
                n = len(buf)
                if sel == 4:
                    pred = word(n - 2)
                    prev = pred - word(n - 4)
                else:
                    pred = 2 * word(n - 4) - word(n - 8)
                    prev = word(n - 4) - (2 * word(n - 8) - word(n - 12))
                sg = (prev >> 15) & 1
                k = cd.sym(self.ti[sel - 4][lane * 2 + sg])
                v = (pred + TIB[k] + cd.raw(TI[k])) & 0xFFFF
                buf += v.to_bytes(2, "little")
        elif sel == 6:
            for _ in range(2):
                n = len(buf)
                d1 = int.from_bytes(bytes(o(i) for i in range(n - 4, n)), "little")
                d2 = int.from_bytes(bytes(o(i) for i in range(n - 8, n - 4)), "little")
                sg = ((d1 - d2) >> 31) & 1
                k = cd.sym(self.tk[sg])
                v = (d1 + TKB[k] + cd.raw(TK[k]) - TK_BIAS) & M32
                buf += v.to_bytes(4, "little")
        elif sel <= 8:
            size = sel - 4  # 3 or 4 bytes a pixel
            px = self.px[sel - 7]
            for _ in range(3 if sel == 7 else 2):
                n = len(buf)
                p0, p1, p2 = o(n - size), o(n - size + 1), o(n - size + 2)
                k = cd.sym(px[0])
                d0 = _s32(TLB[k] + cd.raw(TL[k]) - TL_BIAS)
                k = cd.sym(px[1])
                d1 = _s32(TMB[k] + cd.raw(TM[k]) - TM_BIAS)
                k = cd.sym(px[2])
                d2 = _s32(TMB[k] + cd.raw(TM[k]) - TM_BIAS)
                t = d0 - (d2 >> 1)
                u = t - (d1 >> 1)
                buf += bytes(((d1 + u + p0) & 0xFF, (d2 + t + p1) & 0xFF, (u + p2) & 0xFF))
                if sel == 8:  # the fourth byte: from the byte before the pixel, one CDF
                    pred = o(n - 1)
                    k = cd.sym(self.alpha)
                    buf.append((pred + TGB[k] + cd.raw(TG[k])) & 0xFF)
        elif sel == 9:
            self._special9()
        elif sel <= 13:
            self._filters(sel)
        elif sel == 14:
            self._special14()
        else:
            for _ in range(16):
                buf += cd.raw(16).to_bytes(2, "little")

    def _special9(self) -> None:
        cd, buf, o = self.cd, self.buf, self._o
        p = self.s9p
        b = cd.bit(p >> 4)
        self.s9p = p + ((0x10000 - p) >> 5) if b else p - (p >> 5)
        if b:
            self.s9w = cd.raw(16)
        w = self.s9w
        if w < 6:
            raise RolzError("damaged data (an image block that reads bytes not yet written)")
        for _ in range(4):
            P = len(buf)
            e = [0] * 16
            for c in range(3):
                up = o(P - w + c)
                e[c] = up - o(P - 2 * w + c)
                e[3 + c] = up - o(P - w + c - 3)
                e[6 + c] = up - o(P - w + c + 3)
                e[9 + c] = o(P - w + c - 3) - o(P + c - 3)
                e[12 + c] = o(P + c - 6) - o(P + c - 3)
            px = [0, 0, 0]
            for ch, j in ((0, 1), (1, 0), (2, 2)):
                weights, group = self.s9[ch]
                pr = _s32(sum(a * b for a, b in zip(e, weights)) + 0x200) >> 10
                _, d = group.read(cd, 1 if pr < -4 or pr > 4 else 0)
                r = _s16(d)
                if r:
                    neg = r < 0
                    for i, a in enumerate(e):
                        if a:
                            weights[i] = _s16(weights[i] + (1 if (a < 0) == neg else -1))
                px[j] = (d + pr + o(P + j - 3)) & 0xFF
                if ch == 0:
                    e[15] = px[1] - o(P - 2)
                elif ch == 1:
                    e[0] = px[0] - o(P - 3)
            buf += bytes(px)

    def _filters(self, sel: int) -> None:
        cd, buf, o = self.cd, self.buf, self._o
        start = len(buf)
        start_pos = start + self.drop - HIST
        warm = self.last_end[sel] != start_pos
        if sel == 10:
            A, B, g = self.s10
            if warm:
                for i in range(start - 16, start):
                    s = _s8(o(i) - 128)
                    r1 = _s8(s - A.predict())
                    A.learn(r1)
                    A.push(s)
                    r2 = _s8(r1 - B.predict())
                    B.learn(r2)
                    B.push(r1)
            for _ in range(32):
                r2 = _s8(g.read(cd)[1])
                r1 = _s8(B.predict() + r2)
                B.learn(r2)
                B.push(r1)
                s = _s8(A.predict() + r1)
                A.learn(r1)
                A.push(s)
                buf.append((s + 128) & 0xFF)
        elif sel == 11:
            A, B, gs = self.s11
            if warm:
                for i in range(start - 16, start):
                    s = _s8(o(i) - 128)
                    r1 = _s8(s - A.predict())
                    A.learn(r1)
                    A.push(s)
                    A.step()
                    r2 = _s8(r1 - B.predict())
                    B.learn(r2)
                    B.push(r1)
                    B.step()
                    self.s11c ^= 1
            for _ in range(32):
                self.s11c ^= 1
                r2 = _s8(gs[self.s11c].read(cd)[1])
                r1 = _s8(B.predict() + r2)
                B.learn(r2)
                B.push(r1)
                B.step()
                s = _s8(A.predict() + r1)
                A.learn(r1)
                A.push(s)
                A.step()
                buf.append((s + 128) & 0xFF)
        else:
            two = sel == 13
            if two:
                A, B, Cs, gs = self.s13
            else:
                A, B, C, g = self.s12
            if warm:
                for i in range(start - 32, start, 2):
                    s = _s16(o(i) | o(i + 1) << 8)
                    r1 = _s16(s - A.predict())
                    A.learn(r1)
                    A.push(s)
                    A.step()
                    r2 = _s16(r1 - B.predict())
                    B.learn(r2)
                    B.push(r1)
                    B.step()
                    if two:
                        self.s13c ^= 1
                        C = Cs[self.s13c]
                    r3 = _s16(r2 - C.predict())
                    C.learn(r3)
                    C.push(r2)
            for _ in range(16):
                if two:
                    self.s13c ^= 1
                    C, g = Cs[self.s13c], gs[self.s13c]
                r3 = _s16(g.read(cd)[1])
                r2 = _s16(C.predict() + r3)
                C.learn(r3)
                C.push(r2)
                r1 = _s16(B.predict() + r2)
                B.learn(r2)
                B.push(r1)
                B.step()
                s = _s16(A.predict() + r1)
                A.learn(r1)
                A.push(s)
                A.step()
                buf += (s & 0xFFFF).to_bytes(2, "little")
        self.last_end[sel] = start_pos + 32

    def _special14(self) -> None:
        cd, buf, o = self.cd, self.buf, self._o
        high, low = self.s14
        start = len(buf)
        start_pos = start + self.drop - HIST
        if self.last_end[14] != start_pos:
            m = min((start_pos - self.last_end[14]) & M32, 256)
            for i in range(start - m, start):
                x = o(i)
                update(high, x >> 4)
                update(low[x >> 4], x & 15)
        for _ in range(32):
            hi = cd.sym(high)
            buf.append(hi << 4 | cd.sym(low[hi]))
        self.last_end[14] = start_pos + 32


# --- the pipeline -------------------------------------------------------------------------------

def _header_at(out: bytearray, j: int) -> bool:
    a = j - 10
    if out[a] == 0x4D and out[a + 1] == 0x5A:
        return (out[j - 8] | out[j - 7] << 8) < 0x201 and (out[j - 2] | out[j - 1] << 8) < 0x101
    if out[a] == 0x50 and out[a + 1] == 0x45 and not out[a + 2] and not out[a + 3]:
        return (out[j - 4] | out[j - 3] << 8) < 0x61
    return False


class X86Calls:
    """The x86 call/jump stage: requests of REQUEST bytes, each either passed on or rebuilt
    with targets from three tables read from the table LZ."""

    def __init__(self, flags: Coder, tables: LZ, main: LZ, stop=None):
        self.cd, self.tables, self.main, self.stop = flags, tables, main, stop
        self.flag = [128]
        self.models = [128] * 288
        self.base = 0

    def request(self, n: int) -> bytes:
        cd = self.cd
        if not cd.model_bit(self.flag, 0):
            # `base` counts every request, passed on or rebuilt (only that way do the
            # targets of later requests come out right on real data)
            self.base = _s32(self.base + n)
            return self.main.take(n)
        counts = []
        for _ in range(3):
            nb = cd.raw(5)
            counts.append(((1 << nb) | cd.raw(nb)) - 1)
        total = sum(counts)
        if 4 * total > n:
            raise RolzError("damaged data (call tables larger than their piece)")
        raw = self.tables.take(4 * total)
        tabs = [None, None, None]
        at = 0
        for t in (2, 1, 0):
            tabs[t] = memoryview(raw)[at:at + 4 * counts[t]].cast("I") if _LITTLE else \
                array("I", raw[at:at + 4 * counts[t]])
            at += 4 * counts[t]
        if not _LITTLE:  # pragma: no cover - no big-endian machine in use
            for tab in tabs:
                tab.byteswap()
        used = [0, 0, 0]
        src = self.main.take(n - 4 * total)
        out = bytearray(n)
        models, bit = self.models, cd.bit
        base = self.base
        i = si = min(2, n, len(src))
        out[:i] = src[:i]
        end = n - 4
        try:
            while i < end:
                x = src[si]
                si += 1
                out[i] = x
                j = i + 1
                if j > 9 and out[i - 9] in (0x4D, 0x50) and _header_at(out, j):
                    base = _s32(~i)
                if x == 0xE8 or x == 0xE9:
                    t, ctx = x & 1, out[i - 1]
                elif out[i - 1] == 0x0F and x & 0xF0 == 0x80:
                    t, ctx = 1, 0x100 + x - 0x80
                elif out[i - 2] & 0xFB == 0x48 and out[i - 1] & 0xF0 == 0x80 and x & 0xC7 == 5:
                    t, ctx = 2, 0x110 + (out[i - 1] & 15)
                else:
                    i += 1
                    continue
                s = models[ctx]
                b = bit(P12[s])
                models[ctx] = NEXT1[s] if b else NEXT0[s]
                if b:
                    u = used[t]
                    if u >= len(tabs[t]):
                        raise RolzError("damaged data (a call table is read past its end)")
                    used[t] = u + 1
                    v = (tabs[t][u] - (j + base)) & 0xFFFFFF
                    out[j] = v & 0xFF
                    out[j + 1] = (v >> 8) & 0xFF
                    out[j + 2] = v >> 16
                    out[j + 3] = 0xFF if v & 0x800000 else 0
                    i += 5
                else:
                    i += 1
        except IndexError:
            raise RolzError("damaged data (the call tables are not used up exactly)") from None
        if len(src) - si != n - i or used != counts:
            raise RolzError("damaged data (the call tables are not used up exactly)")
        out[i:] = src[si:]
        self.base = _s32(base + n)
        return bytes(out)


class Directory:
    """The decoded directory: the window, and the one regular file's size and CRC."""

    def __init__(self, lz: LZ):
        head = lz.take(8)
        self.window = int.from_bytes(head[:4], "little")
        count = int.from_bytes(head[4:], "little")
        if not 1 <= count <= (DIR_CAP - 8) // 29:
            raise RolzError("damaged directory (a file count gitfurl does not accept)")
        names, used = 0, 8
        while names < count:
            if used >= DIR_CAP:
                raise RolzError("damaged directory (longer than gitfurl reads)")
            used += 1
            if lz.take(1) == b"\0":
                names += 1
        rest = lz.take(28 * count)
        f = [int.from_bytes(rest[i:i + 8], "little") for i in range(0, 8 * count, 8)]
        at = 16 * count
        crcs = [int.from_bytes(rest[at + 4 * i:at + 4 * i + 4], "little") for i in range(count)]
        at += 4 * count
        attrs = [int.from_bytes(rest[at + 4 * i:at + 4 * i + 4], "little") for i in range(count)]
        files = [i for i in range(count) if not attrs[i] & 0x10]
        if len(files) != 1 or any(f[i] for i in range(count) if attrs[i] & 0x10):
            raise RolzError("a directory that does not hold exactly one file")
        self.size, self.crc = f[files[0]], crcs[files[0]]
        if not 0 < self.window or self.size > self.window:
            raise RolzError("damaged directory (a file larger than its window)")


def memory_for(window: int) -> int:
    """Bytes one record may hold at most, for a window of `window` bytes: the main LZ's output
    and the final output (each at most the window, as the file is), the reduced-offset tables,
    the two small LZs, one packet per stream and the models."""
    pages = window // 0x1400 + 0x201
    mids = window // 0x280000 + 0x201
    tables = 6 * 512 * pages + 2048 * mids + 256 * 512 * 4 + 4 * (pages + mids)
    small = 2 * (SMALL_WINDOW + HIST + TRIM_SLACK + 2 * REQUEST)
    return 2 * (window + HIST + 1024) + tables + small + 5 * (PACKET_MAX + 7) + 4 * REQUEST \
        + MODELS


def decode_record(read, size: int, budget: int, stop=None, chunk: int = 1 << 20,
                  info: dict | None = None):
    """The file of one record's container, as pieces of at most `chunk` bytes. `budget` is the
    largest window the plan allows. Raises RolzError; the last piece is followed by the end
    checks (CRC, every stream used up), which raise before the generator ends."""
    box = Container(read, size)
    coders = [Coder(box, box.chunks[t]) for t in range(5)]
    dlz = LZ(coders[0], SMALL_WINDOW, False, stop)
    d = Directory(dlz)
    if dlz.pending() or not coders[0].used_up():
        raise RolzError("damaged directory (its stream is not used up exactly)")
    if d.window > budget:
        raise RolzError(f"a window of {d.window:,} bytes, over the {budget:,} planned")
    if info is not None:
        info.update(window=d.window, size=d.size, crc=d.crc)
    total = d.size
    main = LZ(coders[4], d.window, True, stop, trim=False)
    tlz = LZ(coders[2], SMALL_WINDOW, False, stop)
    calls = X86Calls(coders[3], tlz, main, stop)
    cd = coders[1]
    g0, g1, g2 = [128] * 64, [128] * 64, [128] * 64
    count = cd.gamma(g0)
    left = cd.gamma(g0)  # new bytes still to come from the call stage
    if left > total or count > total >> 8:
        raise RolzError("damaged data (copy counts larger than the file)")
    shift_base = max(16, d.window.bit_length() - 1 - 5)
    out = bytearray()
    sent = crc = 0
    pending = b""  # call-stage output not yet placed

    def flush():
        nonlocal sent, crc
        while len(out) - sent >= chunk:
            piece = bytes(out[sent:sent + chunk])
            crc = zlib.crc32(piece, crc)
            sent += chunk
            yield piece

    def fill(upto: int):
        """New bytes from the call stage until the output reaches `upto`."""
        nonlocal left, pending
        while len(out) < upto:
            if not pending:
                if not left:
                    raise RolzError("damaged data (the file needs more new bytes than there "
                                    "are)")
                n = min(REQUEST, left)
                left -= n
                if stop:
                    stop()
                pending = calls.request(n)
            k = min(upto - len(out), len(pending), chunk)
            out.extend(pending[:k])
            pending = pending[k:]
            yield from flush()

    pos = 0
    for _ in range(count):
        pos += cd.gamma(g0)
        if pos > total:
            raise RolzError("damaged data (a copy past the end of the file)")
        s = 8 if not pos >> (shift_base + 8) else min(16, pos.bit_length() - shift_base)
        back = cd.gamma(g1)
        length = (cd.gamma(g2) + 1) << s
        src = ((pos >> s) - back) << s
        if not 0 <= src < pos or length > total - pos:
            raise RolzError("damaged data (a copy outside the file)")
        yield from fill(pos)
        dist = pos - src
        while length:
            k = min(length, chunk, dist)
            at = len(out) - dist
            out += out[at:at + k]
            length -= k
            yield from flush()
        pos = len(out)
    yield from fill(total)
    if pending or left:
        raise RolzError("damaged data (more new bytes than the file holds)")
    if sent < len(out):
        piece = bytes(out[sent:])
        crc = zlib.crc32(piece, crc)
        sent = len(out)
        yield piece
    out.clear()  # the file is no longer needed for copies
    if crc != d.crc:
        raise RolzError("the file's CRC does not match")
    if main.pending() or tlz.pending():
        raise RolzError("damaged data (an LZ gave more than was read from it)")
    if not all(c.used_up() for c in coders):
        raise RolzError("damaged data (a stream is not used up exactly)")
    if info is not None:
        info.update(main=main.rd - HIST + main.drop, tables=tlz.rd - HIST + tlz.drop)


def decode_block(read, size: int, budget: int, stop=None, chunk: int = 1 << 20,
                 limit: int | None = None):
    """The block's output: its records back to back, until its input is used up. `read(off,
    n)` reads the block; at most `limit` bytes of output are accepted."""
    at = given = 0
    while at < size:
        if size - at < 4:
            raise RolzError("damaged data (a record header is cut off)")
        total = int.from_bytes(read(at, 4), "little")
        if not 4 < total <= size - at:
            raise RolzError("damaged data (a record length is too small or runs past the "
                            "block)")
        base = at + 4

        def sub(off, n, base=base):
            return read(base + off, n)

        for piece in decode_record(sub, total - 4, budget, stop, chunk):
            given += len(piece)
            if limit is not None and given > limit:
                raise RolzError("more data than the block's files hold")
            yield piece
        at += total
