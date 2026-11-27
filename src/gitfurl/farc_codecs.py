"""Streaming decoders for FreeArc method chains: storing, lzma, exe, x86, delta, rep, 4x4 (with
lzma inside), srep, rolz (in farc_rolz.py) and the cm stage (in gitfurl's optional native
module, gitfurl._codecs).

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

import hashlib
import heapq
import lzma
import os
import re
import sys
from array import array
from dataclasses import dataclass
from itertools import accumulate

from gitfurl import farc, farc_rolz

try:  # the optional native decoder module (native/gitfurl-codecs); only the cm stage needs it
    from gitfurl import _codecs
except ImportError:  # pragma: no cover - depends on the installation
    _codecs = None

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
X4_STORED = 0xFFFFFFFF  # a 4x4 piece header's size field for a piece kept as it is
SREP_MEMORY_CAP = 1 << 30  # srep's history, pending matches and buffered input, together
SREP_MIN_BUDGET = 16 << 20  # ... at least this much, however small the block
SREP_PENDING_COST = 192  # bytes counted for each pending srep match besides its data
SREP_RECORD_COST = 72  # bytes counted for each match record of one srep block
CHECK_SLICE = 1 << 20  # an srep block checksum is computed this many bytes at a time
CHECK_BLOCKS = 256  # ... or, for VHASH, this many NH blocks, with a Stop check in between
# One cm-stage job being decoded: its output (at most 512 MiB, held until it is passed on),
# its models (under 10 MiB) and one chunk of its input.
CM_JOB_MEMORY = 520 << 20
CM_THREADS = 1  # cm-stage jobs decoded at once, each on its own thread
CM_HEADER = 11  # bytes of the cm stage's stream header
CM_MISSING = "the cm stage needs gitfurl's native decoder module, which is not installed"

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
    budget: int  # lzma: dictionary; rep: window kept; delta, x86: largest block
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
        elif st.name == "4x4":  # one lzma decoder at a time, like lzma
            d = st.lzma["dict_size"]
            if limit is not None:
                d = min(d, max(4096, limit))
            out.append(StagePlan(st, d, d, limit))
        elif st.name == "x86":
            # a code block is held whole, encoded and decoded (one that runs past the limit
            # is refused); the encoded part is converted in place, half a stream at a time
            b = st.size if limit is None else min(st.size, limit)
            enc = x86_input_cap(st.size)
            out.append(StagePlan(st, b, b + enc + enc // 2 + X86_SLACK, limit))
        elif st.name == "srep":
            budget = srep_budget(limit)
            out.append(StagePlan(st, budget, budget + srep_block_memory(st.size), limit))
        elif st.name == "cm":  # budget: jobs decoded at once
            out.append(StagePlan(st, CM_THREADS, CM_THREADS * CM_JOB_MEMORY, limit))
        elif st.name == "rolz":
            # the window comes from the data; one over `budget` is refused before it is used
            w = rolz_window(limit)
            out.append(StagePlan(st, w, farc_rolz.memory_for(w), limit))
        else:  # storing
            out.append(StagePlan(st, 0, 0, limit))
        if st.name == "exe" and limit is not None:
            limit += EXE_SLACK
        elif st.name not in _SIZE_KEEPING:
            limit = None
    return out


def rolz_window(limit: int | None) -> int:
    """The largest rolz window accepted: WINDOW_CAP, or the files' size (rounded up to 1 KiB,
    as the window is) when that is smaller and the rolz output reaches the files unchanged."""
    if limit is None:
        return farc_rolz.WINDOW_CAP
    return min(farc_rolz.WINDOW_CAP, max(1024, -(-limit // 1024) * 1024))


def _rep_match_cap(window: int) -> int:
    return min(REP_MAX_MATCHES, max(REP_MIN_MATCHES, window // 8))


def srep_budget(limit: int | None) -> int:
    """Bytes an srep stage may keep across blocks: earlier output (formats 1 and 2), pending
    matches (formats 3 and 4) and, for format 4 read from another stage, its whole input."""
    if limit is None:
        return SREP_MEMORY_CAP
    return min(SREP_MEMORY_CAP, max(SREP_MIN_BUDGET, 2 * limit))


def _srep_list_cap(block_cap: int) -> int:
    """Largest literal bytes, match list, or both together, of one srep block."""
    return 2 * block_cap + 1040


def srep_block_memory(block_cap: int) -> int:
    """Bytes one srep block may take while it is decoded: its output, its literals and match
    list, a format 4 block's part of the index, and the records of one block."""
    lists = _srep_list_cap(block_cap)
    return block_cap + 2 * lists + SREP_RECORD_COST * (lists // 16)


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

    def at_end(self) -> bool:
        """True when the input has ended (reads ahead at most one chunk to find out)."""
        if self.at < len(self.buf):
            return False
        if not self.ended:
            self.buf, self.at = self.src.read(self.chunk), 0
            if self.buf:
                return False
            self.ended = True
        return True

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


class X4Stage(_Stage):
    """The 4x4 wrapper with lzma inside: a version word (0), then pieces, each an 8-byte header
    (decoded size, or X4_STORED for a piece kept as it is; then the payload's length) and its
    payload. Each lzma piece is a complete raw LZMA1 stream with its end marker, decoded by a
    fresh decoder fed only that piece's payload, a chunk at a time; it must give exactly the
    header's size. With a `limit`, the output ends after that many bytes and the rest is never
    read."""

    def __init__(self, src, opts: dict, dict_size: int, chunk: int, limit: int | None = None,
                 stop=None):
        super().__init__(stop)
        self.inp = _Input(src, chunk)
        self.opts, self.dict, self.chunk, self.left = opts, dict_size, chunk, limit
        self.started = False
        self.kind = ""  # "" between pieces, else "stored" or "lzma"
        self.pay = 0  # payload bytes of the current piece not read yet
        self.want = 0  # output the current lzma piece still owes
        self.idle = 0
        self.dec = self._decoder(dict_size)  # fails here, before anything is read
        self.dec = None

    def _decoder(self, d: int):
        try:
            return lzma.LZMADecompressor(
                lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1,
                                           **dict(self.opts, dict_size=d)}])
        except (lzma.LZMAError, MemoryError, ValueError) as exc:
            raise StartError("the lzma decoder of a 4x4 stage could not be started (not "
                             "enough memory?)") from exc

    def _header(self) -> bool:
        """Read the next piece's header; False at the end of the stream."""
        self.stop()
        if not self.started:
            self.started = True
            head = self.inp.exact(4)
            if not head:
                return False
            if len(head) != 4:
                raise CodecError("the 4x4 data ended inside its header")
            if head != b"\0\0\0\0":
                raise CodecError("a 4x4 stream of a version gitfurl does not handle")
        head = self.inp.exact(8)
        if not head:
            return False
        if len(head) != 8:
            raise CodecError("the 4x4 data ended inside a piece header")
        size, pay = int.from_bytes(head[:4], "little"), int.from_bytes(head[4:], "little")
        if size == X4_STORED:
            if not 0 < pay < I31:
                raise CodecError("bad 4x4 piece header")
            self.kind = "stored"
        else:
            if not 0 < size < I31 or not 0 < pay <= size + size // 4 + 16384:
                raise CodecError("bad 4x4 piece header")
            self.kind, self.want = "lzma", size
            # an LZMA stream never reaches further back than what it has produced
            self.dec = self._decoder(max(4096, min(self.dict, size)))
        self.pay = pay
        return True

    def _feed(self) -> bytes:
        """The current piece's next payload bytes for the lzma decoder."""
        if not self.pay:
            raise CodecError("a 4x4 piece ended before its lzma end marker")
        data = self.inp.some(min(self.chunk, self.pay))
        if not data:
            raise CodecError("the 4x4 data ended inside a piece")
        self.pay -= len(data)
        return data

    def _lzma(self, data: bytes, most: int) -> bytes:
        try:
            out = self.dec.decompress(data, max_length=most)
        except (lzma.LZMAError, EOFError) as exc:
            raise CodecError("damaged lzma data in a 4x4 piece") from exc
        self.idle = 0 if data or out else self.idle + 1
        if self.idle > 1000:  # no input taken and no output given: never seen; refuse
            raise CodecError("the lzma decoder stopped making progress")
        return out

    def _piece_end(self) -> None:
        """All of the piece's output was given: its stream must end here, with nothing
        after the end marker."""
        while not self.dec.eof:
            data = self._feed() if self.dec.needs_input else b""
            if self._lzma(data, 1):
                raise CodecError("a 4x4 piece gives more data than its header says")
        if self.dec.unused_data or self.pay:
            raise CodecError("data after the end of a 4x4 piece's lzma stream")
        self.kind, self.dec = "", None

    def _next(self) -> bytes:
        while True:
            if self.left == 0:
                self.done = True
                return b""
            room = self.chunk if self.left is None else min(self.chunk, self.left)
            if not self.kind and not self._header():
                self.done = True
                return b""
            if self.kind == "stored":
                out = self.inp.some(min(room, self.pay))
                if not out:
                    raise CodecError("the 4x4 data ended inside a piece")
                self.pay -= len(out)
                if not self.pay:
                    self.kind = ""
            else:
                if not self.want:
                    self._piece_end()
                    continue
                data = self._feed() if self.dec.needs_input else b""
                out = self._lzma(data, min(room, self.want))
                self.want -= len(out)
                if self.dec.eof:
                    if self.want:
                        raise CodecError("a 4x4 piece gives less data than its header says")
                    self._piece_end()
                if not out:
                    continue
            if self.left is not None:
                self.left -= len(out)
            return out


