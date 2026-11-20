"""FreeArc method chains: the exe, delta and rep decoders, lzma inside a chain, the pipeline and
extraction of chained archives. Every stream is built in memory by the test-only encoders in
tests/farc_writer.py, which follow the same clean-room specifications as the decoders, plus
the specifications' own worked examples and test vectors."""

import os
import random
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

try:
    from farc_writer import (
        EXE_ROUND,
        F,
        delta_block,
        delta_encode,
        exe_encode,
        make_archive,
        pack,
        rep_encode,
        rep_stream,
        u32,
    )
except ImportError:  # python -m unittest tests.test_codecs
    from tests.farc_writer import (
        EXE_ROUND,
        F,
        delta_block,
        delta_encode,
        exe_encode,
        make_archive,
        pack,
        rep_encode,
        rep_stream,
        u32,
    )

from gitfurl import extract, farc
from gitfurl import farc_codecs as fc
from gitfurl.farc_codecs import CodecError

RND = random.Random(20261119)


def x86ish(n: int, seed: int = 1) -> bytes:
    """Random bytes rich in E8/E9 opcodes and 00/FF operand ends."""
    r = random.Random(seed)
    pool = [0xE8, 0xE9, 0x00, 0xFF, 0x90] + list(range(256))
    return bytes(r.choice(pool) for _ in range(n))


class Mem:
    """An in-memory stage input that hands out at most `step` bytes per read."""

    def __init__(self, data: bytes, step: int = 1 << 20):
        self.data, self.at, self.step = data, 0, step

    def read(self, n: int) -> bytes:
        k = min(n, self.step)
        out = self.data[self.at:self.at + k]
        self.at += len(out)
        return out


def drain(stage, n: int = 1 << 20, limit: int = 1 << 28) -> bytes:
    out = bytearray()
    while True:
        piece = stage.read(n)
        if not piece:
            return bytes(out)
        assert len(piece) <= n
        out += piece
        assert len(out) <= limit, "unbounded output"


def exe(data, chunk=1 << 20, step=1 << 20, n=1 << 20):
    return drain(fc.ExeStage(Mem(data, step), chunk), n)


def delta(data, cap=8 << 20, chunk=1 << 20, step=1 << 20, n=1 << 20):
    return drain(fc.DeltaStage(Mem(data, step), cap, chunk), n)


def rep(data, cap=1 << 30, chunk=1 << 20, step=1 << 20, n=1 << 20):
    return drain(fc.RepStage(Mem(data, step), cap, chunk), n)


# --- exe ------------------------------------------------------------------------------------

class Exe(unittest.TestCase):
    VECTORS = (  # chain-and-exe spec B.9, decode direction
        ("e801000000", "e801000000"),
        ("90e807000000", "90e801000000"),
        ("e8fbffffff909090", "e8f6ffffff909090"),
        ("e9100000ff90909090", "e90b0000ff90909090"),
        ("e8e800000000909090909090", "e8e300000000909090909090"),
        ("0fe8e8e8000000ff00ff00909090", "0fe8e2e8000000ff00ff00909090"),
        ("e800000000e8000000ff9090909090", "e8fbffffffe8f6ffff009090909090"),
    )

    def test_spec_vectors(self):
        for enc, dec in self.VECTORS:
            with self.subTest(enc=enc):
                self.assertEqual(exe(bytes.fromhex(enc)).hex(), dec)
                self.assertEqual(exe_encode(bytes.fromhex(dec)).hex(), enc)

    def test_short_streams_pass_unchanged(self):
        for n in range(6):
            data = b"\xe8\x00\x00\x00\x00"[:n]
            self.assertEqual(exe(data), data)

    def test_final_round_of_five_bytes_is_not_converted(self):
        data = bytearray(b"\x90" * (EXE_ROUND + 1))
        data[EXE_ROUND - 4], data[EXE_ROUND - 3], data[EXE_ROUND] = 0xE8, 0x01, 0x00
        enc = exe_encode(bytes(data))
        self.assertEqual(enc[-5:].hex(), "e801909000")  # spec B.9 boundary vector
        self.assertEqual(exe(enc), bytes(data))

    def test_round_trips_at_round_and_chunk_boundaries(self):
        for size in (6, 9, 10, 100, EXE_ROUND - 1, EXE_ROUND, EXE_ROUND + 1, EXE_ROUND + 4,
                     EXE_ROUND + 5, EXE_ROUND + 6, 2 * EXE_ROUND + 3, 600_000):
            data = x86ish(size, size)
            enc = exe_encode(data)
            for chunk, step, n in ((1 << 20, 1 << 20, 1 << 20), (7, 3, 5), (4096, 1, 65536)):
                if size > 300_000 and chunk < 4096:
                    continue  # byte-sized reads over large streams only make the test slow
                with self.subTest(size=size, chunk=chunk, step=step):
                    self.assertEqual(exe(enc, chunk, step, n), data)

    def test_opcode_straddling_a_round(self):
        for at in range(EXE_ROUND - 6, EXE_ROUND + 1):
            data = bytearray(b"\x90" * (EXE_ROUND + 40))
            data[at:at + 5] = b"\xe8\x10\x20\x00\x00"
            enc = exe_encode(bytes(data))
            self.assertEqual(exe(enc, step=4097), bytes(data), at)

    def test_random_streams_round_trip(self):
        for k in range(300):
            data = x86ish(RND.randrange(0, 60), k)
            self.assertEqual(exe(exe_encode(data)), data, data.hex())

    def test_output_length_equals_input_length(self):
        data = os.urandom(EXE_ROUND + 777)
        self.assertEqual(len(exe(data)), len(data))


