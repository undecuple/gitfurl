"""Streaming decoders for FreeArc method chains: storing, lzma, exe, delta and rep.

A chain is decoded right to left: the last method in the archive's string reads the block's
stored bytes, and the first method's output is the block's file data. Each stage here is a
reader with read(n), which returns 1 to n bytes, or b"" once that stage's output has ended.
Stages pull from the stage to their right a piece at a time, so a block is never held in
memory whole. Every buffer has a cap that is known before the block is decoded (stage_plan),
and the plan shows their sum.

Damaged input raises CodecError, whose message is gitfurl's own text (never the archive's).
Nothing here writes files, runs anything or uses the network.

Written from the clean-room specifications of these methods (behaviour only); no FreeArc
code is used.
"""

from __future__ import annotations

import lzma
import os
import re
import sys
from array import array
from dataclasses import dataclass
from itertools import accumulate

from gitfurl import farc

EXE_ROUND = 262144  # the exe filter converts the stream in rounds of this many bytes
EXE_SLACK = 64  # bytes decoded past the files' end for each exe stage to the left (it needs 5)
REP_MAX_MATCHES = 1 << 21  # matches in one rep block (their tables take 12 bytes each)
REP_MIN_MATCHES = 1 << 16  # ... at least this many are allowed, however small the block
# rep's block headers and match tables may add up to this much, plus 16 bytes per byte of
# output, before a block is refused (a stream that gives little or no output for its size)
REP_FREE_OVERHEAD = 1 << 22
REP_OVERHEAD_PER_BYTE = 16
DELTA_MAX_EMPTY = 64  # empty delta blocks in a row (the encoder never writes one)
STOP_EVERY = 4096  # steps without output between two Stop checks inside a stage
I31 = 1 << 31

_LITTLE = sys.byteorder == "little"
_OPCODES = re.compile(b"[\xe8\xe9]")
_SIZE_KEEPING = ("storing", "exe")  # output length equals input length


class CodecError(Exception):
    """The block's data is damaged, or needs more memory than gitfurl allows."""


class StartError(CodecError):
    """A decoder could not be started (usually: not enough memory)."""


DELTA_ROWS = 1 << 16  # rows of one differenced run restored at a time


def _u32s(data: bytearray):
    """Little-endian 32-bit values (data's length is a multiple of 4), without a copy on a
    little-endian machine."""
    if _LITTLE:
        return memoryview(data).cast("I") if memoryview(b"").cast("I").itemsize == 4 \
            else _u32_array(data)
    return _u32_array(data)  # pragma: no cover - no big-endian machine in use


def _u32_array(data: bytearray) -> array:  # pragma: no cover - see _u32s
    a = array("I" if array("I").itemsize == 4 else "L")
    a.frombytes(data)
    if not _LITTLE:
        a.byteswap()
    return a


# --- planning -------------------------------------------------------------------------------

@dataclass(frozen=True)
class StagePlan:
    stage: farc.Stage
    budget: int  # lzma: dictionary; rep: window kept; delta: largest block
    memory: int  # bytes this stage may hold at most
    limit: int | None = None  # output this stage gives at most (None: all of its stream)


