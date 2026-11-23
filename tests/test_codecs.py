"""FreeArc method chains: the exe, delta, rep, 4x4 and srep decoders, lzma inside a chain, the
pipeline and extraction of chained archives. Every stream is built in memory by the test-only
encoders in tests/farc_writer.py, which follow the same clean-room specifications as the
decoders, plus the specifications' own worked examples and test vectors."""

import hashlib
import os
import random
import tempfile
import tracemalloc
import unittest
import zlib
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
        srep_encode,
        srep_header,
        u32,
        x4_encode,
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
        srep_encode,
        srep_header,
        u32,
        x4_encode,
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

    def test_parse_4x4_and_srep(self):
        ok = {
            "4x4:lzma": "4x4", "4x4:b8mb:lzma:96mb:normal": "4x4", "4x4:t3:b1mb:lzma": "4x4",
            "4x4:t4:i0:r99:lzma:1mb": "4x4", "4x4:r99.5:lzma": "4x4", "4x4:8mb:lzma": "4x4",
            "4x4:4:lzma": "4x4", "4x4:i0:lzma:8mb:max": "4x4",
            "4x4:b16mb:lzma:max:d32m": "4x4",
            "rep:96mb+exe+delta+4x4:lzma:96m:normal:16:mc8": "rep+exe+delta+4x4",
            "4x4:lzma:96mb:fast:32:mc4": "4x4", "srep": "srep", "srep:m3f": "srep",
            "srep64:m3f:a2": "srep", "srep_new:m3f:a16:d1g:1": "srep", "srep_old": "srep",
            "srep:m3f:b64mb": "srep", "srep:m3f:a0/0+lzma:8mb:normal:32": "srep+lzma",
            "srep:m3f:hash=siphash+exe+delta+4x4:lzma:45mb:normal:32": "srep+exe+delta+4x4",
            "rep:64mb+srep:m3f+lzma:8mb": "rep+srep+lzma", "srep:m3f:hash-+lzma": "srep+lzma",
        }
        for text, kind in ok.items():
            with self.subTest(text=text):
                self.assertEqual(farc.parse_method(text).kind, kind)
        sizes = {"4x4:b8:lzma": 256, "4x4:8mb:lzma": 8 << 20, "4x4:lzma": 0,
                 "srep": farc.SREP_BLOCK_DEFAULT, "srep:m3f:b1mb": farc.SREP_BLOCK_DEFAULT,
                 "srep:b32mb": 32 << 20}
        for text, size in sizes.items():
            self.assertEqual(farc.parse_method(text).stages[0].size, size, text)
        self.assertEqual(farc.parse_method("4x4:lzma:96m:lc4:lp0").stages[0].lzma,
                         {"dict_size": 96 << 20, "lc": 4, "lp": 0, "pb": 2})
        self.assertIsNone(farc.parse_method("4x4:lzma").lzma)  # not a single lzma
        bad = ["4x4", "4x4:4:b8mb", "4x4:zzz:c3", "4x4:abc", "4x4:b7mb:qqq", "4x4::lzma",
               "4x4:x9:lzma", "4x4:b8q:lzma", "4x4:r9x:lzma", "4x4:t3x:lzma", "4x4:b2gb:lzma",
               "4x4:lzma:lc8", "4x4:exe", "4x4:b=8mb:lzma", "4x4:lzma+zzz", "4x4:r.5:lzma",
               "4X4:lzma",
               "srep:index=x.idx", "srep:m3f:index", "srep:b65mb", "srep:b1zz", "srep::m3f",
               "srepx", "SREP", "srep:"]
        for text in bad:
            with self.subTest(text=text):
                m = farc.parse_method(text)
                self.assertFalse(m.supported)
                self.assertEqual(m.kind, farc.KIND_OTHER)
                self.assertNotIn(text, m.reason + repr(m))

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
        x = fc.stage_plan(farc.parse_method("exe+4x4:lzma:1gb+lzma:1gb"), 5000)
        self.assertEqual([p.budget for p in x[1:]], [5000 + fc.EXE_SLACK, 1 << 30])
        self.assertEqual([p.limit for p in x], [5000, 5000 + fc.EXE_SLACK, None])
        s = fc.stage_plan(farc.parse_method("srep+lzma:8mb"), 1000)[0]
        self.assertEqual(s.budget, fc.SREP_MIN_BUDGET)
        self.assertEqual(s.memory, s.budget + fc.srep_block_memory(8 << 20))
        s = fc.stage_plan(farc.parse_method("srep:b32mb"), 100 << 20)[0]
        self.assertEqual(s.budget, 200 << 20)
        self.assertEqual(s.memory, (200 << 20) + fc.srep_block_memory(32 << 20))
        s = fc.stage_plan(farc.parse_method("rep+srep+lzma"), 100)[1]  # feeds rep: no limit
        self.assertEqual((s.budget, s.limit), (fc.SREP_MEMORY_CAP, None))
        self.assertEqual(fc.stage_plan(farc.parse_method("srep"), 1 << 40)[0].budget,
                         fc.SREP_MEMORY_CAP)

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