# --- srep block checksums -------------------------------------------------------------------

M64 = (1 << 64) - 1
_AES_SBOX: bytes = b""


def _aes_tables() -> bytes:
    """The AES S-box (FIPS-197), computed: the inverse in GF(2^8), then the affine map."""
    global _AES_SBOX
    if not _AES_SBOX:
        exp, log = [0] * 255, [0] * 256
        x = 1
        for i in range(255):  # powers of the generator 3
            exp[i], log[x] = x, i
            x ^= (x << 1) ^ (0x11B if x & 0x80 else 0)
        box = bytearray(256)
        for a in range(256):
            b = exp[(255 - log[a]) % 255] if a else 0
            r = b
            for k in range(1, 5):
                r ^= ((b << k) | (b >> (8 - k))) & 0xFF
            box[a] = r ^ 0x63
        _AES_SBOX = bytes(box)
    return _AES_SBOX


def _xtime(b: int) -> int:
    return ((b << 1) ^ (0x1B if b & 0x80 else 0)) & 0xFF


def aes_encrypt_block(key: bytes, block: bytes) -> bytes:
    """AES (FIPS-197) encryption of one 16-byte block with a 16-, 24- or 32-byte key. Used only
    to derive srep's VHASH keys from a stream's seed (a few hundred blocks per stream)."""
    sbox = _aes_tables()
    nk = len(key) // 4
    rounds = nk + 6
    w = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, 4 * (rounds + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = [sbox[b] for b in t[1:] + t[:1]]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            t = [sbox[b] for b in t]
        w.append([a ^ b for a, b in zip(w[i - nk], t)])
    keys = [[b for word in w[4 * r:4 * r + 4] for b in word] for r in range(rounds + 1)]
    st = [a ^ b for a, b in zip(block, keys[0])]
    for r in range(1, rounds + 1):
        st = [sbox[b] for b in st]
        st = [st[(i + 4 * (i % 4)) % 16] for i in range(16)]  # row i%4 moves left by i%4
        if r != rounds:
            mixed = []
            for c in range(4):
                a = st[4 * c:4 * c + 4]
                total = a[0] ^ a[1] ^ a[2] ^ a[3]
                mixed += [a[j] ^ total ^ _xtime(a[j] ^ a[(j + 1) % 4]) for j in range(4)]
            st = mixed
        st = [a ^ b for a, b in zip(st, keys[r])]
    return bytes(st)


class Vhash:
    """VHASH with a 128-bit result (two lanes), as srep uses it: an AES-256 key (the stream's
    32-byte seed) and an NH block of `block` bytes (4096 in srep)."""

    P127 = (1 << 127) - 1
    P64 = (1 << 64) - 257

    def __init__(self, key: bytes, block: int = 4096):
        self.block = block
        nwords = block // 8 + 2

        def words(first: int, count: int, start: int = 0) -> list[int]:
            out: list[int] = []
            ctr = start
            while len(out) < count:
                enc = aes_encrypt_block(key, bytes([first]) + bytes(14) + bytes([ctr & 0xFF]))
                out += [int.from_bytes(enc[:8], "big"), int.from_bytes(enc[8:], "big")]
                ctr += 1
            return out[:count]

        nh = words(0x80, nwords)
        self.even = (nh[0:nwords - 2:2], nh[2:nwords:2])  # lane 0, lane 1
        self.odd = (nh[1:nwords - 2:2], nh[3:nwords:2])
        poly = [k & 0x1FFFFFFF1FFFFFFF for k in words(0xC0, 4)]
        self.poly = ((poly[0] << 64) | poly[1], (poly[2] << 64) | poly[3])
        l3: list[tuple[int, int]] = []
        ctr = 0
        while len(l3) < 2:
            k1, k2 = words(0xE0, 2, ctr)
            ctr += 1
            if k1 < self.P64 and k2 < self.P64:
                l3.append((k1, k2))
        self.l3 = l3

    def digest(self, data, stop=None) -> bytes:
        """The 16 bytes srep stores: lane 0's result, then lane 1's, little-endian. stop() is
        called every CHECK_BLOCKS NH blocks."""
        full, rest = divmod(len(data), self.block)
        view = memoryview(data)
        accs = [self.poly[0], self.poly[1]]
        for i in range(full + (1 if rest else 0)):
            if stop is not None and i and i % CHECK_BLOCKS == 0:
                stop()
            if i < full:
                c = _u64s(view[i * self.block:(i + 1) * self.block])
            else:  # the last part, zero-padded to a multiple of 16 bytes
                c = _u64s(bytes(view[full * self.block:]) + bytes(-rest % 16))
            for lane in (0, 1):
                ke, ko, key = self.even[lane], self.odd[lane], self.poly[lane]
                nh = sum(((a + x) & M64) * ((b + y) & M64)
                         for a, b, x, y in zip(c[0::2], c[1::2], ke, ko)) & ((1 << 126) - 1)
                accs[lane] = (accs[lane] * key + nh if i else nh + key) % self.P127
        out = b""
        for lane in (0, 1):
            acc = accs[lane]
            k1, k2 = self.l3[lane]
            v = (acc + ((8 * rest) << 64)) % self.P127
            q, t = divmod(v, (1 << 64) - (1 << 32))
            out += (((q + k1) % self.P64) * ((t + k2) % self.P64) % self.P64).to_bytes(
                8, "little")
        return out


def _u64s(data):
    """Little-endian 64-bit values of data (a multiple of 8 bytes long)."""
    a = array("Q")
    if a.itemsize != 8:  # pragma: no cover - no such platform in use
        return [int.from_bytes(data[i:i + 8], "little") for i in range(0, len(data), 8)]
    a.frombytes(bytes(data))
    if not _LITTLE:  # pragma: no cover - no big-endian machine in use
        a.byteswap()
    return a


def _sip_words(data, stop):
    """data's 64-bit words (all but its last n % 8 bytes), then its last word, padded and
    holding the length; one slice of CHECK_SLICE bytes at a time, stop() between slices."""
    n = len(data)
    view = memoryview(data)
    body = n - n % 8
    for at in range(0, body, CHECK_SLICE):
        if at and stop is not None:
            stop()
        yield from _u64s(view[at:min(at + CHECK_SLICE, body)])
    yield int.from_bytes(bytes(view[body:]) + bytes(7 - n % 8) + bytes([n & 0xFF]), "little")


def siphash24(key: bytes, data, stop=None) -> int:
    """SipHash-2-4 (Aumasson and Bernstein) of data with a 16-byte key: k0, k1 little-endian.
    stop() is called between slices of CHECK_SLICE bytes."""
    k0 = int.from_bytes(key[:8], "little")
    k1 = int.from_bytes(key[8:16], "little")
    v0 = k0 ^ 0x736F6D6570736575
    v1 = k1 ^ 0x646F72616E646F6D
    v2 = k0 ^ 0x6C7967656E657261
    v3 = k1 ^ 0x7465646279746573
    for m in _sip_words(data, stop):
        v3 ^= m
        for _ in range(2):
            v0 = (v0 + v1) & M64
            v1 = ((v1 << 13) | (v1 >> 51)) & M64 ^ v0
            v0 = ((v0 << 32) | (v0 >> 32)) & M64
            v2 = (v2 + v3) & M64
            v3 = ((v3 << 16) | (v3 >> 48)) & M64 ^ v2
            v0 = (v0 + v3) & M64
            v3 = ((v3 << 21) | (v3 >> 43)) & M64 ^ v0
            v2 = (v2 + v1) & M64
            v1 = ((v1 << 17) | (v1 >> 47)) & M64 ^ v2
            v2 = ((v2 << 32) | (v2 >> 32)) & M64
        v0 ^= m
    v2 ^= 0xFF
    for _ in range(4):
        v0 = (v0 + v1) & M64
        v1 = ((v1 << 13) | (v1 >> 51)) & M64 ^ v0
        v0 = ((v0 << 32) | (v0 >> 32)) & M64
        v2 = (v2 + v3) & M64
        v3 = ((v3 << 16) | (v3 >> 48)) & M64 ^ v2
        v0 = (v0 + v3) & M64
        v3 = ((v3 << 21) | (v3 >> 43)) & M64 ^ v0
        v2 = (v2 + v1) & M64
        v1 = ((v1 << 17) | (v1 >> 47)) & M64 ^ v2
        v2 = ((v2 << 32) | (v2 >> 32)) & M64
    return v0 ^ v1 ^ v2 ^ v3


# checksum kind -> (seed length, checksum length)
SREP_KINDS = {0: (0, 16), 1: (0, 16), 2: (0, 20), 3: (0, 64), 4: (32, 16), 5: (16, 8)}


def _hashlib_checker(name: str):
    def check(data, stop) -> bytes:
        h = hashlib.new(name, usedforsecurity=False)
        view = memoryview(data)
        for at in range(0, len(data), CHECK_SLICE):
            if at:
                stop()
            h.update(view[at:at + CHECK_SLICE])
        return h.digest()
    return check


def _srep_checker(kind: int, seed: bytes):
    """A function (data, stop) giving a block's checksum, or None for kind 1 (no checksum).
    Each one calls stop() between slices of the block (CHECK_SLICE bytes, or CHECK_BLOCKS
    NH blocks of VHASH)."""
    if kind in (0, 2, 3):
        return _hashlib_checker({0: "md5", 2: "sha1", 3: "sha512"}[kind])
    if kind == 4:
        return Vhash(seed).digest
    if kind == 5:
        return lambda data, stop: siphash24(seed, data, stop).to_bytes(8, "little")
    return None


# --- srep ---------------------------------------------------------------------------------

SREP_SIG = bytes.fromhex("1718352653524550")
SREP_FOOTER_SIG = (0xAFBAADAC, 0xD9CAE7E8)
_M32 = 0xFFFFFFFF


class _Spool:
    """Random reads of a format 4 srep stream: from the archive itself when the stage reads
    the block's stored bytes, else from a copy of its whole input held in memory."""

    def __init__(self, size: int, data=None, fd: int = -1, base: int = 0):
        self.size, self.data, self.fd, self.base = size, data, fd, base

    def read(self, pos: int, n: int) -> bytes:
        if pos < 0 or n < 0 or pos + n > self.size:
            raise CodecError("damaged srep data (a part lies outside the stream)")
        if self.data is not None:
            return bytes(self.data[pos:pos + n])
        try:
            data = os.pread(self.fd, n, self.base + pos)
        except OSError as exc:
            raise CodecError(f"the archive could not be read ({exc.strerror or exc})") from exc
        if len(data) != n:
            raise CodecError("the archive could not be read completely (did it change?)")
        return data


class SrepStage(_Stage):
    """srep (long-range matches): formats 1 and 2 (matches point back into earlier output), 3
    (matches recorded at their source and replayed later) and 4 (as 3, with the match lists in
    an index at the end of the stream), with md5, sha1, sha512, VHASH or SipHash block
    checksums, or none. An optional flag byte may come first (0x01: srep data follows; 0x00:
    the data follows as it is).

    Caps: a block's size (`block_cap`), its literals and match list (2 * block_cap + 1040
    bytes each and together), and `budget` bytes for what is kept across blocks: the output
    that matches may reach back into (formats 1 and 2; a match further back than that is
    refused), the data of pending matches (formats 3 and 4), and the input of a format 4
    stream that is not read from the archive directly. A stream that needs more is refused.
    Output past `limit` is refused (more data than the block's files hold).
    """

    def __init__(self, src, block_cap: int, budget: int, chunk: int, limit: int | None = None,
                 stop=None):
        super().__init__(stop)
        self.inp = _Input(src, chunk)
        self.direct = (src.fd, src.pos, src.left) if isinstance(src, Source) else None
        self.bcap, self.budget, self.chunk, self.limit = block_cap, budget, chunk, limit
        self.lists = _srep_list_cap(block_cap)
        self.version = 0  # 0 until the header is read; -1: the data is stored as it is
        self.total = 0  # output so far
        self.hist = bytearray()  # formats 1 and 2: the last output, from position hbase
        self.hbase = 0
        self.pending: list = []  # formats 3 and 4: heap of (dest, number, data)
        self.held = 0  # bytes counted against the budget for pending matches
        self.seq = 0
        self.spool: _Spool | None = None

    # -- header ----------------------------------------------------------------------------

    def _start(self) -> None:
        first = self.inp.exact(1)
        if not first:
            raise CodecError("the srep data is empty")
        flag = 0
        if first[0] == 0:
            self.version = -1
            return
        if first[0] == 1:
            flag, head = 1, self.inp.exact(16)
        elif first[0] == SREP_SIG[0]:
            head = first + self.inp.exact(15)
        else:
            raise CodecError("not srep data (unknown first byte)")
        if len(head) != 16 or head[:8] != SREP_SIG:
            raise CodecError("not srep data, or a kind gitfurl does not handle")
        version, kind, slen = head[8], head[9], head[10]
        hlen = (head[11] + 16) & 0xFF
        if version not in (1, 2, 3, 4):
            raise CodecError("an srep stream of a format gitfurl does not handle")
        if SREP_KINDS.get(kind) != (slen, hlen):
            raise CodecError("an srep stream with a checksum kind gitfurl does not handle")
        self.base = int.from_bytes(head[12:16], "little")
        if version == 1 and not self.base:
            raise CodecError("bad srep header")
        seed = self.inp.exact(slen)
        if len(seed) != slen:
            raise CodecError("the srep data ended inside its header")
        self.hlen = hlen
        self.check = _srep_checker(kind, bytes(seed))
        self.version = version
        self.start = 16 + slen  # where the first block starts in the stream
        if version == 4:
            self._footer(flag, head + seed)

    def _footer(self, flag: int, head: bytes) -> None:
        if self.direct is not None:
            fd, pos, left = self.direct
            self.spool = _Spool(left - flag, fd=fd, base=pos + flag)
        else:
            data = bytearray(head)
            while True:
                piece = self.inp.some(self.chunk)
                if not piece:
                    break
                data += piece
                if len(data) > self.budget:
                    raise CodecError("srep data that needs more memory than gitfurl allows")
                self.stop()
            self.held = len(data)
            self.spool = _Spool(len(data), data=data)
        sp = self.spool
        size = sp.size
        if size < self.start + 24:
            raise CodecError("damaged srep data (no index footer)")
        foot = sp.read(size - 24, 24)
        lo, hi, flen, fver, sig1, sig2 = (int.from_bytes(foot[i:i + 4], "little")
                                          for i in range(0, 24, 4))
        if (sig1, sig2) != SREP_FOOTER_SIG or fver & 0xFF != 1:
            raise CodecError("damaged srep data (bad index footer)")
        isize = lo | hi << 32
        if flen < 24 or (flen - 24) % 4 or isize % 16 or self.start + flen + isize > size:
            raise CodecError("damaged srep data (bad index footer)")
        count = (flen - 24) // 4
        self.index_at = size - flen - isize
        if count * (12 + self.hlen) > self.index_at - self.start:
            raise CodecError("damaged srep data (bad index footer)")
        self._hold(4 * count)
        sizes = _u32s(bytearray(sp.read(size - flen, 4 * count)))
        if sum(sizes) != isize or any(n % 16 for n in sizes):
            raise CodecError("damaged srep data (bad index)")
        self.sizes = sizes
        self.k = 0  # next block
        self.at = self.start  # next block's position
        self.iat = self.index_at  # its part of the index

    def _hold(self, n: int) -> None:
        self.held += n
        if self.held > self.budget:
            raise CodecError("srep data that needs more memory than gitfurl allows")

    # -- blocks ----------------------------------------------------------------------------

    def _block_header(self):
        """(literal count, size, match list size, checksum) of the next block, or None at
        the end of the stream."""
        hsize = 12 + self.hlen
        if self.version == 4:
            if self.k == len(self.sizes):
                return None
            if self.at + hsize > self.index_at:
                raise CodecError("damaged srep data (a block runs into the index)")
            head = self.spool.read(self.at, hsize)
            self.at += hsize
        else:
            head = self.inp.exact(hsize)
            if not head:
                return None
            if len(head) != hsize:
                raise CodecError("the srep data ended inside a block header")
        lits, size, mlen = (int.from_bytes(head[i:i + 4], "little") for i in (0, 4, 8))
        if self.version != 4 and lits == 0 and size == 0 and mlen == 0 \
                and self.inp.at_end():
            return None  # srep's own end mark, accepted only as the last thing
        if not 0 < size <= self.bcap:
            raise CodecError("an srep block is empty or larger than gitfurl handles")
        rec = 12 if self.version == 1 else 16
        if lits > size or mlen % rec or mlen > self.lists or lits + mlen > self.lists:
            raise CodecError("bad srep block header")
        if self.version == 4 and mlen:
            raise CodecError("bad srep block header")
        if self.limit is not None and self.total + size > self.limit:
            raise CodecError("srep data that decodes to more than the block's files hold")
        return lits, size, mlen, bytes(head[12:])

    def _block_body(self, lits: int, mlen: int) -> tuple[memoryview, bytes]:
        """(match list words, literal bytes) of the current block."""
        if self.version == 4:
            mlen = self.sizes[self.k]
            if mlen > self.lists:
                raise CodecError("an srep block lists more matches than gitfurl handles")
            if self.at + lits > self.index_at:
                raise CodecError("damaged srep data (a block runs into the index)")
            recs = bytearray(self.spool.read(self.iat, mlen))
            self.iat += mlen
            body = self.spool.read(self.at, lits)
            self.at += lits
            self.k += 1
            return _u32s(recs), body
        body = self.inp.exact(mlen + lits)
        if len(body) != mlen + lits:
            raise CodecError("the srep data ended inside a block")
        recs = body[:mlen]
        return _u32s(recs), bytes(body[mlen:])

    def _next(self) -> bytes:
        if not self.version:
            self._start()
        if self.version == -1:
            out = self.inp.some(self.chunk)
            if not out:
                self.done = True
            elif self.limit is not None and self.total + len(out) > self.limit:
                raise CodecError("srep data that decodes to more than the block's files hold")
            self.total += len(out)
            return out
        head = self._block_header()
        if head is None:
            self._finish()
            self.done = True
            return b""
        lits, size, mlen, check = head
        words, literals = self._block_body(lits, mlen)
        if self.version in (1, 2):
            out = self._backward(words, literals, size)
        else:
            out = self._forward(words, literals, size)
        if self.check is not None and self.check(out, self.stop)[:self.hlen] != check:
            raise CodecError("an srep block failed its checksum")
        if (self.k == len(self.sizes)) if self.version == 4 else self.inp.at_end():
            self._finish()  # checked now: the files may need no further read
            self.done = True
        return out

    def _finish(self) -> None:
        if self.pending:
            raise CodecError("damaged srep data (a match points past its end)")
        if self.version == 4 and self.at != self.index_at:
            raise CodecError("damaged srep data (the blocks do not end where the index starts)")

    def _backward(self, words, literals: bytes, size: int) -> bytearray:
        """Formats 1 and 2: each record is a literal run, then a match from earlier output."""
        hist, lit, left = self.hist, 0, size
        start = self.total
        unit = self.base if self.version == 1 else 1
        step = 3 if self.version == 1 else 4
        nlit = len(literals)
        for r in range(0, len(words), step):
            if r % (step * STOP_EVERY) == 0 and r:
                self.stop()
            gap = words[r]
            if self.version == 1:
                offset = words[r + 1] * unit
                length = (words[r + 2] * unit + self.base) & _M32
            else:
                offset = words[r + 1] | words[r + 2] << 32
                length = (words[r + 3] + self.base) & _M32
            if gap > nlit - lit or gap + length > left:
                raise CodecError("bad srep match (past the end of its block)")
            hist += literals[lit:lit + gap]
            lit += gap
            left -= gap + length
            dest = start + size - left - length
            src = (dest // unit) * unit - offset
            if offset > (dest // unit) * unit or src >= dest:
                raise CodecError("bad srep match (its source is not before it)")
            if src < self.hbase:
                raise CodecError("an srep match reaches further back than gitfurl keeps")
            i, dist = src - self.hbase, dest - src
            if length <= dist:
                hist += hist[i:i + length]
            else:  # overlapping: the last `dist` bytes repeat
                hist += (hist[i:i + dist] * (length // dist + 1))[:length]
        if nlit - lit != left:
            raise CodecError("bad srep block (its literals do not fill it)")
        hist += literals[lit:]
        self.total += size
        out = hist[len(hist) - size:]
        if len(hist) > self.budget:  # keep only what the budget allows
            cut = len(hist) - self.budget
            del hist[:cut]
            self.hbase += cut
        return out

    def _forward(self, words, literals: bytes, size: int) -> bytearray:
        """Formats 3 and 4: records name a source in this block and where it is copied to.

        Each record is kept as one int, (dest - start) << 2w | length << w | (src - start)
        with w the bit length of the block cap (length and src - start are below 2 ** w), so
        a block's records take one small int each (SREP_RECORD_COST) and sort by dest."""
        start, end = self.total, self.total + size
        w = self.bcap.bit_length()
        mask = (1 << w) - 1
        local: list[int] = []  # records due in this block
        later: list[int] = []  # records due in a later block
        anchor = start
        for r in range(0, len(words), 4):
            if r % (4 * STOP_EVERY) == 0 and r:
                self.stop()
            src = anchor + words[r]
            offset = words[r + 1] | words[r + 2] << 32
            length = (words[r + 3] + self.base) & _M32
            if src >= end or not length or src + length > end or not offset:
                raise CodecError("bad srep match")
            dest = src + offset
            if self.limit is not None and dest + length > self.limit:
                raise CodecError("damaged srep data (a match points past its end)")
            (local if dest < end else later).append(
                (dest - start) << (2 * w) | length << w | (src - start))
            anchor = src
        local.sort()
        out = bytearray()
        lit, cur = 0, start
        nlit = len(literals)
        pending, k, steps = self.pending, 0, 0
        while True:
            # the next copy by dest: a pending match (data from an earlier block) or a record
            nxt = start + (local[k] >> 2 * w) if k < len(local) else end
            if pending and pending[0][0] < nxt:
                dest, _, src = heapq.heappop(pending)
                self.held -= len(src) + SREP_PENDING_COST
                length = len(src)
            elif k < len(local):
                v = local[k]
                k += 1
                dest, length, src = nxt, v >> w & mask, v & mask
            else:
                break
            steps += 1
            if steps % STOP_EVERY == 0:
                self.stop()
            if dest < cur or dest + length > end:
                raise CodecError("bad srep match (matches overlap or cross a block)")
            n = dest - cur
            if n > nlit - lit:
                raise CodecError("bad srep block (too few literals)")
            out += literals[lit:lit + n]
            lit += n
            if isinstance(src, bytes):
                out += src
            else:
                i, dist = src, dest - start - src
                if length <= dist:
                    out += out[i:i + length]
                else:  # overlapping: the last `dist` bytes repeat
                    out += (out[i:i + dist] * (length // dist + 1))[:length]
            cur = dest + length
        del local
        if nlit - lit != end - cur:
            raise CodecError("bad srep block (its literals do not fill it)")
        out += literals[lit:]
        for v in later:
            length, i = v >> w & mask, v & mask
            self._hold(length + SREP_PENDING_COST)
            self.seq += 1
            heapq.heappush(self.pending, (start + (v >> 2 * w), self.seq, bytes(out[i:i + length])))
        self.total = end
        return out


# --- x86 (the code filter FreeArc calls dispack070) -----------------------------------------

X86_DATA = 0xC71B3AE1  # frame tags; other values from X86_TAGS to X86_TAGS + 15 are refused
X86_EXE = 0xC71B3AE2
X86_TAGS = 0xC71B3AE0
X86_BASE = 0x40000000  # address of the stream's first output byte
X86_WRAP = 0xC0000000  # ... an address that reaches this after a frame drops by 2 GiB
X86_STREAMS = 19
X86_HEAD = 4 * X86_STREAMS  # a code block's table of stream sizes
X86_ROUND = 4096  # opcode-stream bytes decoded between two checks of the output (and Stop)
X86_SLACK = 64 << 10  # output one round may give past the block's size before it is seen
_X86_WIDE = {13: 2, 14: 4, 15: 4, 16: 4, 17: 4, 18: 4}  # streams of 2- and 4-byte items


def x86_input_cap(block: int) -> int:
    """Largest encoded size of one x86 code block, for a method block size of `block`."""
    return block + block // 4 + 1024


# Operand kinds, from the format's tables. 0x20: a ModRM byte follows; its low two bits are
# the immediate after it (1: 8-bit, 2: 16-bit, 3: 32-bit, or 16-bit after a 0x66 prefix);
# 0x40: the real kind is in the group table, by the ModRM's reg field. 8-11 are the address
# kinds (A 8, AJ 9, R1 10, R4 11; no ModRM). -1: invalid. Flags of one-byte opcodes only:
# 0x80 marks a function's end, 0x100 an extra 16-bit item first, 0x200 the 0x66 prefix,
# 0x400 a two-byte opcode, 0x800 the call path (E8), 0x1000 a jump table (CE), 0x2000 an
# escaped byte (F1). x86_unfilter() uses these numbers directly (they are its hot path).
_X_KINDS = {"-": 0, "I1": 1, "I2": 2, "I4": 3, "M": 0x20, "M+I1": 0x21, "M+I4": 0x23,
            "G": 0x60, "A": 8, "AJ": 9, "R1": 10, "R4": 11, "E": -1}
_X_ONE = """
M    M    M    M    I1   I4   -    -    M    M    M    M    I1   I4   -    -
M    M    M    M    I1   I4   -    -    M    M    M    M    I1   I4   -    -
M    M    M    M    I1   I4   -    -    M    M    M    M    I1   I4   -    -
M    M    M    M    I1   I4   -    -    M    M    M    M    I1   I4   -    -
-    -    -    -    -    -    -    -    -    -    -    -    -    -    -    -
-    -    -    -    -    -    -    -    -    -    -    -    -    -    -    -
-    -    M    M    -    -    -    -    I4   M+I4 I1   M+I1 -    -    -    -
R1   R1   R1   R1   R1   R1   R1   R1   R1   R1   R1   R1   R1   R1   R1   R1
M+I1 M+I4 M+I1 M+I1 M    M    M    M    M    M    M    M    M    M    M    M
-    -    -    -    -    -    -    -    -    -    AJ   -    -    -    -    -
A    A    A    A    -    -    -    -    I1   I4   -    -    -    -    -    -
I1   I1   I1   I1   I1   I1   I1   I1   I4   I4   I4   I4   I4   I4   I4   I4
M+I1 M+I1 I2   -    M    M    M+I1 M+I4 I1   -    I2   -    -    I1   E    -
M    M    M    M    I1   I1   -    -    M    M    M    M    M    M    M    M
R1   R1   R1   R1   I1   I1   I1   I1   R4   R4   A    R1   -    -    -    -
-    E    -    -    -    -    G    G    -    -    -    -    -    -    G    G
"""
_X_TWO = """
E    E    E    E    E    E    -    E    -    -    E    E    E    E    E    E
M    M    M    M    M    M    M    M    M    E    E    E    E    E    E    E
M    M    M    M    E    E    E    E    M    M    M    M    M    M    M    M
-    -    -    -    -    -    E    -    E    E    E    E    E    E    E    E
M    M    M    M    M    M    M    M    M    M    M    M    M    M    M    M
M    M    M    M    M    M    M    M    M    M    M    M    M    M    M    M
M    M    M    M    M    M    M    M    M    M    M    M    M    M    M    M
M+I1 M+I1 M+I1 M+I1 M    M    M    -    E    E    E    E    E    E    M    M
R4   R4   R4   R4   R4   R4   R4   R4   R4   R4   R4   R4   R4   R4   R4   R4
M    M    M    M    M    M    M    M    M    M    M    M    M    M    M    M
-    -    -    M    M+I1 M    M    M    E    E    E    M    M+I1 M    E    M
M    M    M    M    M    M    M    M    E    E    E    M    M    M    M    M
M    M    M    M    M    M    M    M    -    -    -    -    -    -    -    -
M    M    M    M    M    M    M    M    M    M    M    M    M    M    M    M
M    M    M    M    M    M    M    M    M    M    M    M    M    M    M    M
M    M    M    M    M    M    M    M    M    M    M    M    M    M    M    E
"""
# F6, F7, FE and FF by the ModRM's reg field: reg + 8 * (opcode bit 0) + 16 * (opcode bit 3)
_X_GROUP = """
M+I1 E    M    M    M    M    M    M
M+I4 E    M    M    M    M    M    M
M    M    E    E    E    E    E    E
M    M    M    E    M    E    M    E
"""
X86_ONE = [_X_KINDS[w] for w in _X_ONE.split()]
X86_TWO = [_X_KINDS[w] for w in _X_TWO.split()]
X86_GROUP = [_X_KINDS[w] for w in _X_GROUP.split()]
for _c, _flag in ((0xC2, 0x80), (0xC3, 0x80), (0xCC, 0x80), (0x9A, 0x100), (0xEA, 0x100),
                  (0xC8, 0x100), (0xE8, 0x800)):
    X86_ONE[_c] |= _flag
X86_ONE[0x66], X86_ONE[0x0F], X86_ONE[0xCE], X86_ONE[0xF1] = 0x200, 0x400, 0x1000, 0x2000
del _c, _flag
# What a ModRM byte brings: 0 nothing, 1 a SIB (whose base field 5 adds a 32-bit
# displacement), 2 an 8-bit displacement, 3 a 32-bit displacement, 4 a 32-bit absolute
# address, 5 a SIB and an 8-bit displacement, 6 a SIB and a 32-bit displacement.
X86_MODRM = bytes(
    0 if m >> 6 == 3 else
    (1, 5, 6)[m >> 6] if m & 7 == 4 else
    (4 if m & 7 == 5 else 0) if m >> 6 == 0 else
    (2, 3)[(m >> 6) - 1]
    for m in range(256))


def _x86_little(data: bytearray, size: int) -> None:
    """Turn a stream of big-endian items of `size` (2 or 4) bytes little-endian, in place
    (at most half the stream is copied meanwhile)."""
    if len(data) % size:
        raise CodecError("damaged x86 data (a stream does not hold whole items)")
    for a in range(size // 2):
        b = size - 1 - a
        keep = data[a::size]
        data[a::size] = data[b::size]
        data[b::size] = keep
        del keep


def x86_sizes(head, total: int) -> list[int]:
    """The 19 stream sizes from a code block's header; they must fill its `total` bytes."""
    sizes = [int.from_bytes(head[i:i + 4], "little") for i in range(0, X86_HEAD, 4)]
    if total < X86_HEAD or X86_HEAD + sum(sizes) != total:
        raise CodecError("damaged x86 data (a code block's stream sizes do not add up)")
    return sizes


def x86_split(block) -> list[bytearray]:
    """The 19 streams of one bare code block (its size table, then the streams)."""
    if len(block) < X86_HEAD:
        raise CodecError("damaged x86 data (a code block is shorter than its header)")
    out, at = [], X86_HEAD
    for n in x86_sizes(block[:X86_HEAD], len(block)):
        out.append(bytearray(block[at:at + n]))
        at += n
    return out


def x86_unfilter(streams: list, dlen: int, base: int, stop=None) -> bytearray:
    """Rebuild one code block of `dlen` bytes, whose first byte is at address `base`, from its
    19 streams (bytearrays; the 2- and 4-byte streams are made little-endian in place).

    Raises CodecError on any inconsistency: a read past a stream's end, output past `dlen`
    or short of it, a stream not used up, or an opcode the encoder never writes. Output past
    `dlen` is seen at most X86_ROUND opcode bytes late (X86_SLACK covers it); stop() is called
    every X86_ROUND opcode bytes."""
    stop = stop or _no_stop
    for i, size in _X86_WIDE.items():
        _x86_little(streams[i], size)
    ops, sib, idx = streams[0], streams[1], streams[2]
    d8 = streams[3:11]
    j8, i8, i16, i32, disp, addr = streams[11:17]
    calls, jumps = _u32s(streams[17]), _u32s(streams[18])
    one, two, group, modrm = X86_ONE, X86_TWO, X86_GROUP, X86_MODRM
    ft = [0] * 256  # the function table: entry 0 is the latest target
    push, pop = ft.insert, ft.pop
    nextfunc = True
    out = bytearray()
    append = out.append
    m32 = _M32
    base &= m32
    op = p_sib = p_idx = p_j8 = p_i8 = p_i16 = p_i32 = p_disp = p_addr = p_call = p_jump = 0
    p_d8 = [0] * 8
    n_ops = len(ops)
    try:
        while op < n_ops:
            stop()
            lim = min(n_ops, op + X86_ROUND)
            while op < lim:
                c = ops[op]
                op += 1
                p = one[c]
                if nextfunc and c != 0xCC and c != 0xCE:
                    pop()
                    push(0, (base + len(out)) & m32)
                    nextfunc = False
                if not p:
                    append(c)
                    continue
                prefix = False
                if p >= 0x80:
                    if p == 0x1000:  # a jump table of 1 to 256 targets
                        for _ in range(ops[op] + 1):
                            k = idx[p_idx]
                            p_idx += 1
                            if k:
                                v = pop(k - 1)
                            else:
                                v = calls[p_call]
                                p_call += 1
                                pop()
                            push(0, v)
                            out += v.to_bytes(4, "little")
                        op += 1
                        if len(out) > dlen:
                            break
                        continue
                    if p == 0x2000:  # one byte kept as it is
                        append(ops[op])
                        op += 1
                        continue
                    append(c)
                    if p == 0x200:  # 0x66: the next byte is the opcode
                        prefix = True
                        c = ops[op]
                        op += 1
                        append(c)
                        p = one[c]
                        if p == 0x200:  # a second 0x66 is an opcode with no operands
                            continue
                        if p >= 0x1000:  # CE and F1 are not escapes here
                            p = -1
                    if p == 0x400:  # 0x0F: a two-byte opcode
                        c = ops[op]
                        op += 1
                        append(c)
                        p = two[c]
                    elif p >= 0x80:
                        if p & 0x80:
                            nextfunc = True
                        if p & 0x100:
                            out += i16[p_i16:p_i16 + 2]
                            p_i16 += 2
                        if p & 0x800:  # E8: a call, its target through the function table
                            k = idx[p_idx]
                            p_idx += 1
                            if k:
                                v = pop(k - 1)
                            else:
                                v = calls[p_call]
                                p_call += 1
                                pop()
                            push(0, v)
                            out += ((v - base - len(out) - 4) & m32).to_bytes(4, "little")
                            continue
                        p &= 0x7F
                    if not p:
                        continue
                else:
                    append(c)
                if p & 0x20:  # a ModRM byte
                    if p < 0:
                        raise CodecError("damaged x86 data (an opcode the format never "
                                         "writes)")
                    m = ops[op]
                    op += 1
                    append(m)
                    if p & 0x40:  # a group
                        p = group[(m >> 3 & 7) | (c & 1) << 3 | (c & 8) << 1]
                        if p < 0:
                            raise CodecError("damaged x86 data (an opcode the format never "
                                             "writes)")
                    f = modrm[m]
                    if f:
                        if f == 2:
                            r = m & 7
                            append(d8[r][p_d8[r]])
                            p_d8[r] += 1
                        elif f == 3:
                            out += disp[p_disp:p_disp + 4]
                            p_disp += 4
                        elif f == 4:
                            out += addr[p_addr:p_addr + 4]
                            p_addr += 4
                        else:
                            s = sib[p_sib]
                            p_sib += 1
                            append(s)
                            if f == 5:
                                append(d8[4][p_d8[4]])
                                p_d8[4] += 1
                            elif f == 6 or s & 7 == 5:
                                out += disp[p_disp:p_disp + 4]
                                p_disp += 4
                    p &= 3
                    if not p:
                        continue
                if p == 1:  # I1
                    append(i8[p_i8])
                    p_i8 += 1
                elif p == 3 and not prefix:  # I4
                    out += i32[p_i32:p_i32 + 4]
                    p_i32 += 4
                elif p == 10:  # R1
                    append(j8[p_j8])
                    p_j8 += 1
                elif p == 11:  # R4, a jump: its absolute target, made relative again
                    v = jumps[p_jump]
                    p_jump += 1
                    out += ((v - base - len(out) - 4) & m32).to_bytes(4, "little")
                elif p < 8:
                    if p < 0:
                        raise CodecError("damaged x86 data (an opcode the format never "
                                         "writes)")
                    out += i16[p_i16:p_i16 + 2]  # I2, or I4 after 0x66
                    p_i16 += 2
                elif p == 8:  # A
                    out += addr[p_addr:p_addr + 4]
                    p_addr += 4
                else:  # AJ: kept as it is
                    out += jumps[p_jump].to_bytes(4, "little")
                    p_jump += 1
            if len(out) > dlen:
                raise CodecError("damaged x86 data (a code block gives more than its size)")
    except IndexError:
        raise CodecError("damaged x86 data (a stream ends early)") from None
    used = [op, p_sib, p_idx, *p_d8, p_j8, p_i8, p_i16, p_i32, p_disp, p_addr, 4 * p_call,
            4 * p_jump]
    if used != [len(s) for s in streams]:
        raise CodecError("damaged x86 data (a code block's streams do not match its "
                         "instructions)")
    if len(out) != dlen:
        raise CodecError("damaged x86 data (a code block gives less than its size)")
    return out


class X86Stage(_Stage):
    """The x86 code filter (FreeArc's dispack070), decode direction.

    The stream: a chunk size, then frames: a code block (tag, decoded size, encoded size, the
    19-dword size table and the streams), a data frame (tag, length, the bytes) or a raw chunk
    (chunk-size bytes whose first dword is not a tag). A code block is held whole (encoded and
    decoded); data and raw frames are passed on a piece at a time. Refused: a chunk size
    below 4 or over the method's block size, a tag in the reserved range, a code block over
    the method's block size or `block_cap` (the stage's `limit` when smaller) or encoded in
    more than x86_input_cap(block size) bytes, a data frame that is empty or longer than the
    chunk size (FreeArc's encoder never writes one), and a cut inside any frame. A cut exactly
    between two frames cannot be seen here; the files' sizes and CRCs see it. With a `limit`,
    no frame is read once that much was output.
    """

    def __init__(self, src, block_size: int, block_cap: int, chunk: int,
                 limit: int | None = None, stop=None):
        super().__init__(stop)
        self.inp = _Input(src, chunk)
        self.bsize, self.cap, self.chunk, self.limit = block_size, block_cap, chunk, limit
        self.icap = x86_input_cap(block_size)
        self.chunksize = 0  # 0 until read
        self.base = X86_BASE
        self.total = 0  # output so far
        self.copy = 0  # bytes of the current data or raw frame still to pass on
        self.frame = 0  # the current data or raw frame's length

    def _word(self, what: str, end_ok: bool = False) -> int | None:
        data = self.inp.exact(4)
        if not data and end_ok:
            return None
        if len(data) != 4:
            raise CodecError(f"the x86 data ended inside {what}")
        return int.from_bytes(data, "little")

    def _advance(self, n: int) -> None:
        self.total += n
        self.base += n
        if self.base >= X86_WRAP:
            self.base -= 0x80000000

    def _next(self) -> bytes:
        if self.copy:
            piece = self.inp.some(min(self.chunk, self.copy))
            if not piece:
                raise CodecError("the x86 data ended inside a frame")
            self.copy -= len(piece)
            if not self.copy:
                self._advance(self.frame)
            return piece
        if self.limit is not None and self.total >= self.limit:
            self.done = True
            return b""
        if not self.chunksize:
            size = self._word("its header", end_ok=True)
            if size is None:
                self.done = True
                return b""
            if not 4 <= size <= self.bsize:
                raise CodecError("bad x86 header (a chunk size its method does not allow)")
            self.chunksize = size
        tag = self._word("a frame header", end_ok=True)
        if tag is None:
            self.done = True
            return b""
        if tag == X86_EXE:
            dlen = self._word("a frame header")
            elen = self._word("a frame header")
            if dlen > self.bsize or elen > self.icap or elen < X86_HEAD:
                raise CodecError("bad x86 code block (sizes its method does not allow)")
            if dlen > self.cap:
                raise CodecError("an x86 code block runs past the files' data further than "
                                 "gitfurl decodes")
            sizes = x86_sizes(self._need(X86_HEAD), elen)
            streams = [self._need(n) for n in sizes]
            out = x86_unfilter(streams, dlen, self.base, self.stop)
            del streams
            self._advance(dlen)
            return out
        if X86_TAGS <= tag < X86_TAGS + 16 and tag != X86_DATA:
            raise CodecError("bad x86 frame (a reserved tag)")
        if tag == X86_DATA:
            n = self._word("a frame header")
            if not 0 < n <= self.chunksize:
                raise CodecError("bad x86 data frame (a length FreeArc never writes)")
            self.copy = self.frame = n
            return b""
        # a raw chunk: the tag was its first four bytes
        self.frame, self.copy = self.chunksize, self.chunksize - 4
        if not self.copy:
            self._advance(self.frame)
        return tag.to_bytes(4, "little")

    def _need(self, n: int) -> bytearray:
        data = self.inp.exact(n)
        if len(data) != n:
            raise CodecError("the x86 data ended inside a code block")
        return data


def cm_available() -> bool:
    """True when the native module the cm stage needs can be imported."""
    return _codecs is not None


class CmStage(_Stage):
    """The cm stage, decoded by gitfurl's native module (gitfurl._codecs). It reads the
    block's stored bytes itself, so it is always the chain's last method: an 11-byte header,
    then independent jobs, each two streams found by their lengths. Up to `threads` jobs are
    decoded at once, on as many threads; each gives at most 512 MiB, held until it is passed
    on. The decoder reads a job's data a chunk at a time through _read, and calls stop()
    between chunks and every few segments. Every job but the last must give the header's job
    size, and with a `limit` more output than that is refused."""

    def __init__(self, fd: int, offset: int, size: int, threads: int, chunk: int,
                 limit: int | None = None, stop=None):
        super().__init__(stop)
        if _codecs is None:
            raise StartError(CM_MISSING)
        self.fd, self.offset, self.size = fd, offset, size
        self.threads, self.chunk, self.left = max(1, threads), chunk, limit
        self.head = self._read(0, CM_HEADER)
        try:
            self.job_size, _ = _codecs.cm_header(self.head)
        except _codecs.CmError as exc:
            raise CodecError(f"cm stage: {exc}") from None
        self.at, self.ended, self.outs = CM_HEADER, False, []

    def _read(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0 or off + n > self.size:
            raise CodecError("cm stage: damaged data (a read past the end of the block)")
        try:
            data = os.pread(self.fd, n, self.offset + off)
        except OSError as exc:
            raise CodecError(f"the archive could not be read ({exc.strerror or exc})") from exc
        if len(data) != n:
            raise CodecError("the archive could not be read completely (did it change?)")
        return data

    def _u32(self, off: int) -> int:
        return int.from_bytes(self._read(off, 4), "little")

    def _at_end(self) -> bool:
        """True (and the stream checked to end there) when the next job length is 0."""
        if self._u32(self.at):
            return False
        if self.at + 4 != self.size:
            raise CodecError("cm stage: damaged data (data after the end of the stream)")
        self.ended = True
        return True

    def _batch(self) -> None:
        jobs = []
        while len(jobs) < self.threads and not self._at_end():
            a_len = self._u32(self.at)
            a_off = self.at + 4
            b_len = self._u32(a_off + a_len) if a_len <= self.size - a_off - 4 else -1
            b_off = a_off + a_len + 4
            if b_len < 0 or b_len > self.size - b_off:
                raise CodecError("cm stage: damaged data (a job longer than the block)")
            jobs.append((a_off, a_len, b_off, b_len))
            self.at = b_off + b_len
        if not jobs:
            return
        if not self.ended:
            self._at_end()  # whether the last of these is the stream's last job
        try:
            outs = _codecs.cm_decode_jobs(self.head, jobs, self._read, self.threads, self.stop)
        except _codecs.CmError as exc:
            raise CodecError(f"cm stage: {exc}") from None
        except MemoryError:
            raise CodecError("cm stage: not enough memory for a job") from None
        for i, out in enumerate(outs):
            last = self.ended and i == len(outs) - 1
            if not len(out) or (len(out) < self.job_size and not last):
                raise CodecError("cm stage: damaged data (a job shorter than the others)")
            if self.left is not None:
                if len(out) > self.left:
                    raise CodecError("cm stage: more output than the block's files hold")
                self.left -= len(out)
        self.outs = outs

    def _next(self) -> bytes:
        while True:
            while self.outs:
                piece = self.outs[0].take(self.chunk)
                if piece:
                    return piece
                self.outs.pop(0)
            if self.ended:
                self.done = True
                return b""
            self._batch()


class RolzStage(_Stage):
    """The rolz method (see farc_rolz.py). It reads the block's stored bytes at random (each
    record's index is at its end), so it is always the chain's last method and reads the
    archive itself. `window` is the largest window accepted (the plan's); with a `limit`, more
    output than that is refused."""

    def __init__(self, fd: int, offset: int, size: int, window: int, chunk: int,
                 limit: int | None = None, stop=None):
        super().__init__(stop)
        self.fd, self.offset, self.size = fd, offset, size
        self.gen = farc_rolz.decode_block(self._read, size, window, stop, chunk, limit)

    def _read(self, off: int, n: int) -> bytes:
        if off < 0 or n < 0 or off + n > self.size:
            raise farc_rolz.RolzError("damaged data (a read past the end of the block)")
        try:
            data = os.pread(self.fd, n, self.offset + off)
        except OSError as exc:
            raise CodecError(f"the archive could not be read ({exc.strerror or exc})") from exc
        if len(data) != n:
            raise CodecError("the archive could not be read completely (did it change?)")
        return data

    def _next(self) -> bytes:
        try:
            return next(self.gen)
        except StopIteration:
            self.done = True
            return b""
        except farc_rolz.RolzError as exc:
            self.done = True
            raise CodecError(f"rolz stage: {exc}") from None


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
        elif name == "4x4":
            reader = X4Stage(reader, p.stage.lzma, p.budget, chunk, p.limit, stop)
        elif name == "x86":
            reader = X86Stage(reader, p.stage.size, p.budget, chunk, p.limit, stop)
        elif name == "srep":
            reader = SrepStage(reader, p.stage.size, p.budget, chunk, p.limit, stop)
        elif name == "cm":
            if not isinstance(reader, Source):
                raise StartError("the cm stage can only read the stored data (the last method)")
            reader = CmStage(fd, offset, compsize, p.budget, chunk, p.limit, stop)
        elif name == "rolz":
            if not isinstance(reader, Source):
                raise StartError("rolz can only read the stored data (the last method)")
            reader = RolzStage(fd, offset, compsize, p.budget, chunk, p.limit, stop)
        # storing: output equals input, nothing to add
    return reader