def stage_plan(method: farc.Method, out_size: int) -> list[StagePlan]:
    """Each stage's buffer caps for one block, in the archive's order (first = file data).

    out_size is the block's decoded size (the sum of its files' sizes). A stage whose output
    reaches the files through size-keeping stages only (storing, exe) is never asked for more
    than out_size bytes plus EXE_SLACK for each exe stage to its left (an exe stage gives
    correct output up to 5 bytes before where its input ends), so it stops there (`limit`),
    and its window never needs to reach further back than that.
    """
    out: list[StagePlan] = []
    limit: int | None = out_size  # None once a stage to the left changes sizes
    for st in method.stages:
        if st.name == "lzma":
            d = st.lzma["dict_size"]
            if limit is not None:
                d = min(d, max(4096, limit))
            out.append(StagePlan(st, d, d, limit))
        elif st.name == "exe":
            out.append(StagePlan(st, EXE_ROUND, 2 * EXE_ROUND, limit))
        elif st.name == "delta":
            # a block is held whole, so one that runs past the limit is refused (larger
            # than gitfurl handles), not decoded
            b = st.size if limit is None else min(st.size, limit)
            # data, a copy of one table, one column of it while it is moved (at most half
            # the block: such a table is at least 2 bytes wide), the three descriptor
            # arrays (at most b // 4 + 1 tables, 12 bytes each) and the work space of one
            # run of rows
            out.append(StagePlan(st, b, 2 * b + b // 2 + 12 * (b // 4 + 1) + 24 * DELTA_ROWS,
                                 limit))
        elif st.name == "rep":
            w = st.size if limit is None else min(st.size, limit)
            tables = 12 * _rep_match_cap(w) + 8
            out.append(StagePlan(st, w, w + tables, limit))
        else:  # storing
            out.append(StagePlan(st, 0, 0, limit))
        if st.name == "exe" and limit is not None:
            limit += EXE_SLACK
        elif st.name not in _SIZE_KEEPING:
            limit = None
    return out


def _rep_match_cap(window: int) -> int:
    return min(REP_MAX_MATCHES, max(REP_MIN_MATCHES, window // 8))


def chain_memory(method: farc.Method, out_size: int, chunk: int) -> int:
    """Bytes the chain may hold at once for one block: every decoding stage's caps, plus one
    chunk between each pair of stages. 0 when nothing is decoded (storing only)."""
    plans = [p for p in stage_plan(method, out_size) if p.stage.name != "storing"]
    if not plans:
        return 0
    return sum(p.memory for p in plans) + chunk * (len(plans) + 1)


# --- the pipeline ---------------------------------------------------------------------------

class Source:
    """The block's stored bytes, read from the archive a chunk at a time. Calls stop() before
    each read, so a Stop is seen however much the stages to its left pull."""

    def __init__(self, fd: int, offset: int, size: int, chunk: int, stop):
        self.fd, self.pos, self.left, self.chunk, self.stop = fd, offset, size, chunk, stop

    def read(self, n: int) -> bytes:
        if not self.left:
            return b""
        self.stop()
        k = min(max(n, 1), self.chunk, self.left)
        try:
            data = os.pread(self.fd, k, self.pos)
        except OSError as exc:
            raise CodecError(f"the archive could not be read ({exc.strerror or exc})") from exc
        if len(data) != k:
            raise CodecError("the archive could not be read completely (did it change?)")
        self.pos += k
        self.left -= k
        return data


class _Input:
    """Buffered reads from the stage to the right; at most one chunk is held."""

    def __init__(self, src, chunk: int):
        self.src, self.chunk = src, chunk
        self.buf, self.at, self.ended = b"", 0, False

    def some(self, n: int) -> bytes:
        """1 to n bytes, or b"" when the input has ended."""
        if self.at == len(self.buf):
            if self.ended:
                return b""
            self.buf, self.at = self.src.read(self.chunk), 0
            if not self.buf:
                self.ended = True
                return b""
        out = self.buf[self.at:self.at + n]
        self.at += len(out)
        return out

    def exact(self, n: int) -> bytearray:
        """n bytes, or fewer only when the input ended first (held once, not twice; callers
        never ask for more than their cap)."""
        out = bytearray(n)
        got = 0
        while got < n:
            more = self.some(n - got)
            if not more:
                del out[got:]
                break
            out[got:got + len(more)] = more
            got += len(more)
        return out


def _no_stop() -> None:
    return None


class _Stage:
    """Common read(): serve pending output, refill with _next() until it ends. stop() is
    called before each refill, so a Stop is seen even while a stage reads input that gives
    no output."""

    def __init__(self, stop=None):
        self.out, self.out_at, self.done = b"", 0, False
        self.stop = stop or _no_stop

    def read(self, n: int) -> bytes:
        while self.out_at >= len(self.out):
            if self.done:
                return b""
            self.stop()
            self.out = b""  # the last output is not held while the next is made
            self.out, self.out_at = self._next(), 0
        piece = self.out[self.out_at:self.out_at + n]
        self.out_at += len(piece)
        return piece

    def _next(self) -> bytes:  # pragma: no cover - overridden
        raise NotImplementedError


class LzmaStage(_Stage):
    """Raw LZMA1 through Python's lzma module. The stream must end with its end marker before
    its input ends; anything after the marker is not read. With a `limit`, at most that many
    bytes are decoded and the output then ends: the rest is never looked at, and damage there
    is not seen."""

    def __init__(self, src, opts: dict, dict_size: int, chunk: int, limit: int | None = None,
                 stop=None):
        super().__init__(stop)
        self.src, self.chunk, self.left = src, chunk, limit
        try:
            self.dec = lzma.LZMADecompressor(
                lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1,
                                           **dict(opts, dict_size=dict_size)}])
        except (lzma.LZMAError, MemoryError, ValueError) as exc:
            raise StartError("the lzma decoder could not be started (not enough memory?)") \
                from exc

    def _next(self) -> bytes:
        idle = 0
        while True:
            if self.dec.eof or self.left == 0:
                self.done = True
                return b""
            data = b""
            if self.dec.needs_input:
                data = self.src.read(self.chunk)
                if not data:
                    raise CodecError("the lzma data ended before its end marker")
            want = self.chunk if self.left is None else min(self.chunk, self.left)
            try:
                out = self.dec.decompress(data, max_length=want)
            except (lzma.LZMAError, EOFError) as exc:
                raise CodecError("damaged lzma data") from exc
            if out:
                if self.left is not None:
                    self.left -= len(out)
                return out
            idle = 0 if data else idle + 1
            if idle > 1000:  # no input taken and no output given: never seen; refuse
                raise CodecError("the lzma decoder stopped making progress")