# --- 4x4 ------------------------------------------------------------------------------------

LZ = {"lc": 3, "lp": 0, "pb": 2}
X4_V1 = bytes.fromhex("000000002c0100001100000000309888ad4b36ed2f807bfffff6b04000")
X4_V2 = bytes.fromhex("00000000ffffffff10000000000102030405060708090a0b0c0d0e0f")
X4_V3 = bytes.fromhex("00000000f00000000f000000003c1e4bc06ce3a1bdfffffdbe2000ffffffff1000"
                      "0000000102030405060708090a0b0c0d0e0f")


def x4(data, step=1 << 20, chunk=1 << 20, limit=None, stop=None, dict_size=1 << 20):
    return drain(fc.X4Stage(Mem(data, step), LZ, dict_size, chunk, limit, stop))


class X4(unittest.TestCase):
    def test_spec_vectors(self):
        """4x4 spec section 8, V1-V4: bytes, CRC-32 and SHA-256."""
        cases = [(X4_V1, 300, 0xD0C4B884,
                  "d9f5aeb06abebb3be3f38adec9a2e3b94228d52193be923eb4e24c9b56ee0930"),
                 (X4_V2, 16, 0xCECEE288,
                  "be45cb2605bf36bebde684841a28f0fd43c69850a3dce5fedba69928ee3a8991"),
                 (X4_V3, 256, 0x5FE1DA41,
                  "6eb8bccbc1c4a784c0c4ce8529f856c2f35329680dfc79cc84b93aa1b3d71fcc")]
        for enc, size, crc, sha in cases:
            for step, chunk in ((1 << 20, 1 << 20), (1, 3), (5, 7)):
                with self.subTest(size=size, step=step):
                    out = x4(enc, step, chunk)
                    self.assertEqual((len(out), zlib.crc32(out), hashlib.sha256(out).hexdigest()),
                                     (size, crc, sha))
        self.assertEqual(x4(X4_V1), b"abc" * 100)
        self.assertEqual(x4(b"\0\0\0\0"), b"")
        self.assertEqual(x4(b""), b"")

    def test_spec_damage_vectors(self):
        v1, v2 = bytearray(X4_V1), bytearray(X4_V2)
        d11 = bytearray(v1)
        d11[12 + 6] ^= 0xFF
        d13 = bytearray(v1)
        d13[8:12] = u32(18)
        bad = {
            "D1": b"\1\0\0\0" + X4_V1[4:], "D2": b"\0\0\0\0\x2c\x01\x00",
            "D3": X4_V1[:4] + u32(301) + X4_V1[8:], "D4": X4_V1[:4] + u32(299) + X4_V1[8:],
            "D5": X4_V1[:-1], "D6": X4_V2[:8] + u32(17) + X4_V2[12:],
            "D7": bytes(12), "D8": b"\0\0\0\0\xff\xff\xff\xff\0\0\0\0",
            "D9": b"\0\0\0\0" + u32(1 << 31) + u32(1) + b"\0",
            "D10": b"\0\0\0\0" + u32(10) + u32(81920), "D11": d11, "D12": X4_V1 + b"\0",
            "D13": bytes(d13) + b"\0", "short version": b"\0\0", "stored too big": X4_V2[:8]
            + u32(1 << 31),
        }
        for name, enc in bad.items():
            for step in (1, 1 << 20):
                with self.subTest(name=name, step=step), self.assertRaises(CodecError):
                    x4(bytes(enc), step)
        for i in range(12, len(v1)):  # every single-byte flip of V1's payload
            flip = bytearray(v1)
            flip[i] ^= 0xFF
            for step in (1, 1 << 20):
                with self.subTest(flip=i, step=step), self.assertRaises(CodecError):
                    x4(bytes(flip), step)
        self.assertEqual(x4(bytes(v2) + bytes(v2[4:])), bytes(range(16)) * 2)

    def test_limit_stops_before_the_stored_piece(self):
        src = Mem(X4_V3 + b"\xff" * 5, step=1)  # damaged tail, never read
        out = drain(fc.X4Stage(src, LZ, 1 << 20, 1, limit=100))
        self.assertEqual(out, (b"xyz" * 80)[:100])
        self.assertLessEqual(src.at, 4 + 8 + 15)

    def test_round_trips_with_unequal_and_stored_pieces(self):
        r = random.Random(4)
        data = (b"hello 4x4 " * 3000 + bytes(r.getrandbits(8) for _ in range(40_000))
                + bytes(5000) + b"tail")
        for sizes in ([1 << 16], [1, 7, 4096, 33_000], [len(data)], [40_001, 3]):
            enc = x4_encode(data, sizes=sizes)
            for step, chunk in ((1 << 20, 1 << 20), (999, 4093)):
                with self.subTest(sizes=sizes, step=step):
                    self.assertEqual(x4(enc, step, chunk), data)
        self.assertIn(b"\xff\xff\xff\xff", x4_encode(os.urandom(5000)))  # stored

    def test_each_piece_gets_a_fresh_small_decoder(self):
        made = []
        real = fc.lzma.LZMADecompressor

        def spy(*a, **k):
            made.append(k["filters"][0]["dict_size"])
            return real(*a, **k)
        enc = x4_encode(b"q" * 70_000, sizes=[100, 69_900])
        with mock.patch.object(fc.lzma, "LZMADecompressor", side_effect=spy):
            self.assertEqual(x4(enc, dict_size=1 << 20), b"q" * 70_000)
        self.assertEqual(made, [1 << 20, 4096, 69_900])

    def test_start_failure_and_stop(self):
        with mock.patch.object(fc.lzma, "LZMADecompressor", side_effect=MemoryError), \
                self.assertRaises(fc.StartError):
            fc.X4Stage(Mem(b""), LZ, 1 << 20, 1 << 20)
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 5:
                raise Stopped
        enc = x4_encode(bytes(range(256)) * 40, sizes=[1])
        with self.assertRaises(Stopped):
            x4(enc, stop=stop)