# --- delta ----------------------------------------------------------------------------------

class Delta(unittest.TestCase):
    def test_spec_worked_example(self):
        enc = bytes.fromhex("0c000000 04000000 02000000 0c000000 03000000"
                            "aabb7f7f7e1020f500150fcc".replace(" ", ""))
        self.assertEqual(delta(enc).hex(), "aabb10207f05217f1a307ecc")
        self.assertEqual(delta_block(bytes.fromhex("aabb10207f05217f1a307ecc"),
                                     [(2, 12, 3)]), enc)

    def test_empty_and_untouched_blocks(self):
        self.assertEqual(delta(b""), b"")
        self.assertEqual(delta(u32(5) + u32(0) + b"abcde"), b"abcde")
        self.assertEqual(delta(u32(0) + u32(0)), b"")

    def test_every_width_and_kept_pattern(self):
        r = random.Random(5)
        for width in range(1, 31):
            for shape in {1 << width, (1 << width) | 1, (1 << width) | (1 << (width - 1)),
                          (2 << width) - 1, (1 << width) | r.getrandbits(width)}:
                for rows in (0, 1, 2, 7):
                    data = bytes(r.getrandbits(8) for _ in range(3 + width * rows + 2))
                    enc = delta_block(data, [(3, shape, rows)])
                    with self.subTest(width=width, shape=bin(shape), rows=rows):
                        self.assertEqual(delta(enc), data)

    def test_growing_tables_several_blocks_and_chunks(self):
        t16 = b"".join((i * 3 % 65536).to_bytes(2, "little") for i in range(5000))
        t32 = b"".join((i * 70001 % (1 << 32)).to_bytes(4, "little") for i in range(5000))
        data = os.urandom(333) + t16 + os.urandom(5) + t32 + os.urandom(100)

        def find(chunk):
            return [(0, 16, len(chunk) // 4)] if len(chunk) >= 8 else []
        enc = delta_encode(data, block=4099, find=find)
        self.assertNotEqual(enc[8:], data)
        for chunk, step, n in ((1 << 20, 1 << 20, 1 << 20), (5, 3, 7)):
            self.assertEqual(delta(enc, chunk=chunk, step=step, n=n), data)

    def test_adjacent_tables_and_wide_runs(self):
        data = os.urandom(200)
        tables = [(0, 1 << 5, 4), (0, (1 << 7) | 0b0001000, 3), (10, 1 << 8, 5)]
        self.assertEqual(delta(delta_block(data, tables)), data)
        # runs of 12 and of 8 differenced columns (wider than the 4-byte fast path)
        tables = [(1, 1 << 12, 4), (0, (1 << 9) | 1, 3), (0, 1 << 30, 2)]
        data = os.urandom(1 + 48 + 27 + 60 + 5)
        self.assertEqual(delta(delta_block(data, tables)), data)

    def test_damaged_streams(self):
        good = delta_block(b"x" * 40, [(0, 16, 10)])
        bad = {
            "truncated header": [good[:1], good[:3], good[:6]],
            "truncated tables or data": [good[:9], good[:20], good[:-1]],
            "bad sizes": [u32(1 << 31) + u32(0), u32(4) + u32(3) + b"abcd",
                          u32(4) + u32(1 << 31) + b"abcd"],
            "bad shape": [delta_block(b"x" * 8, [(0, 1, 2)]), delta_block(b"x" * 8, [(0, 0, 2)]),
                          u32(8) + u32(4) + u32(0) + u32(1 << 31) + u32(0) + b"x" * 8],
            "outside": [u32(8) + u32(4) + u32(0) + u32(16) + u32(3) + b"x" * 8,
                        delta_block(b"x" * 8, [(9, 2, 0)]),
                        u32(8) + u32(4) + u32(1 << 31) + u32(2) + u32(0) + b"x" * 8,
                        u32(8) + u32(4) + u32(0) + u32(2) + u32(1 << 31) + b"x" * 8],
            "too many tables": [u32(4) + u32(12) + b"\0" * 36 + b"abcd"],
        }
        for what, streams in bad.items():
            for s in streams:
                with self.subTest(what=what, s=s[:24].hex()), self.assertRaises(CodecError):
                    delta(s)
        self.assertEqual(delta(good + good), b"x" * 80)
        with self.assertRaises(CodecError):
            delta(good + good[:2])

    def test_long_tables_are_restored_a_run_of_rows_at_a_time(self):
        data = b"".join((i * 40503 % (1 << 32)).to_bytes(4, "little") for i in range(70_000))
        data += os.urandom(3)
        tables = [(0, 1 << 4, 70_000)]  # more rows than DELTA_ROWS
        enc = delta_block(data, tables)
        self.assertEqual(delta(enc, cap=1 << 20), data)
        small = os.urandom(3 * 50 + 1)
        for shape in (1 << 3, (1 << 3) | 2, 1 << 1):
            rows = 150 // (shape.bit_length() - 1)
            enc = delta_block(small, [(1, shape, rows)])
            with mock.patch.object(fc, "DELTA_ROWS", 7):
                self.assertEqual(delta(enc), small, bin(shape))

    def test_block_over_the_cap(self):
        enc = delta_block(b"y" * 100)
        self.assertEqual(delta(enc, cap=100), b"y" * 100)
        with self.assertRaises(CodecError):
            delta(enc, cap=99)

    def test_empty_blocks_in_a_row_are_capped_and_stop_is_seen(self):
        empty = u32(0) + u32(0)
        tail = delta_block(b"ok")
        self.assertEqual(delta(empty * fc.DELTA_MAX_EMPTY + tail), b"ok")
        self.assertEqual(delta((empty * fc.DELTA_MAX_EMPTY + tail) * 3), b"ok" * 3)
        with self.assertRaisesRegex(CodecError, "empty delta blocks"):
            delta(empty * (fc.DELTA_MAX_EMPTY + 1) + tail)
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 5:
                raise Stopped
        with self.assertRaises(Stopped):
            drain(fc.DeltaStage(Mem(empty * 50 + tail), 1 << 20, 1 << 20, stop=stop))

    def test_limit_ends_the_output_after_that_block(self):
        enc = delta_block(b"a" * 10) + delta_block(b"b" * 10) + b"\xff" * 3  # damaged tail
        self.assertEqual(drain(fc.DeltaStage(Mem(enc), 100, 1 << 20, limit=10)), b"a" * 10)
        self.assertEqual(drain(fc.DeltaStage(Mem(enc), 100, 1 << 20, limit=11)),
                         b"a" * 10 + b"b" * 10)
        with self.assertRaises(CodecError):
            delta(enc)

    def test_memory_stays_within_the_plan(self):
        b, chunk = 1 << 21, 1 << 16

        def block(count):  # one 2-wide table with a kept column, then empty tables
            shapes = [0b110] + [2] * (count - 1)
            rows = [b // 2] + [0] * (count - 1)
            return (u32(b) + u32(4 * count) + b"".join(map(u32, [0] * count))
                    + b"".join(map(u32, shapes)) + b"".join(map(u32, rows)) + bytes(b))
        enc = block(1) + block(b // 4 + 1)
        m = farc.parse_method("delta:2mb")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blk"
            path.write_bytes(enc)
            fd = os.open(path, os.O_RDONLY)
            try:
                top = fc.open_chain(m, fd, 0, len(enc), 2 * b, chunk, lambda: None)
                tracemalloc.start()
                try:
                    got = 0
                    while got < 2 * b:
                        piece = top.read(chunk)
                        self.assertTrue(piece)
                        got += len(piece)
                        del piece
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            finally:
                os.close(fd)
        self.assertLessEqual(peak, fc.chain_memory(m, 2 * b, chunk))


# --- rep ------------------------------------------------------------------------------------

class Stopped(Exception):
    pass


class Rep(unittest.TestCase):
    def test_empty_input_stream(self):
        enc = rep_encode(b"", window=1 << 20)
        self.assertEqual(len(enc), 20)
        self.assertEqual(rep(enc), b"")

    def test_overlapping_and_repeating_matches(self):
        # "ab", then 7 bytes from distance 2, then "c", then 5 bytes from distance 1
        enc = rep_stream(64, [([b"ab", b"c", b""], [(7, 2), (5, 1)])])
        self.assertEqual(rep(enc), b"ab" + b"abababa" + b"c" + b"ccccc")
        self.assertEqual(rep(enc, chunk=3, step=1, n=2), b"ababababac" + b"ccccc")

    def test_ring_wrap_full_window_distance_and_long_matches(self):
        w = 16
        data = bytes(range(16))
        blocks = [([data, b"", b"Z"], [(16, 16), (40, 16)]), ([b"", b""], [(3, 16)])]
        want = data + data + (data * 3)[:40] + b"Z"
        want += want[-16:][:3]
        enc = rep_stream(w, blocks)
        for chunk, n in ((1 << 20, 1 << 20), (5, 3), (16, 16), (1, 1)):
            self.assertEqual(rep(enc, chunk=chunk, n=n), want, (chunk, n))

    def test_zero_length_match_ignores_its_distance(self):
        enc = rep_stream(8, [([b"abc", b"d"], [(0, 999)])])
        self.assertEqual(rep(enc), b"abcd")

    def test_matches_across_blocks_and_round_trips(self):
        base = os.urandom(3000)
        data = base + base[:1000] + os.urandom(500) + base * 3 + b"\0" * 5000
        for window in (1 << 20, 4096, 2048):
            enc = rep_encode(data, window=window, block=1000, min_len=4)
            self.assertLess(len(enc), len(data))
            for chunk, step, n in ((1 << 20, 1 << 20, 1 << 20), (7, 5, 3), (4096, 1, 1000)):
                with self.subTest(window=window, chunk=chunk):
                    self.assertEqual(rep(enc, chunk=chunk, step=step, n=n), data)

    def test_window_kept_is_capped(self):
        data = os.urandom(5000) + os.urandom(100) * 50
        enc = rep_encode(data, window=1 << 20, min_len=8)
        stage = fc.RepStage(Mem(enc), 1024, 1 << 20)
        self.assertEqual(drain(stage), data)  # only short distances are used
        self.assertLessEqual(len(stage.ring), 1024)
        far = rep_stream(1 << 20, [([os.urandom(3000), b""], [(10, 2000)])])
        with self.assertRaisesRegex(CodecError, "further back"):
            rep(far, cap=1024)
        self.assertEqual(len(rep(far, cap=4096)), 3010)

    def test_a_large_declared_window_is_not_allocated(self):
        data = os.urandom(1000) * 3
        enc = rep_encode(data, window=1 << 30, min_len=8)
        self.assertEqual(int.from_bytes(enc[:4], "little"), 1 << 30)
        stage = fc.RepStage(Mem(enc), 1 << 30, 1 << 20)
        self.assertEqual(drain(stage), data)
        self.assertEqual(len(stage.ring), len(data))  # grown as output was made

    def test_damaged_streams(self):
        ok = rep_stream(64, [([b"abcd", b""], [(4, 4)])])
        n = 1 << 31
        bad = {
            "truncated": [b"", ok[:3], ok[:4], ok[:6], ok[:12], ok[:-5], ok[:-4], ok[:-1]],
            "window": [u32(0) + u32(0), u32(farc.DICT_CAP + 1) + u32(0)],
            "small block": [u32(64) + u32(4) + b"\0" * 4 + u32(0)],
            "too many matches": [u32(64) + u32(8) + u32(1) + b"\0" * 4 + u32(0),
                                 u32(64) + u32(8) + u32(n) + b"\0" * 4 + u32(0)],
            "values over 2^31": [rep_stream(64, [([b"ab", b""], [(n, 1)])]),
                                 rep_stream(64, [([b"ab", b""], [(1, n)])])],
            "sizes do not agree": [ok[:4] + u32(int.from_bytes(ok[4:8], "little") + 1)
                                   + ok[8:-4] + b"\0" + u32(0),
                                   ok[:4] + u32(int.from_bytes(ok[4:8], "little") - 1)
                                   + ok[8:-5] + u32(0)],
            "before the start": [rep_stream(64, [([b"abcd", b""], [(4, 5)])]),
                                 rep_stream(64, [([b"", b""], [(1, 1)])]),
                                 rep_stream(64, [([b"abcd", b""], [(4, 0)])]),
                                 rep_stream(2, [([b"abcd", b""], [(1, 3)])])],
            "after the end": [ok + b"\0"],
        }
        self.assertEqual(rep(ok), b"abcdabcd")
        for what, streams in bad.items():
            for s in streams:
                with self.subTest(what=what, s=s[:24].hex()), self.assertRaises(CodecError):
                    rep(s)

    def test_input_that_gives_no_output_is_capped_and_stop_is_seen(self):
        empty = u32(8) + u32(0) + u32(0)
        zeros = u32(12 * 1000 + 8) + u32(1000) + b"\0" * (12 * 1000 + 4)  # 1000 L=0 matches
        end = rep_stream(16, [([b"A"], [])])[4:]
        for body in (empty * 5000, zeros * 20):
            stream = u32(16) + body + end
            self.assertEqual(rep(stream), b"A")
            with mock.patch.object(fc, "REP_FREE_OVERHEAD", 1 << 14), \
                    self.assertRaisesRegex(CodecError, "too little output"):
                rep(stream)
        # output counts: 16 bytes of headers and tables are allowed per byte of output
        two = u32(10) + u32(0) + u32(2) + b"xy"  # 24 bytes of overhead per 2 bytes: allowed
        one = u32(9) + u32(0) + u32(1) + b"x"  # 24 per byte: refused
        with mock.patch.object(fc, "REP_FREE_OVERHEAD", 1 << 10):
            self.assertEqual(rep(u32(16) + (two + empty) * 2000 + u32(0)), b"xy" * 2000)
            with self.assertRaisesRegex(CodecError, "too little output"):
                rep(u32(16) + (one + empty) * 2000 + u32(0))
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 5:
                raise Stopped
        for body in (empty * 50, zeros * 20):
            calls.clear()
            with self.subTest(len(body)), self.assertRaises(Stopped):
                drain(fc.RepStage(Mem(u32(16) + body + end), 1 << 20, 1 << 20, stop=stop))

    def test_limit_ends_the_output(self):
        data = os.urandom(1000) * 4
        enc = rep_encode(data, window=1 << 20, min_len=8) + b"junk"  # damaged after the end
        for limit in (0, 1, 999, 1500, 3999):
            stage = fc.RepStage(Mem(enc), limit or 1, 1 << 20, limit=limit)
            self.assertEqual(drain(stage), data[:limit], limit)
        with self.assertRaisesRegex(CodecError, "after the end"):
            rep(enc)

    def test_match_count_cap(self):
        many = fc.REP_MIN_MATCHES + 1
        s = rep_stream(64, [([b"a"] + [b""] * many, [(1, 1)] * many)])
        with self.assertRaisesRegex(CodecError, "more matches"):
            rep(s, cap=64)
        self.assertEqual(len(rep(s, cap=8 * many)), many + 1)


# --- lzma, methods and planning -------------------------------------------------------------

class LzmaAndPlan(unittest.TestCase):
    def lz(self, data, step=1 << 20, chunk=1 << 20):
        opts = {"lc": 3, "lp": 0, "pb": 2}
        return drain(fc.LzmaStage(Mem(data, step), opts, 1 << 20, chunk))

    def test_lzma_stage(self):
        data = os.urandom(5000) * 20
        enc = pack("lzma:1mb", data)
        self.assertEqual(self.lz(enc), data)
        self.assertEqual(self.lz(enc, step=7, chunk=13), data)
        with self.assertRaisesRegex(CodecError, "end marker"):
            self.lz(enc[:-10])
        bad = bytearray(enc)
        for i in range(20, len(bad) // 2):
            bad[i] ^= 0x5A
        with self.assertRaises(CodecError):
            self.lz(bytes(bad))

    def test_parse_chains(self):
        ok = {
            "rep:15mb+exe+delta+lzma:8mb:normal:32": "rep+exe+delta+lzma",
            "exe+storing": "exe+storing", "storing+exe": "storing+exe",
            "delta+lzma:8mb:normal:32": "delta+lzma", "rep:1mb+lzma": "rep+lzma",
            "rep:15mb:32+lzma:8mb": "rep+lzma", "rep:1gb": "rep", "delta:b1gb": "delta",
            "rep:max:l32:c0:d1mb:s64:h20:a2:50%:=96mb:256": "rep",
            "rep:b24": "rep", "rep:=24": "rep", "delta:x:16mb": "delta", "delta:20": "delta",
            "delta:0b": "delta", "delta:=5k": "delta", "exe": "exe",
            "+".join(["exe"] * farc.MAX_STAGES): "+".join(["exe"] * farc.MAX_STAGES),
        }
        for text, kind in ok.items():
            with self.subTest(text=text):
                self.assertEqual(farc.parse_method(text).kind, kind)
        sizes = {"rep:b24": 1 << 24, "rep:1gb": 1 << 30, "delta:20": 1 << 20,
                 "delta:x:16mb": 16 << 20, "delta": 8 << 20, "rep": 64 << 20,
                 "rep:max:l32:c0:d1mb:s64:h20:a2:50%:=96mb:256": 96 << 20}
        for text, size in sizes.items():
            self.assertEqual(farc.parse_method(text).stages[0].size, size, text)
        bad = ["exe:1", "exe:*x", "EXE", "zzexe", "exe9", "exe ", " exe", "rep:b",
               "rep:l", "rep:1025mb", "rep:4gb", "rep:0b", "rep:16Mb", "rep:a50%", "rep:zz",
               "delta:2g", "delta:b", "delta:16Mb", "delta:", "delta:*x", "rep+", "+exe",
               "exe++lzma", "", "+".join(["exe"] * (farc.MAX_STAGES + 1)),
               "rep:" + "1" * 2050, "lzma:" + ":".join(["fast"] * 200),
               "zz9:lzma", "rep+exe+delta+zz9:lzma:14mb", "qqq+lzma", "storing:x",
               "rep:1mb+exe+aes-256/ctr:n1000", "delta:==20", "delta:b==8mb", "rep:b==24",
               "rep:==1mb", "rep:d==1mb", "delta:=", "rep:b=", "delta:=mb"]
        for text in bad:
            with self.subTest(text=text):
                m = farc.parse_method(text)
                self.assertFalse(m.supported)
                self.assertEqual(m.kind, farc.KIND_OTHER)
                self.assertNotIn(text or "\0", m.reason + repr(m))

    def test_summary_names_supported_chains_only(self):
        arc = farc.Archive(Path("x"), 0, 0, False)
        for text in ("rep:15mb+exe+delta+lzma:8mb", "storing", "zzzmethod:7+lzma",
                     "exe+lzma", "exe+lzma:1mb", "lzma"):
            arc.blocks.append(farc.DataBlock(farc.parse_method(text), 0, 0, 0))
        line = farc.summarise(arc)[1]
        self.assertEqual(line, "6 data blocks: 1 storing, 1 lzma, 1 rep+exe+delta+lzma, "
                               f"2 exe+lzma, 1 {farc.KIND_OTHER}")
        self.assertNotIn("zzz", line)
        for k in range(8):
            arc.blocks.append(farc.DataBlock(farc.parse_method("exe+" * (k % 7) + "rep"),
                                             0, 0, 0))
        self.assertIn("with other chains of these methods", farc.summarise(arc)[1])

    def test_memory_plan(self):
        m = farc.parse_method("rep:15mb+exe+delta+lzma:8mb:normal:32")
        plans = fc.stage_plan(m, 1 << 30)
        self.assertEqual([p.budget for p in plans], [15 << 20, fc.EXE_ROUND, 8 << 20, 8 << 20])
        small = fc.stage_plan(m, 1000)  # rep and exe keep sizes up to the files' 1000 bytes
        self.assertEqual(small[0].budget, 1000)
        self.assertEqual(small[2].budget, 8 << 20)  # delta's output is not the file data
        total = fc.chain_memory(m, 1 << 30, 1 << 20)
        self.assertEqual(total, sum(p.memory for p in plans) + 5 * (1 << 20))
        self.assertEqual(fc.chain_memory(farc.parse_method("storing"), 100, 1 << 20), 0)
        lz = fc.stage_plan(farc.parse_method("exe+lzma:1gb"), 5000)
        self.assertEqual(lz[1].budget, 5000 + fc.EXE_SLACK)  # exe may read a little further
        self.assertEqual([p.limit for p in lz], [5000, 5000 + fc.EXE_SLACK])
        lz = fc.stage_plan(farc.parse_method("exe+exe+rep:1gb+exe+lzma:1gb"), 5000)
        self.assertEqual([p.limit for p in lz], [5000, 5064, 5128, None, None])
        self.assertEqual([p.budget for p in lz][2:], [5128, fc.EXE_ROUND, 1 << 30])
        lz = fc.stage_plan(farc.parse_method("delta+lzma:1gb"), 5000)
        self.assertEqual(lz[1].budget, 1 << 30)
        self.assertEqual(lz[1].limit, None)

    def test_lzma_decodes_only_what_is_needed(self):
        """Spec A.6/A.7: bytes past what the files need are not looked at, so damage there,
        or a match further back than the capped dictionary there, is not an error."""
        r = random.Random(1)
        data = bytes(r.choice(b"abcdefgh ") for _ in range(300_000))
        bad = bytearray(pack("lzma:1mb", data))
        bad[-1] ^= 0xFF
        bad[-2] ^= 0x55
        opts = {"lc": 3, "lp": 0, "pb": 2}
        for chunk in (1 << 20, 4093):
            stage = fc.LzmaStage(Mem(bytes(bad)), opts, 1 << 20, chunk, limit=100_000)
            self.assertEqual(drain(stage, chunk), data[:100_000])
        with self.assertRaises(CodecError):
            drain(fc.LzmaStage(Mem(bytes(bad)), opts, 1 << 20, 1 << 20))
        with self.assertRaises(CodecError):  # the limit at the stream's own length: seen
            drain(fc.LzmaStage(Mem(bytes(bad)), opts, 1 << 20, 1 << 20, limit=300_000))

    def test_lzma_start_failure_is_a_start_error(self):
        m = farc.parse_method("rep+lzma:1mb")
        with mock.patch.object(fc.lzma, "LZMADecompressor", side_effect=MemoryError), \
                self.assertRaises(fc.StartError):
            fc.open_chain(m, -1, 0, 0, 10, 1 << 20, lambda: None)


# --- whole chains, through a file -----------------------------------------------------------

CHAINS = ["exe+storing", "storing+exe", "exe+lzma:1mb", "delta+lzma:1mb", "exe+delta+lzma:1mb",
          "rep:1mb+lzma:1mb", "rep:1mb+exe+delta+lzma:1mb", "rep:64kb+exe+delta",
          "lzma:1mb+storing", "exe+exe+lzma:1mb"]


def sample(n=EXE_ROUND + 70_000):
    part = x86ish(n // 3, 3)
    table = b"".join((i * 7).to_bytes(4, "little") for i in range(n // 12))
    return (part + table + part[:n // 4] + os.urandom(1000))[:n]


class Chains(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def decode(self, method, enc, size, chunk=1 << 20, stop=lambda: None):
        path = Path(self._tmp.name) / "blk"
        path.write_bytes(b"pad" + enc)
        fd = os.open(path, os.O_RDONLY)
        try:
            top = fc.open_chain(farc.parse_method(method), fd, 3, len(enc), size, chunk, stop)
            out = bytearray()
            while len(out) < size:
                piece = top.read(min(chunk, size - len(out)))
                if not piece:
                    break
                out += piece
            return bytes(out)
        finally:
            os.close(fd)

    def test_chains_round_trip_with_small_and_large_chunks(self):
        data = sample()
        for method in CHAINS:
            enc = pack(method, data)
            for chunk in (1 << 20, 4093):
                with self.subTest(method=method, chunk=chunk):
                    self.assertEqual(self.decode(method, enc, len(data), chunk), data)

    def test_files_shorter_than_the_stream(self):
        r = random.Random(2)
        part = bytes(r.getrandbits(8) for _ in range(5000)).replace(b"\xe8", b"\x01")
        data = part.replace(b"\xe9", b"\x01") * 4  # matches 5000 bytes back after byte 5000
        for method in ("lzma:1mb", "exe+lzma:1mb", "exe+exe+lzma:1mb"):
            with self.subTest(method=method):
                enc = pack(method, data)
                self.assertEqual(self.decode(method, enc, 100), data[:100])
        code = bytearray(x86ish(20_000, 7))
        code[995:1005] = b"\x90" * 5 + b"\xe8\x10\x20\x00\x00"  # a call across the files' end
        code = bytes(code)
        for method in ("exe+lzma:1mb", "exe+exe+lzma:1mb", "exe+rep:1mb+exe+lzma:1mb"):
            enc = pack(method, code)
            for size in range(1000, 1012):
                with self.subTest(method=method, size=size):
                    self.assertEqual(self.decode(method, enc, size), code[:size])
        bad = bytearray(pack("lzma:1mb", data))
        bad[-3:] = b"\xff\x00\xff"  # damaged end, after what the files need
        self.assertEqual(self.decode("lzma:1mb", bytes(bad), 1000), data[:1000])
        self.assertEqual(self.decode("exe+lzma:1mb", bytes(bad), 1000), data[:1000])
        with self.assertRaises(CodecError):  # exe needs the end of the stream here
            self.decode("exe+lzma:1mb", bytes(bad), len(data))

    def test_stop_is_seen_in_empty_blocks_under_lzma(self):
        empty = u32(8) + u32(0) + u32(0)
        streams = {"rep:1mb+lzma:1mb": u32(16) + empty * 20_000
                   + rep_stream(16, [([b"A"], [])])[4:],
                   "delta+lzma:1mb": (u32(0) + u32(0)) * 60 + delta_block(b"A")}
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 10:
                raise Stopped
        for method, inner in streams.items():
            enc = pack("lzma:1mb", inner)
            self.assertEqual(self.decode(method, enc, 1), b"A")
            calls.clear()
            with self.subTest(method=method), self.assertRaises(Stopped):
                self.decode(method, enc, 1, stop=stop)

    def test_stop_is_checked_while_the_chain_reads(self):
        data = sample()
        enc = pack("rep:1mb+exe+delta+lzma:1mb", data)
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 3:
                raise KeyboardInterrupt

        with self.assertRaises(KeyboardInterrupt):
            self.decode("rep:1mb+exe+delta+lzma:1mb", enc, len(data), 512, stop)


class ChainedExtraction(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        base = Path(self._tmp.name)
        self.src = base / "src"
        self.src.mkdir()
        self.setup = self.src / "setup.exe"
        self.setup.write_bytes(b"MZ" + b"\0" * 100)
        self.prefix = base / "data" / "gitfurl" / "prefixes" / "p1"
        (self.prefix / "drive_c").mkdir(parents=True)
        patcher = mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(base / "data"),
                                                 "HOME": str(base)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.dest = self.prefix / "drive_c" / "Games" / "p1"

    def put(self, data: bytes):
        (self.src / "a.arc").write_bytes(data)

    def test_every_chain_extracts_byte_for_byte(self):
        data = sample()
        files = [F("one.exe", data[:100_000]), F("two.bin", data[100_000:]), F("empty", b"")]
        blocks = [(m, [F(f"{i}-{f[1]}", f[2]) for f in files]) for i, m in enumerate(CHAINS)]
        self.put(make_archive(blocks))
        plan = extract.plan_extract(self.setup, "p1")
        text = "\n".join(plan.describe())
        self.assertIn("memory: up to", text)
        self.assertGreater(plan.memory, fc.EXE_ROUND)
        res = extract.extract(plan)
        self.assertTrue(res.ok, res.lines())
        for i in range(len(CHAINS)):
            for f in files:
                self.assertEqual((self.dest / f"{i}-{f[1]}").read_bytes(), f[2], (i, f[1]))

    def test_summary_names_the_chain(self):
        self.put(make_archive([("rep:1mb+exe+delta+lzma:8mb:normal:32", [F("a", b"a" * 99)])]))
        arc = farc.read_archive(self.src / "a.arc")
        self.assertIn("1 data block: 0 storing, 0 lzma, 1 rep+exe+delta+lzma",
                      farc.summarise(arc))

    def test_damaged_chain_data_is_an_error_in_the_result(self):
        data = sample()
        self.put(make_archive([("rep:1mb+exe+delta+lzma:1mb", [F("a", data), F("b", data)])]))
        blk = extract.plan_extract(self.setup, "p1").archives[0].archive.blocks[0]
        raw = bytearray((self.src / "a.arc").read_bytes())
        for i in range(blk.offset + 50, blk.offset + blk.compsize - 50):
            raw[i] ^= 0x33
        self.put(bytes(raw))
        res = extract.extract(extract.plan_extract(self.setup, "p1"))
        self.assertFalse(res.ok)
        self.assertIn("could not be decompressed", res.error or "")
        total = sum(p.stat().st_size for p in self.dest.iterdir())
        self.assertLessEqual(total, 2 * len(data))

    def test_block_shorter_than_its_files(self):
        self.put(make_archive([("exe+delta+lzma:1mb", [F("a", b"a" * 100),
                                                       F("b", b"b" * 100, size=5000)])]))
        res = extract.extract(extract.plan_extract(self.setup, "p1"))
        self.assertIn("ended before", res.error)
        self.assertEqual((self.dest / "a").read_bytes(), b"a" * 100)

    def test_block_longer_than_its_files_writes_the_declared_size(self):
        self.put(make_archive([("rep:1mb+lzma:1mb", [F("a", b"abc" * 1000, size=1000)])]))
        res = extract.extract(extract.plan_extract(self.setup, "p1"))
        self.assertEqual((self.dest / "a").read_bytes(), (b"abc" * 1000)[:1000])
        self.assertEqual(res.crc_errors, ["a"])  # the CRC was of all 3000 bytes
        self.assertIsNone(res.error)

    def test_stop_in_a_chained_block(self):
        self.put(make_archive([("rep:1mb+exe+delta+lzma:1mb", [F("a", sample())])]))

        class Cancel:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 6

        with mock.patch.object(extract, "CHUNK", 4096):
            res = extract.extract(extract.plan_extract(self.setup, "p1"), cancel=Cancel())
        self.assertTrue(res.stopped)
        self.assertEqual(res.incomplete, "a")

    def test_rep_window_over_the_cap_in_the_stream(self):
        raw = make_archive([("rep:1mb", [F("a", b"zz")])])
        good = pack("rep:1mb", b"zz")
        at = raw.index(good)
        self.put(raw[:at] + u32(farc.DICT_CAP + 1) + raw[at + 4:])
        res = extract.extract(extract.plan_extract(self.setup, "p1"))
        self.assertIn("over the 1024 MiB limit", res.error or "")
        self.assertEqual(res.files, 0)

    def test_unhandled_methods_in_a_chain_are_refused_without_naming_them(self):
        self.put(make_archive([("rep:1mb+zzzmethod:3+lzma", [F("a", b"1")])]))
        with self.assertRaises(extract.ExtractRefused) as cm:
            extract.plan_extract(self.setup, "p1")
        self.assertIn(farc.KIND_OTHER, str(cm.exception))
        self.assertNotIn("zzz", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