class ExeStage(_Stage):
    """The x86 branch converter ("exe"), decode direction, in rounds of EXE_ROUND bytes.

    Each round reads EXE_ROUND bytes minus the 0-4 carried from the last round (fewer only at
    the end of the input), converts the opcodes it may, outputs up to where the walk stopped
    and carries the rest. A round of 5 bytes or fewer is output unchanged. The output length
    always equals the input length; the filter cannot detect damage.
    """

    def __init__(self, src, chunk: int, stop=None):
        super().__init__(stop)
        self.inp = _Input(src, chunk)
        self.carry = b""
        self.base = 0  # stream position of carry[0]
        self.rejected: list[int] = []  # stream positions of the last rejected opcodes

    def _next(self) -> bytes:
        buf = self.inp.exact(EXE_ROUND - len(self.carry))
        if self.carry:
            buf[:0] = self.carry
        n = len(buf)
        if n == 0:
            self.done = True
            return b""
        if n <= 5:
            self.carry = b""
            self.base += n
            return buf
        done, self.rejected = x86_branches(buf, self.base, self.rejected, decode=True)
        self.carry = bytes(buf[done:])
        self.base += done
        del buf[done:]
        return buf


def x86_branches(buf: bytearray, base: int, rejected: list[int], decode: bool) -> tuple[int, list]:
    """Convert one piece of an x86 stream in place (decode, or encode for the tests).

    base: stream position of buf[0]. rejected: stream positions of opcodes examined and
    rejected before this piece (only the last three positions matter). Returns (done, the new
    rejected list), done being the first position not examined and not inside a converted
    operand; buf[done:] has to be examined again with the next piece.
    """
    n = len(buf)
    limit = n - 4  # an opcode at i is examined only while i + 4 < n
    nxt = 0
    rej = [p for p in rejected if p >= base - 3]
    if limit <= 0:
        return 0, rej
    snapshot = bytes(buf)  # bytes at or after a position being examined never changed yet
    for m in _OPCODES.finditer(snapshot, 0, limit):
        i = m.start()
        if i < nxt:
            continue  # inside the operand of a converted opcode
        at = base + i
        hist = [at - p for p in rej if at - 3 <= p < at]
        if len(rej) > 3:
            rej = rej[-3:]
        q = -1
        if len(hist) >= 2:
            rej.append(at)
            continue
        if hist:
            q = 3 - hist[0]
            if buf[i + 1 + q] in (0, 0xFF):
                rej.append(at)
                continue
        if buf[i + 4] not in (0, 0xFF):
            rej.append(at)
            continue
        v = int.from_bytes(buf[i + 1:i + 5], "little")
        a = (at + 5) & 0xFFFFFFFF
        w = (v - a if decode else v + a) & 0xFFFFFFFF
        if q >= 0:
            mask = (1 << (8 * (q + 1))) - 1
            rounds = 0
            while (w >> (8 * q)) & 0xFF in (0, 0xFF):
                rounds += 1
                if rounds > 1024:
                    raise CodecError("the exe filter did not settle (internal limit)")
                v = w ^ mask
                w = (v - a if decode else v + a) & 0xFFFFFFFF
        buf[i + 1:i + 4] = (w & 0xFFFFFF).to_bytes(3, "little")
        buf[i + 4] = 0xFF if w >> 24 & 1 else 0
        nxt = i + 5
    return max(limit, nxt), rej[-3:]