# --- srep -----------------------------------------------------------------------------------

def hx(text: str) -> bytes:
    return bytes.fromhex("".join(line.split(":", 1)[1] for line in text.strip().splitlines()))


SREP = {  # srep spec section 10 (hand-built), decoded output
    "A": (hx("""
0000: 17 18 35 26 53 52 45 50 02 01 00 00 04 00 00 00
0010: 0a 00 00 00 12 00 00 00 10 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 08 00 00 00
0030: 08 00 00 00 00 00 00 00 04 00 00 00 61 62 63 64
0040: 65 66 67 68 58 59"""), b"abcdefghabcdefghXY"),
    "B": (hx("""
0000: 17 18 35 26 53 52 45 50 02 00 00 00 04 00 00 00
0010: 0a 00 00 00 12 00 00 00 10 00 00 00 c7 d0 34 c7
0020: 8c 81 5e 14 64 72 c6 f0 a9 0e 63 55 08 00 00 00
0030: 08 00 00 00 00 00 00 00 04 00 00 00 61 62 63 64
0040: 65 66 67 68 58 59"""), b"abcdefghabcdefghXY"),
    "C": (hx("""
0000: 17 18 35 26 53 52 45 50 01 01 00 00 04 00 00 00
0010: 04 00 00 00 0c 00 00 00 0c 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 04 00 00 00
0030: 01 00 00 00 01 00 00 00 77 78 79 7a"""), b"wxyzwxyzwxyz"),
    "D": (hx("""
0000: 17 18 35 26 53 52 45 50 01 01 00 00 04 00 00 00
0010: 05 00 00 00 09 00 00 00 0c 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 05 00 00 00
0030: 01 00 00 00 00 00 00 00 51 61 62 63 64"""), b"QabcdQabc"),
    "D2": (hx("""
0000: 17 18 35 26 53 52 45 50 01 01 00 00 04 00 00 00
0010: 05 00 00 00 09 00 00 00 0c 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 05 00 00 00
0030: 00 00 00 00 00 00 00 00 51 61 62 63 64"""), b"Qabcddddd"),
    "E": (hx("""
0000: 17 18 35 26 53 52 45 50 03 01 00 00 00 00 00 00
0010: 08 00 00 00 08 00 00 00 20 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030: 0c 00 00 00 00 00 00 00 04 00 00 00 04 00 00 00
0040: 04 00 00 00 00 00 00 00 04 00 00 00 41 42 43 44
0050: 45 46 47 48 01 00 00 00 09 00 00 00 00 00 00 00
0060: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0070: 21"""), b"ABCDEFGHEFGHABCD!"),
    "E2": (hx("""
0000: 17 18 35 26 53 52 45 50 03 00 00 00 00 00 00 00
0010: 08 00 00 00 08 00 00 00 20 00 00 00 47 83 e7 84
0020: b4 fa 2f ba 9e 4d 65 02 db c6 4f 8f 00 00 00 00
0030: 0c 00 00 00 00 00 00 00 04 00 00 00 04 00 00 00
0040: 04 00 00 00 00 00 00 00 04 00 00 00 41 42 43 44
0050: 45 46 47 48 01 00 00 00 09 00 00 00 00 00 00 00
0060: 3b 82 2a 2d 2f 73 c0 95 57 fb 54 99 66 77 39 05
0070: 21"""), b"ABCDEFGHEFGHABCD!"),
    "F": (hx("""
0000: 17 18 35 26 53 52 45 50 04 01 00 00 00 00 00 00
0010: 08 00 00 00 08 00 00 00 00 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 41 42 43 44
0030: 45 46 47 48 01 00 00 00 09 00 00 00 00 00 00 00
0040: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0050: 21 00 00 00 00 0c 00 00 00 00 00 00 00 04 00 00
0060: 00 04 00 00 00 04 00 00 00 00 00 00 00 04 00 00
0070: 00 20 00 00 00 00 00 00 00 20 00 00 00 00 00 00
0080: 00 20 00 00 00 01 00 00 00 ac ad ba af e8 e7 ca
0090: d9"""), b"ABCDEFGHEFGHABCD!"),
    "F0": (hx("""
0000: 17 18 35 26 53 52 45 50 04 01 00 00 00 00 00 00
0010: 00 00 00 00 00 00 00 00 18 00 00 00 01 00 00 00
0020: ac ad ba af e8 e7 ca d9"""), b""),
    "G": (hx("""
0000: 17 18 35 26 53 52 45 50 03 01 00 00 00 00 00 00
0010: 02 00 00 00 08 00 00 00 10 00 00 00 00 00 00 00
0020: 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00
0030: 02 00 00 00 00 00 00 00 06 00 00 00 61 62"""), b"abababab"),
    "J": (hx("""
0000: 17 18 35 26 53 52 45 50 02 04 20 00 04 00 00 00
0010: 00 01 02 03 04 05 06 07 08 09 0a 0b 0c 0d 0e 0f
0020: 10 11 12 13 14 15 16 17 18 19 1a 1b 1c 1d 1e 1f
0030: 0a 00 00 00 12 00 00 00 10 00 00 00 ea ef 26 89
0040: 48 c4 fd db a2 30 f1 8d aa ab 74 0d 08 00 00 00
0050: 08 00 00 00 00 00 00 00 04 00 00 00 61 62 63 64
0060: 65 66 67 68 58 59"""), b"abcdefghabcdefghXY"),
}