class DeltaStage(_Stage):
    """The table preprocessor ("delta"): blocks of (DLEN, ALEN, three descriptor arrays, data).

    Each block's data is held whole (a table can be anywhere in it), so DLEN is capped by
    `block_cap` (the method's block size, or the stage's `limit` when that is smaller). Tables
    are restored in place. With a `limit`, no block is read once that much was output.
    Empty blocks (DLEN 0) are accepted, but not more than DELTA_MAX_EMPTY in a row.
    """

    def __init__(self, src, block_cap: int, chunk: int, limit: int | None = None, stop=None):
        super().__init__(stop)
        self.inp = _Input(src, chunk)
        self.cap = block_cap
        self.left = limit
        self.empty = 0  # empty blocks in a row

    def _need(self, n: int, what: str) -> bytearray:
        data = self.inp.exact(n)
        if len(data) != n:
            raise CodecError(f"the delta data ended inside a block ({what})")
        return data

    def _next(self) -> bytes:
        if self.left is not None and self.left <= 0:
            self.done = True
            return b""
        head = self.inp.exact(4)
        if not head:
            self.done = True
            return b""
        if len(head) != 4:
            raise CodecError("the delta data ended inside a block header")
        dlen = int.from_bytes(head, "little")
        alen = int.from_bytes(self._need(4, "header"), "little")
        if dlen >= I31 or alen >= I31 or alen % 4:
            raise CodecError("bad delta block header")
        if dlen > self.cap:
            raise CodecError("a delta block is larger than its method allows, or runs past "
                             "the files' data further than gitfurl decodes")
        self.empty = self.empty + 1 if dlen == 0 else 0
        if self.empty > DELTA_MAX_EMPTY:
            raise CodecError("too many empty delta blocks in a row")
        count = alen // 4
        if count > dlen // 4 + 1:
            raise CodecError("a delta block lists more tables than gitfurl handles")
        gaps = _u32s(self._need(alen, "tables"))
        shapes = _u32s(self._need(alen, "tables"))
        recs = _u32s(self._need(alen, "tables"))
        data = self._need(dlen, "data")
        pos = 0
        for gap, shape, rows in zip(gaps, shapes, recs):
            if gap >= I31 or rows >= I31:
                raise CodecError("bad delta table")
            width = shape.bit_length() - 1
            if width < 1 or width > 30:
                raise CodecError("bad delta table")
            start = pos + gap
            end = start + width * rows
            if end > dlen:
                raise CodecError("a delta table lies outside its block")
            kept = [c for c in range(width) if shape >> c & 1]
            if rows and len(kept) < width:
                _undelta(data, start, width, rows, kept)
            pos = end
        if self.left is not None:
            self.left -= dlen
        return data