def srep(data, step=1 << 20, chunk=1 << 20, block=1 << 20, budget=16 << 20, limit=None,
         stop=None):
    return drain(fc.SrepStage(Mem(data, step), block, budget, chunk, limit, stop))


def patch(data: bytes, at: int, new: bytes) -> bytes:
    return data[:at] + new + data[at + len(new):]


def srep_full_block(version: int, size: int, kind: str = "none") -> tuple[bytes, bytes]:
    """(stream, output): one srep block of `size` bytes, format 2 or 3, base 0: 8 literal
    bytes, then size // 8 - 1 matches of 8 bytes each, the most records such a block holds."""
    n = size // 8 - 1
    eight = (8).to_bytes(8, "little")
    if version == 2:  # literal run before the match, distance back, length
        recs = u32(8) + eight + u32(8) + (u32(0) + eight + u32(8)) * (n - 1)
    else:  # source from the last source, distance forward, length
        recs = u32(0) + eight + u32(8) + (u32(8) + eight + u32(8)) * (n - 1)
    out = b"ABCDEFGH" * (size // 8)
    head, check, hlen = srep_header(version, kind)
    csum = check(out)[:hlen] if check else bytes(hlen)
    return head + u32(8) + u32(size) + u32(len(recs)) + csum + recs + out[:8], out


class Srep(unittest.TestCase):
    def test_spec_vectors(self):
        for name, (enc, out) in SREP.items():
            for step in (1, 7, 1 << 20):
                with self.subTest(name=name, step=step):
                    self.assertEqual(srep(enc, step), out)
        for version in (1, 2, 3):  # a header alone decodes to nothing
            self.assertEqual(srep(srep_header(version, base=4)[0]), b"")

    def test_checksums(self):
        """FIPS-197 AES examples, the SipHash paper's example, and the srep spec's VHASH
        values (audit-derived; agreeing with streams made by srep 3.93a, see docs/testing.md)."""
        p = bytes.fromhex("00112233445566778899aabbccddeeff")
        self.assertEqual(fc.aes_encrypt_block(bytes(range(16)), p).hex(),
                         "69c4e0d86a7b0430d8cdb78070b4c55a")
        self.assertEqual(fc.aes_encrypt_block(bytes(range(32)), p).hex(),
                         "8ea2b7ca516745bfeafc49904b496089")
        self.assertEqual(fc.siphash24(bytes(range(16)), bytes(range(15))), 0xA129CA6149BE45E5)
        v = fc.Vhash(bytes(range(32)))
        for msg, want in ((b"", "2eca48aefe4117ae20f2769dbdfe8de3"),
                          (b"abcdefghabcdefghXY", "eaef268948c4fddba230f18daaab740d"),
                          ((b"abc" * 1700), "76cee655fcebc10443dc1d633fc06f5d"),
                          (bytes(4096), "951705578dce7014fe78af3271430d4d"),
                          (bytes(8192), "0bc07b6f898437300c3702b1f23a6bec")):
            self.assertEqual(v.digest(msg).hex(), want, len(msg))

    def test_spec_negative_cases(self):
        a, e, f = SREP["A"][0], SREP["E"][0], SREP["F"][0]
        bad = {
            "unknown version": patch(a, 8, b"\5"), "offset 0": patch(a, 0x30, u32(0)),
            "offset 9": patch(a, 0x30, u32(9)), "N 19": patch(a, 0x14, u32(19)),
            "trailing byte": a + b"\0", "short match list": a[:0x2c] + a[0x30:],
            "E offset 13": patch(e, 0x40, u32(13)), "E duplicate dest": patch(e, 0x40, u32(8)),
            "E cut after block 1": e[:0x54], "F footer signature": f[:-1] + b"\0",
            "F sizes 16/16": patch(f, 0x71, u32(16) + u32(16)), "flag 02": b"\2" + a,
            "bad signature": patch(a, 4, b"SREQ"), "kind 9": patch(a, 9, b"\x09"),
            "md5 with sha1 length": patch(a, 9, b"\0\0\4"), "v1 base 0": patch(SREP["C"][0], 12,
                                                                        u32(0)),
            "B literal flipped": patch(SREP["B"][0], 0x40, b"E"),
            "J literal flipped": patch(SREP["J"][0], 0x60, b"E"),
            "E2 literal flipped": patch(SREP["E2"][0], 0x70, b"?"),
            "truncated header": a[:10], "truncated seed": SREP["J"][0][:30],
            "truncated block header": a[:20], "truncated literals": a[:-1],
            "empty": b"", "flag only": b"\1",
            "F footer version 2": patch(f, 0x85, b"\2"), "F0 truncated": SREP["F0"][0][:-1],
            "zero length match": patch(SREP["G"][0], 0x38, u32(0)),
            "source past block": patch(SREP["G"][0], 0x2c, u32(2)),
            "zero end mark then data": a + bytes(12 + 16) + b"x",
            "block 0 bytes": a + u32(0) + u32(0) + u32(16) + bytes(16) + bytes(16),
        }
        for name, enc in bad.items():
            for step in (1, 1 << 20):
                with self.subTest(name=name, step=step), self.assertRaises(CodecError):
                    srep(enc, step)
        self.assertEqual(srep(a + bytes(12 + 16)), b"abcdefghabcdefghXY")  # srep's end mark

    def test_flag_byte(self):
        a, out = SREP["A"]
        self.assertEqual(srep(b"\1" + a), out)
        self.assertEqual(srep(b"\0anything at all"), b"anything at all")
        self.assertEqual(srep(b"\0"), b"")
        self.assertEqual(srep(b"\1" + SREP["F"][0]), SREP["F"][1])

    def test_round_trips(self):
        r = random.Random(9)
        part = bytes(r.getrandbits(8) for _ in range(3000))
        data = (part + b"srep " * 400 + part[500:2500] + bytes(2000) + part) * 4
        for version in (2, 3, 4):
            for kind in ("none", "md5", "sha1", "sha512", "vmac", "siphash"):
                for block, base in ((1000, 0), (4096, 16), (1 << 16, 0)):
                    enc = srep_encode(data, version, kind, block=block, base=base)
                    self.assertLess(len(enc), len(data) // 2)
                    with self.subTest(version=version, kind=kind, block=block):
                        self.assertEqual(srep(enc, step=997, chunk=4093), data)
                        bad = bytearray(enc)
                        bad[-40 if version == 4 else -1] ^= 1
                        if kind != "none":
                            with self.assertRaises(CodecError):
                                srep(bytes(bad))
        self.assertEqual(srep(srep_encode(b"", 4)), b"")
        self.assertEqual(srep(srep_encode(b"x", 3, "vmac", flag=b"\1")), b"x")

    def test_caps(self):
        data = bytes(range(256)) * 400
        enc = srep_encode(data, 3, block=4096)
        self.assertEqual(srep(enc, block=4096), data)
        with self.assertRaisesRegex(CodecError, "larger than gitfurl handles"):
            srep(enc, block=4095)
        with self.assertRaisesRegex(CodecError, "more than the block's files hold"):
            srep(enc, limit=len(data) - 1)
        self.assertEqual(srep(enc, limit=len(data)), data)
        # pending matches: each counts its data and SREP_PENDING_COST against the budget
        far = os.urandom(3000) + bytes(50_000) + os.urandom(3000)
        far = far[:3000] + far[53_000:] + far[:3000]
        enc = srep_encode(far, 3, block=1000)
        self.assertEqual(srep(enc, budget=4000), far)
        with self.assertRaisesRegex(CodecError, "more memory"):
            srep(enc, budget=3000)
        # format 4 read from another stage: its input is held, within the budget
        enc4 = srep_encode(data, 4, block=4096)
        self.assertEqual(srep(enc4, budget=len(enc4) + 2000), data)
        with self.assertRaisesRegex(CodecError, "more memory"):
            srep(enc4, budget=len(enc4) - 1)
        # formats 1 and 2: matches may reach back as far as the budget keeps
        back = os.urandom(5000) + os.urandom(20_000) + b"x"
        back += back[:5000]
        enc2 = srep_encode(back, 2, block=1000, base=16)
        self.assertEqual(srep(enc2, budget=len(back)), back)
        with self.assertRaisesRegex(CodecError, "further back than gitfurl keeps"):
            srep(enc2, budget=len(back) - 10_000)

    def test_stop_is_checked_between_blocks_and_records(self):
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 5:
                raise Stopped
        enc = srep_encode(bytes(range(256)) * 100, 3, block=100)
        with self.assertRaises(Stopped):
            srep(enc, stop=stop)
        calls.clear()
        records = b"".join(u32(0) + u32(1) + u32(0) + u32(1) for _ in range(6 * fc.STOP_EVERY))
        head = srep_header(3, base=0)[0]
        many = head + u32(1) + u32(1 << 20) + u32(len(records)) + bytes(16) + records + b"z"
        with self.assertRaises(Stopped):
            drain(fc.SrepStage(Mem(many), 1 << 20, 16 << 20, 1 << 20, stop=stop))
        self.assertGreater(len(calls), 3)

    def test_block_records_stay_within_the_block_memory(self):
        """A 1 MiB block with the most match records it can hold: the traced peak stays under
        srep_block_memory (plus the one chunk its input holds), and is not trivially small."""
        cap = 1 << 20
        enc, want = srep_full_block(3, cap)
        stage = fc.SrepStage(Mem(enc), cap, 16 << 20, 1 << 20)
        tracemalloc.start()
        try:
            out = stage.read(cap)
            peak = tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
        self.assertEqual(out, want)
        plan = fc.srep_block_memory(cap) + (1 << 20)
        self.assertLessEqual(peak, plan)
        self.assertGreater(peak, plan * 0.4)

    def test_checksums_check_stop_between_slices(self):
        """A block's checksum is computed a slice at a time, with a Stop check in between
        (1 MiB for md5, sha1, sha512 and SipHash, 256 NH blocks of 4096 bytes for VHASH)."""
        # three full slices and 13 bytes: three Stop checks, also for SipHash, whose last
        # 13 % 8 bytes are added after its slices
        data = bytes(range(256)) * (3 * fc.CHECK_SLICE // 256) + b"tail of block"
        for kind, num in (("md5", 0), ("sha1", 2), ("sha512", 3), ("vmac", 4), ("siphash", 5)):
            head, check, hlen = srep_header(3, kind)
            calls = []
            got = fc._srep_checker(num, head[16:])(data, lambda calls=calls: calls.append(1))
            with self.subTest(kind=kind):
                self.assertEqual(got[:hlen], check(data)[:hlen])
                self.assertEqual(len(calls), 3)
        # a Stop asked while a block's checksum is computed ends the stage there: one block
        # of literals only, with a wrong SipHash checksum (so without that check the stage
        # would end with "failed its checksum"); the first call is the stage's own refill
        size = 4 << 20
        head = srep_header(3, "siphash")[0]
        enc = head + u32(size) + u32(size) + u32(0) + bytes(8) + bytes(size)
        calls.clear()

        def stop():
            calls.append(1)
            if len(calls) > 1:
                raise Stopped
        with self.assertRaises(Stopped):
            srep(enc, block=size, stop=stop)
        self.assertEqual(len(calls), 2)
        with self.assertRaisesRegex(CodecError, "failed its checksum"):
            srep(enc, block=size, stop=lambda: None)

    def test_format_4_reads_the_archive_directly(self):
        data = bytes(range(256)) * 300 + os.urandom(1000)
        for flag in (b"", b"\1"):
            enc = srep_encode(data, 4, "md5", block=4096, flag=flag)
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "blk"
                path.write_bytes(b"pad" + enc + b"after")
                fd = os.open(path, os.O_RDONLY)
                try:
                    m = farc.parse_method("srep:m3")
                    top = fc.open_chain(m, fd, 3, len(enc), len(data), 1 << 20, lambda: None)
                    self.assertIsNone(top.spool)
                    self.assertEqual(drain(top), data)
                    self.assertIsNone(top.spool.data)  # never copied into memory
                finally:
                    os.close(fd)


# --- whole chains, through a file -----------------------------------------------------------

CHAINS = ["exe+storing", "storing+exe", "exe+lzma:1mb", "delta+lzma:1mb", "exe+delta+lzma:1mb",
          "rep:1mb+lzma:1mb", "rep:1mb+exe+delta+lzma:1mb", "rep:64kb+exe+delta",
          "lzma:1mb+storing", "exe+exe+lzma:1mb", "4x4:lzma:1mb", "rep:1mb+exe+delta+4x4:lzma:1mb",
          "srep+lzma:1mb", "srep:m3f+exe+delta+4x4:lzma:1mb", "rep:1mb+srep:m3+lzma:1mb",
          "srep:m3", "srep:m2o:hash=md5+4x4:lzma:1mb", "exe+srep:m1f:hash=siphash+lzma:1mb"]


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
        self.assertIn("srep keeps up to 1.0 GiB", text)  # rep+srep: no size known
        self.assertGreater(plan.memory, fc.EXE_ROUND)
        res = extract.extract(plan)
        self.assertTrue(res.ok, res.lines())
        for i in range(len(CHAINS)):
            for f in files:
                self.assertEqual((self.dest / f"{i}-{f[1]}").read_bytes(), f[2], (i, f[1]))

    def test_srep_and_4x4_memory_stays_within_the_plan(self):
        data = sample()
        for method in ("srep:m3f+4x4:lzma:1mb", "srep:m3+lzma:1mb", "srep:m2o"):
            m = farc.parse_method(method)
            enc = pack(method, data)
            self.put(make_archive([(method, [F("a", data)])]))
            blk = farc.read_archive(self.src / "a.arc").blocks[0]
            fd = os.open(self.src / "a.arc", os.O_RDONLY)
            try:
                top = fc.open_chain(m, fd, blk.offset, len(enc), len(data), 1 << 16,
                                    lambda: None)
                tracemalloc.start()
                try:
                    got = 0
                    while got < len(data):
                        piece = top.read(1 << 16)
                        self.assertTrue(piece)
                        got += len(piece)
                        del piece
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            finally:
                os.close(fd)
            self.assertLessEqual(peak, fc.chain_memory(m, len(data), 1 << 16), method)

    def test_srep_full_blocks_stay_within_the_plan(self):
        """8 MiB blocks (srep's default cap) with the most match records they can hold, read
        through open_chain from a file: the traced peak stays under the plan's figure, and the
        format 3 block (whose records the plan counts) uses a good part of it."""
        size = 8 << 20
        m = farc.parse_method("srep")
        plan = fc.chain_memory(m, size, 1 << 20)
        for version in (3, 2):
            enc, want = srep_full_block(version, size)
            path = self.src / "blk"
            path.write_bytes(enc)
            fd = os.open(path, os.O_RDONLY)
            try:
                top = fc.open_chain(m, fd, 0, len(enc), size, 1 << 20, lambda: None)
                h = hashlib.sha256()
                tracemalloc.start()
                try:
                    while piece := top.read(1 << 20):
                        h.update(piece)
                        del piece
                    peak = tracemalloc.get_traced_memory()[1]
                finally:
                    tracemalloc.stop()
            finally:
                os.close(fd)
            with self.subTest(version=version):
                self.assertEqual(h.digest(), hashlib.sha256(want).digest())
                self.assertLessEqual(peak, plan)
                if version == 3:
                    self.assertGreater(peak, plan * 0.4)

    def test_damaged_srep_block_is_an_error_in_the_result(self):
        data = sample()
        self.put(make_archive([("srep:m3f:hash=md5", [F("a", data)])]))
        blk = extract.plan_extract(self.setup, "p1").archives[0].archive.blocks[0]
        raw = bytearray((self.src / "a.arc").read_bytes())
        raw[blk.offset + blk.compsize - 10] ^= 1  # a literal of the last block
        self.put(bytes(raw))
        res = extract.extract(extract.plan_extract(self.setup, "p1"))
        self.assertIn("failed its checksum", res.error or "")

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
        self.assertFalse((self.dest / "a").exists())  # its CRC was of all 3000 bytes: removed
        self.assertEqual(res.crc_errors, ["a"])
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