def _undelta(data: bytearray, start: int, width: int, rows: int, kept: list[int]) -> None:
    """Restore one table of data[start:start + width * rows] in place."""
    k = len(kept)
    end = start + width * rows
    diffed = [c for c in range(width) if c not in kept]
    if k:  # kept columns were moved to the front, row by row
        table = bytearray(width * rows)
        for j, c in enumerate(kept):
            table[c::width] = data[start + j:start + k * rows:k]
        for j, c in enumerate(diffed):
            table[c::width] = data[start + k * rows + j:end:width - k]
        data[start:end] = table
        del table
    if rows < 2:
        return
    # runs of adjacent differenced columns: (first column, length)
    runs: list[list[int]] = []
    for c in diffed:
        if runs and runs[-1][0] + runs[-1][1] == c:
            runs[-1][1] += 1
        else:
            runs.append([c, 1])
    for c0, length in runs:
        mask = (1 << (8 * length)) - 1
        if length <= 4 and _LITTLE and array("Q").itemsize == 8:
            # DELTA_ROWS rows at a time: each row's run as a 64-bit value; running sums of
            # fewer than 2^17 values below 2^32 fit, and their low bytes are the run
            # modulo 256^length
            carry = 0
            for r0 in range(0, rows, DELTA_ROWS):
                n = min(DELTA_ROWS, rows - r0)
                lo = start + r0 * width + c0
                hi = start + (r0 + n) * width
                wide = bytearray(8 * n)
                for b in range(length):
                    wide[b::8] = data[lo + b:hi:width]
                sums = array("Q", accumulate(memoryview(wide).cast("Q"), initial=carry))
                del wide
                carry = sums[-1] & mask
                raw = memoryview(sums).cast("B")[8:]
                for b in range(length):
                    data[lo + b:hi:width] = raw[b::8]
                raw.release()
                del sums
        else:
            prev = 0
            for r in range(rows):
                at = start + r * width + c0
                prev = (prev + int.from_bytes(data[at:at + length], "little")) & mask
                data[at:at + length] = prev.to_bytes(length, "little")


class RepStage(_Stage):
    """The long-repeat preprocessor ("rep"): W, then blocks of matches and literals, then 0.

    The window grows as output is produced, up to min(W, `window_cap`); a match that reaches
    further back than that is refused rather than kept in more memory. Matches are copied a
    piece at a time, byte-for-byte semantics (overlapping copies repeat their pattern). With a
    `limit`, the output ends after that many bytes and the rest is never read. Block headers
    and match tables may add up to REP_FREE_OVERHEAD plus REP_OVERHEAD_PER_BYTE per byte of
    output; a stream that gives less output for its size is refused.
    """

    def __init__(self, src, window_cap: int, chunk: int, limit: int | None = None, stop=None):
        super().__init__(stop)
        self.inp = _Input(src, chunk)
        self.limit = limit
        self.overhead = 0  # bytes of block headers and match tables read so far
        self.chunk = chunk
        self.wcap = window_cap
        self.window = 0  # W from the stream; 0 until read
        self.cap = 0  # bytes kept: min(W, window_cap)
        self.ring = bytearray()
        self.wpos = 0  # next write index once the ring is full
        self.total = 0
        self.lens = self.dists = self.lits = None
        self.k = 0  # next match/literal-run index in the current block
        self.lit_left = 0
        self.match_left = 0
        self.match_dist = 0

    def _word(self, what: str) -> int:
        data = self.inp.exact(4)
        if len(data) != 4:
            raise CodecError(f"the rep data ended early ({what})")
        return int.from_bytes(data, "little")

    def _start(self) -> None:
        w = self._word("window size")
        if w == 0:
            raise CodecError("bad rep window size")
        if w > farc.DICT_CAP:
            raise CodecError(f"a rep window over the {farc.DICT_CAP >> 20} MiB limit")
        self.window, self.cap = w, min(w, self.wcap)

    def _block(self) -> bool:
        """Read the next block's tables; False at the end marker."""
        self.stop()
        if self.overhead > REP_FREE_OVERHEAD + REP_OVERHEAD_PER_BYTE * self.total:
            raise CodecError("rep data that gives too little output for its size")
        size = self._word("block header (no end marker)")
        if size == 0:
            if self.inp.some(1):
                raise CodecError("data after the end of the rep stream")
            return False
        if size < 8:
            raise CodecError("bad rep block")
        count = self._word("block")
        if count >= I31 or 12 * count + 8 > size:
            raise CodecError("bad rep block")
        if count > _rep_match_cap(self.wcap):
            raise CodecError("a rep block has more matches than gitfurl handles")
        tables = self.inp.exact(12 * count + 4)
        if len(tables) != 12 * count + 4:
            raise CodecError("the rep data ended inside a block")
        self.overhead += 12 * count + 12
        vals = _u32s(tables)
        if max(vals) >= I31:
            raise CodecError("bad rep block")
        self.lens = vals[:count]
        self.dists = vals[count:2 * count]
        self.lits = vals[2 * count:]
        if 12 * count + 8 + sum(self.lits) != size:
            raise CodecError("bad rep block (sizes do not agree)")
        self.k, self.lit_left = 0, self.lits[0]
        return True

    def _next(self) -> bytes:
        if not self.window:
            self._start()
        steps = 0
        while True:
            if self.limit is not None and self.total >= self.limit:
                self.done = True
                return b""
            room = self.chunk if self.limit is None else min(self.chunk,
                                                             self.limit - self.total)
            if self.match_left:  # a match comes before the literal run that follows it
                n = min(room, self.match_left)
                self.match_left -= n
                return self._copy(self.match_dist, n)
            if self.lit_left:
                data = self.inp.some(min(room, self.lit_left))
                if not data:
                    raise CodecError("the rep data ended inside a block")
                self.lit_left -= len(data)
                self._put(data)
                return data
            if self.lens is not None and self.k < len(self.lens):
                length, dist = self.lens[self.k], self.dists[self.k]
                self.k += 1
                self.lit_left = self.lits[self.k]
                if length:
                    if dist == 0 or dist > self.window or dist > self.total:
                        raise CodecError("a rep match points before the start of the data")
                    if dist > self.cap:
                        raise CodecError("a rep match reaches further back than gitfurl keeps")
                    self.match_left, self.match_dist = length, dist
                else:
                    steps += 1
                    if steps % STOP_EVERY == 0:
                        self.stop()
                continue
            if not self._block():
                self.done = True
                return b""

    def _put(self, data: bytes) -> None:
        self.total += len(data)
        ring, cap = self.ring, self.cap
        if len(ring) < cap:
            room = cap - len(ring)
            ring += data[:room]
            data = data[room:]
            if not data:
                return
        if len(data) >= cap:  # only the last cap bytes stay; the oldest is at index 0
            ring[:] = data[-cap:]
            self.wpos = 0
            return
        k = min(len(data), cap - self.wpos)
        ring[self.wpos:self.wpos + k] = data[:k]
        if k < len(data):
            ring[:len(data) - k] = data[k:]
        self.wpos = (self.wpos + len(data)) % cap

    def _get(self, dist: int, n: int) -> bytes:
        """n bytes (n <= dist) starting dist bytes back from the next byte to write."""
        ring, cap = self.ring, self.cap
        if len(ring) < cap:
            start = len(ring) - dist
            return bytes(ring[start:start + n])
        start = (self.wpos - dist) % cap
        if start + n <= cap:
            return bytes(ring[start:start + n])
        return bytes(ring[start:]) + bytes(ring[:n - (cap - start)])

    def _copy(self, dist: int, n: int) -> bytes:
        if n <= dist:
            data = self._get(dist, n)
        else:  # overlapping: the last `dist` bytes repeat
            data = (self._get(dist, dist) * (n // dist + 1))[:n]
        self._put(data)
        return data


def open_chain(method: farc.Method, fd: int, offset: int, compsize: int, out_size: int,
               chunk: int, stop):
    """The leftmost stage of the block's chain (read(n) gives the file data). Every decoder is
    made here, before anything is read; raises StartError when one cannot be started."""
    plans = stage_plan(method, out_size)
    reader = Source(fd, offset, compsize, chunk, stop)
    for p in reversed(plans):
        name = p.stage.name
        if name == "lzma":
            reader = LzmaStage(reader, p.stage.lzma, p.budget, chunk, p.limit, stop)
        elif name == "exe":
            reader = ExeStage(reader, chunk, stop)
        elif name == "delta":
            reader = DeltaStage(reader, p.budget, chunk, p.limit, stop)
        elif name == "rep":
            reader = RepStage(reader, p.budget, chunk, p.limit, stop)
        # storing: output equals input, nothing to add
    return reader
