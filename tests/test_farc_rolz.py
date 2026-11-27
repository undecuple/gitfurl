"""The rolz stage (farc_rolz.py; an external FreeArc method, by gitfurl's own name).

Synthetic tests only. Streams are built with tests/rolz_writer.py: the test writes the values
the decoder should read (token by token, by hand), a stand-in reader records the decoder's
requests, and a plain rANS encoder turns them into packets that the real reader must decode
the same way. Expected outputs below are worked out by hand from the specification's rules,
not taken from the decoder. The real block's check (outside the repository) is described in
docs/testing.md.
"""

import os
import random
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

try:
    import rolz_writer as w
    from farc_writer import ENCODERS, F, make_archive
except ImportError:  # python -m unittest tests.test_farc_rolz
    from tests import rolz_writer as w
    from tests.farc_writer import ENCODERS, F, make_archive

from gitfurl import extract, farc
from gitfurl import farc_codecs as fc
from gitfurl import farc_rolz as r
from gitfurl.farc_rolz import RolzError

NAME = next(iter(farc.ROLZ_NAMES))  # the archive's text for the method; never shown


class Bytes:
    """read(off, n) over bytes, like the stage's own reader."""

    def __init__(self, data: bytes):
        self.data = data

    def __call__(self, off: int, n: int) -> bytes:
        assert 0 <= off and off + n <= len(self.data)
        return self.data[off:off + n]


def one_stream(payloads, t=0):
    """A container holding one stream of type t (one chunk of these packets), and its coder."""
    chunk = b"".join(w.packet(p) for p in payloads)
    data = w.container([(t, chunk)])
    box = r.Container(Bytes(data), len(data))
    return box, r.Coder(box, box.chunks[t])


def lz_stream(values, n, window=r.SMALL_WINDOW, rolz=False):
    """Bytes of a real LZ decoding the stream built from `values`, and the LZ."""
    ops, expect = w.lz_ops(values, n, window, rolz)
    _, cd = one_stream([w.encode(ops)])
    lz = r.LZ(cd, window, rolz, trim=not rolz)
    got = lz.take(n)
    assert got == expect
    return got, lz, cd


def decode(record: bytes, budget=r.WINDOW_CAP, limit=None, chunk=1 << 20) -> bytes:
    return b"".join(r.decode_block(Bytes(record), len(record), budget, None, chunk, limit))


KIND = {"literal": [1], "special": [0, 0, 0], "rep": [0, 0, 1], "rolz": [0, 1, 0],
        "match": [0, 1, 1]}


class Tables(unittest.TestCase):
    def test_buckets(self):
        self.assertEqual(r.TC_BIAS, 136)
        self.assertEqual(r.TL_BIAS, 255)
        self.assertEqual(r.TM_BIAS, 510)
        self.assertEqual(r.TGB[-1], 256)  # TG covers one byte
        self.assertEqual(r.TIB[-1], 65536)  # TI one word
        self.assertEqual(r.TKB[16] - r.TK_BIAS, 1)  # the centre bucket is the delta 0
        self.assertEqual((r.TGB[9], r.TG[9]), (192, 5))  # the specification's example
        self.assertEqual(r.TDB[1], 256)
        self.assertEqual([len(t) for t in (r.TA, r.TB, r.TC, r.TD, r.TE, r.TG, r.TI, r.TK,
                                            r.TL, r.TM, r.TN)],
                         [16, 16, 22, 22, 27, 16, 32, 31, 17, 17, 20])

    def test_literal_classes(self):
        self.assertEqual(len(r.LITCLS), 256)
        cases = {0x00: 0, 0x0F: 0, 0x10: 1, 0x20: 1, 0x21: 2, 0x30: 3, 0x39: 3, 0x3A: 2,
                 0x40: 2, 0x41: 4, 0x42: 5, 0x45: 4, 0x5A: 5, 0x5B: 6, 0x60: 6, 0x61: 7,
                 0x62: 8, 0x75: 7, 0x7A: 8, 0x7B: 9, 0x9F: 9, 0xA0: 10, 0xC0: 11, 0xFF: 12}
        for c, cls in cases.items():
            self.assertEqual(r.LITCLS[c], cls, hex(c))

    def test_bit_states(self):
        self.assertEqual(r.PROB[128], 1 << 31)
        for k in range(1, 128):
            self.assertEqual(r.PROB[k] + r.PROB[256 - k], 1 << 32)
            self.assertGreater(r.NEXT1[k], k)
            self.assertEqual(r.NEXT0[k], max(1, k - 1))
            self.assertEqual(r.NEXT1[256 - k], 256 - r.NEXT0[k])
            self.assertEqual(r.NEXT0[256 - k], 256 - r.NEXT1[k])
        self.assertEqual((r.NEXT1[128], r.NEXT0[128]), (129, 127))
        self.assertTrue(all(0 < r.P12[s] < 4096 for s in range(1, 256)))
        self.assertEqual(r.QS[128], r.QB[16])
        self.assertEqual(len(r.QB), 32)

    def test_cdf_starts(self):
        self.assertEqual(r.uniform(16), [i * 1024 for i in range(16)] + [0x4000])
        self.assertEqual(r.from_freq(r.F_DIST)[:4], [0, 2048, 4096, 5120])
        rep = r.from_freq(r.F_REP)
        self.assertEqual(rep[1], 64 * 0x4000 // 113)

    def test_cdf_update(self):
        cdf = r.uniform(16)
        r.update(cdf, 0)
        # i <= s: toward i; i > s: toward 0x407F - n + i, by 1/128 (rounded down)
        self.assertEqual(cdf[0], 0)
        self.assertEqual(cdf[1], 1024 + ((0x407F - 16 + 1 - 1024) >> 7))
        rng = random.Random(5)
        for n in (16, 17, 20, 22, 27, 31, 32):
            cdf = r.uniform(n)
            for _ in range(3000):
                r.update(cdf, rng.choice([0, n - 1, rng.randrange(n)]))
                self.assertEqual(cdf[0], 0)
                self.assertTrue(all(a < b for a, b in zip(cdf, cdf[1:])), n)


class Reader(unittest.TestCase):
    def test_hand_made_packet(self):
        # words: S1 low, S1 high, S0 low, S0 high; then nothing left to pull
        payload = b"".join(x.to_bytes(2, "little") for x in (0xFFFF, 0, 0xBCDE, 0x000A))
        _, cd = one_stream([payload])
        self.assertEqual(cd.raw(4), 0xE)  # works on S0 = 0x000ABCDE
        self.assertEqual(cd.c, 0xABCD)
        self.assertEqual(cd.o, 0xFFFF)
        self.assertFalse(cd.used_up())  # S0 is not at the end value
        self.assertEqual(cd.raw(0), 0)  # raw(0) is no operation
        self.assertEqual((cd.c, cd.o), (0xABCD, 0xFFFF))

    def _random_ops(self, seed, count=400):
        rng = random.Random(seed)

        class Rec(w.Script):
            def _next(self, what):
                if what == "bit":
                    return rng.randrange(2)
                if what == "symbol":
                    return self._sym
                return rng.randrange(1 << int(what[4:-1]))

        rec = Rec([])
        plan = []
        cdfs = [r.uniform(16), r.from_freq(r.F_REP), r.uniform(27), r.from_freq(r.F_ROLZIDX)]
        for _ in range(count):
            kind = rng.randrange(4)
            if kind == 0:
                p = rng.choice([1, 7, 2048, 4095, rng.randrange(1, 4096)])
                plan.append(("bit", p, rec.bit(p)))
            elif kind == 1:
                n = rng.choice([0, 1, 16, 17, 31, 32, 48, rng.randrange(33)])
                plan.append(("raw", n, rec.raw(n)))
            else:
                c = rng.randrange(4)
                rec._sym = rng.randrange(len(cdfs[c]) - 1)
                plan.append(("sym", c, rec.sym(cdfs[c])))
        return rec.ops, plan

    def _replay(self, cd, plan):
        cdfs = [r.uniform(16), r.from_freq(r.F_REP), r.uniform(27), r.from_freq(r.F_ROLZIDX)]
        for kind, arg, want in plan:
            got = cd.bit(arg) if kind == "bit" else cd.raw(arg) if kind == "raw" \
                else cd.sym(cdfs[arg])
            self.assertEqual(got, want)

    def test_round_trips(self):
        for seed in range(40):
            ops, plan = self._random_ops(seed)
            _, cd = one_stream([w.encode(ops)])
            self._replay(cd, plan)
            self.assertTrue(cd.used_up(), seed)

    def test_several_packets(self):
        for seed in range(10):
            ops, plan = self._random_ops(100 + seed, 600)
            cuts = sorted(random.Random(seed).sample(range(1, len(ops)), 3))
            _, cd = one_stream(w.encode_packets(ops, cuts))
            self._replay(cd, plan)
            self.assertTrue(cd.used_up())

    def test_damage(self):
        ops, plan = self._random_ops(7)
        payload = w.encode(ops)
        good = w.packet(payload)
        # a CRC that does not match
        bad = bytearray(good)
        bad[10] ^= 1
        data = w.container([(0, bytes(bad))])
        box = r.Container(Bytes(data), len(data))
        with self.assertRaisesRegex(RolzError, "CRC"):
            self._replay(r.Coder(box, box.chunks[0]), plan)
        # an extra word: not used up at the end
        _, cd = one_stream([b"\0\0" + payload])
        self._replay(cd, plan)
        self.assertFalse(cd.used_up())
        # a second packet that is never read: not used up
        _, cd = one_stream([payload, payload])
        self._replay(cd, plan)
        self.assertFalse(cd.used_up())
        # the stream runs out
        _, cd = one_stream([payload])
        self._replay(cd, plan)
        with self.assertRaisesRegex(RolzError, "runs out"):
            for _ in range(100):
                cd.raw(16)
        # odd or too short payloads
        for ps in ([payload + b"\0"], [b"\0" * 6, payload]):  # a chunk holds 15 bytes or more
            _, cd = one_stream(ps)
            with self.assertRaisesRegex(RolzError, "two coder states"):
                cd.bit(100)

    def test_unread_stream_without_chunks_is_used_up(self):
        self.assertTrue(r.Coder(None, []).used_up())


class Container(unittest.TestCase):
    def test_index(self):
        chunks = [(4, w.packet(b"a" * 8)), (3, w.packet(b"b" * 10)), (4, w.packet(b"c" * 30)),
                  (0, w.packet(b"d" * 8)), (1, w.packet(b"e" * 8) + w.packet(b"f" * 8))]
        data = w.container(chunks)
        box = r.Container(Bytes(data), len(data))
        at, want = 17, [[], [], [], [], []]
        for t, c in chunks:
            want[t].append((at, len(c)))
            at += len(c)
        self.assertEqual(box.chunks, want)

    def test_long_index(self):
        rng = random.Random(3)
        chunks = [(rng.randrange(5), w.packet(bytes(8 + 2 * rng.randrange(400))))
                  for _ in range(3000)]
        data = w.container(chunks)
        box = r.Container(Bytes(data), len(data))
        self.assertEqual(sum(len(c) for c in box.chunks), 3000)

    def test_varints(self):
        self.assertEqual(next(r._varints(bytes([14])))[0], 7)
        for v in (0, 1, 127, 128, 255, 300, 70000, 1 << 40):
            got = next(r._varints(w.varint(v)))
            self.assertEqual(got, (v, True))
        with self.assertRaisesRegex(RolzError, "too long"):
            next(r._varints(b"\x01" * 12))
        with self.assertRaisesRegex(RolzError, "ends inside"):
            next(r._varints(b"\x01"))

    def test_refused(self):
        good = w.container([(0, w.packet(b"d" * 8))])
        cases = {
            "magic": b"\0" + good[1:],
            "short": good[:16],
        }
        for what, data in cases.items():
            with self.assertRaisesRegex(RolzError, "magic", msg=what):
                r.Container(Bytes(data), len(data))

        def build(index_at=None, idx=None, first=None, body=w.packet(b"d" * 8)):
            ia = 17 + len(body) if index_at is None else index_at
            head = w.packet(ia.to_bytes(6, "little")) if first is None else first
            ix = w.varint(1) + w.varint(len(body) << 4) if idx is None else idx
            return r.MAGIC + head + body + w.packet(ix)

        bad = {
            "zero length": r.MAGIC + b"\0\0\0" + bytes(4) + bytes(30),
            "first packet": build(first=w.packet(bytes(5))),
            "outside": build(index_at=10 ** 6),
            "count": build(idx=w.varint(0)),
            "not used up": build(idx=w.varint(1) + w.varint(15 << 4) + w.varint(3)),
            "type": build(idx=w.varint(1) + w.varint(15 << 4 | 6)),
            "do not add up": build(idx=w.varint(1) + w.varint(16 << 4)),
            "ends early": build(idx=w.varint(2) + w.varint(15 << 4)),
        }
        for what, data in bad.items():
            with self.assertRaises(RolzError, msg=what):
                r.Container(Bytes(data), len(data))
        # a CRC flip in the first packet
        data = bytearray(build())
        data[8] ^= 0x10
        with self.assertRaisesRegex(RolzError, "CRC"):
            r.Container(Bytes(bytes(data)), len(data))
        # index packets: all but the last must hold 4096 bytes
        chunks = [(0, w.packet(bytes(8)))] * 2000
        data = w.container(chunks, index_packet=1000)
        with self.assertRaisesRegex(RolzError, "unexpected size"):
            r.Container(Bytes(data), len(data))


class Tokens(unittest.TestCase):
    """Token by token, with outputs worked out by hand."""

    def test_literals(self):
        got, lz, cd = lz_stream(w.literals(b"Hello"), 5)
        self.assertEqual(got, b"Hello")
        self.assertTrue(cd.used_up())
        self.assertEqual(lz.state, 0)

    def test_match_and_rep(self):
        # "abcd", then a match: bucket 0, low 3, mid 0 -> distance 1 + 3 = 4; length symbol 2
        # -> 2 + base 2 = 4; then a rep (symbol 0: rep0 = 4) of length 1 + base 1 = 2
        values = w.literals(b"abcd") + KIND["match"] + [0, 3, 0, 2] + KIND["rep"] + [0, 1]
        got, lz, _ = lz_stream(values, 10)
        self.assertEqual(got, b"abcdabcdab")
        self.assertEqual(lz.reps, [4, 1, 2, 3])
        self.assertEqual(lz.state, 10)  # literals keep 0, the match gives 9, a rep after 7: 10

    def test_overlap_and_rep_delta(self):
        # "ab", a match of distance 1 (low 0) and length 2 + 6 = 8: eight more "b"s; then a
        # rep symbol 5 (rep0 + 1 = 2, pushed) of length 1 + 2 = 3: "bbb"
        values = w.literals(b"ab") + KIND["match"] + [0, 0, 0, 6] + KIND["rep"] + [5, 2]
        got, lz, _ = lz_stream(values, 13)
        self.assertEqual(got, b"ab" + b"b" * 8 + b"bbb")
        self.assertEqual(lz.reps, [2, 1, 1, 2])

    def test_wide_distance(self):
        # bucket 5 (13 bits after the low nibble: mid symbol 0, raw(5) = 1) -> distance
        # TDB[5] + 1 + (lo4 | mid << 4) = 7936 + 1 + (2 | 1 << 4) = 7955
        data = bytes(range(256)) * 32
        values = w.literals(data) + KIND["match"] + [5, 2, 0, 1, 0]
        got, _, _ = lz_stream(values, len(data) + 2)
        pos = len(data)
        self.assertEqual(got[pos:], data[pos - 7955:pos - 7955 + 2])

    def test_specials(self):
        cases = {
            # selector: (script values after the selector, expected bytes)
            0: ([1] * 8, bytes(range(1, 9))),  # +1 each from the byte before
            1: ([1] * 8, bytes([1, 1, 2, 2, 3, 3, 4, 4])),
            2: ([1] * 9, bytes([1, 1, 1, 2, 2, 2, 3, 3, 3])),
            3: ([1] * 8, bytes([1, 1, 1, 1, 2, 2, 2, 2])),
            4: ([1] * 4, b"".join(v.to_bytes(2, "little") for v in (1, 2, 3, 4))),
            5: ([1] * 4, b"".join(v.to_bytes(2, "little") for v in (1, 1, 3, 3))),
            6: ([15, 16, 0], bytes(4) + (1).to_bytes(4, "little")),  # delta 0, then +1
            7: ([9, 8, 8] * 3, bytes([1, 1, 1, 2, 2, 2, 3, 3, 3])),
            8: ([9, 8, 8, 1] * 2, bytes([1, 1, 1, 1, 2, 2, 2, 2])),
            10: ([0] * 32, b"\x80" * 32),
            11: ([0] * 32, b"\x80" * 32),
            12: ([0] * 16, bytes(32)),
            13: ([0] * 16, bytes(32)),
            14: ([3, 4] * 32, b"\x34" * 32),
            15: (list(range(16)), b"".join(v.to_bytes(2, "little") for v in range(16))),
        }
        for sel, (tail, want) in cases.items():
            got, lz, cd = lz_stream(KIND["special"] + [sel] + tail, len(want))
            self.assertEqual(got, want, sel)
            self.assertEqual(lz.state, 13)
            self.assertTrue(cd.used_up())

    def test_special_byte_down(self):
        # special 0 with symbol 15 (base 255): -1 each, through the wrap
        got, _, _ = lz_stream(KIND["special"] + [0] + [15] * 8, 8)
        self.assertEqual(got, bytes([255, 254, 253, 252, 251, 250, 249, 248]))

    def test_special_9(self):
        # a width read with the flag bit; then 4 pixels of residual 0 at prediction 0
        values = KIND["special"] + [9, 1, 6] + [0] * 12
        got, lz, _ = lz_stream(values, 12)
        self.assertEqual(got, bytes(12))
        self.assertEqual(lz.s9w, 6)

    def test_special_9_without_width_refused(self):
        sc = w.Script(KIND["special"] + [9, 0])
        lz = r.LZ(sc, r.SMALL_WINDOW, False)
        with self.assertRaisesRegex(RolzError, "not yet written"):
            lz.take(12)

    def test_special_14_warm_up(self):
        # a literal 0x37 first: the second block's models were trained on it (warm-up), so
        # their CDFs differ from a fresh decoder's; the decoded bytes are still the symbols
        values = w.literals(b"\x37") + KIND["special"] + [14] + [3, 7] * 32
        got, lz, _ = lz_stream(values, 33)
        self.assertEqual(got, b"\x37" * 33)
        self.assertEqual(lz.last_end[14], 33)
        fresh = r.uniform(16)
        self.assertNotEqual(lz.s14[0], fresh)

    def test_rolz(self):
        # "xyzw", a match of 4 at distance 4 (entries: under "w" {4, 4}, under "x" {5, 3}),
        # then a reduced-offset match under "w": index 0 -> the newest entry, distance 4;
        # its length context is 2 (index 0, length 4), symbol 10 keeps the length: 4
        values = w.literals(b"xyzw") + KIND["match"] + [0, 3, 0, 2] + KIND["rolz"] + [0, 10]
        got, lz, cd = lz_stream(values, 12, window=1 << 16, rolz=True)
        self.assertEqual(got, b"xyzw" * 3)
        self.assertTrue(cd.used_up())
        t = lz.rolz
        self.assertEqual(t.count[ord("w")], 2)  # the match's entry and the new one
        self.assertEqual(t.count[ord("x")], 1)
        first = t.slot(ord("w"), 1)
        self.assertEqual((t.pos[first], t.len[first], t.last[first]), (4, 4, 5))

    def test_rolz_pages_recycle(self):
        t = r._Rolz(0)
        self.assertEqual(t.npages, 0x201)
        for i in range(512 * 300):
            t.add(7, i, i)
        # 300 pages filled for context 7; only 0x201 - 0x100 are free, so old ones come back
        self.assertEqual(t.count[7], 512 * 300)
        newest = t.slot(7, 0)
        self.assertEqual(t.pos[newest], 512 * 300 - 1)
        self.assertEqual(t.pos[t.slot(7, 511)], 512 * 299)
        # the first page (page 7) went back to the free list and was taken again for the
        # entries from 258 * 512 on, so the oldest slot now holds one of those
        self.assertEqual(t.pos[t.slot(7, 512 * 300 - 1)], 258 * 512)
        with self.assertRaisesRegex(RolzError, "past its list"):
            t.slot(8, 0)

    def test_refused_tokens(self):
        cases = [
            (KIND["rolz"] + [0, 10], False, "has none"),
            (w.literals(b"a") + KIND["rolz"] + [0], True, "past its list"),
            # bucket 4, low 15, mid 15 + raw(4) 15: 3841 + 4095 > the 4080 bytes of history
            (KIND["match"] + [4, 15, 15, 15], False, "before the history"),
            (KIND["rep"] + [4], False, "before the history"),  # rep0 1 - 1 = 0
        ]
        for values, rolz, msg in cases:
            sc = w.Script(values)
            lz = r.LZ(sc, 1 << 16, rolz)
            with self.assertRaisesRegex(RolzError, msg):
                lz.take(64)

    def test_distance_over_window(self):
        data = bytes(5000)
        values = w.literals(data) + KIND["match"] + [4, 15, 15, 15]  # distance 7936
        sc = w.Script(values)
        lz = r.LZ(sc, 4096, False)
        with self.assertRaisesRegex(RolzError, "before the history"):
            lz.take(len(data) + 1)

    def test_small_window_drops_old_output(self):
        data = bytes(range(256)) * 12
        sc = w.Script(w.literals(data))
        lz = r.LZ(sc, 256, False)
        r_slack = r.TRIM_SLACK
        try:
            r.TRIM_SLACK = 64
            for i in range(0, len(data), 100):
                self.assertEqual(lz.take(min(100, len(data) - i)), data[i:i + 100])
        finally:
            r.TRIM_SLACK = r_slack
        self.assertGreater(lz.drop, 0)
        self.assertLessEqual(len(lz.buf), 256 + r.HIST + 64 + 100)


class FakeLZ:
    def __init__(self, data: bytes):
        self.data, self.at = data, 0

    def take(self, n):
        assert self.at + n <= len(self.data)
        out = self.data[self.at:self.at + n]
        self.at += n
        return out


def tables(*vals) -> bytes:
    return b"".join(v.to_bytes(4, "little") for v in vals)


class Calls(unittest.TestCase):
    def run_request(self, flags, main, tab, n, base=0):
        sc = w.Script(flags)
        calls = r.X86Calls(sc, FakeLZ(tab), FakeLZ(main))
        calls.base = base
        out = calls.request(n)
        self.assertTrue(sc.done())
        return out, calls

    def test_passed_on(self):
        out, calls = self.run_request([0], b"\xe8" * 16, b"", 16)
        self.assertEqual(out, b"\xe8" * 16)
        self.assertEqual(calls.base, 16)  # every request counts

    def test_call(self):
        # counts: table0 1 (raw(5) = 1, raw(1) = 0), table1 0, table2 0; one E8 with bit 1:
        # target 0x1000 - (j 3 + base 0) = 0xFFD
        main = b"\x90\x90\xe8" + b"\x90" * 9
        out, calls = self.run_request([1, 1, 0, 0, 0, 1], main, tables(0x1000), 16)
        self.assertEqual(out, b"\x90\x90\xe8\xfd\x0f\x00\x00" + b"\x90" * 9)
        self.assertEqual(calls.base, 16)
        # a target below the call: sign-extended from 24 bits
        out, _ = self.run_request([1, 1, 0, 0, 0, 1], main, tables(0), 16)
        self.assertEqual(out[3:7], b"\xfd\xff\xff\xff")
        # base carried from earlier requests: 0x1000 - (3 + 100)
        out, _ = self.run_request([1, 1, 0, 0, 0, 1], main, tables(0x1000), 16, base=100)
        self.assertEqual(out[3:7], (0x1000 - 103).to_bytes(4, "little"))

    def test_tables_in_order(self):
        # counts 1, 2, 1; the table LZ gives table2, then table1, then table0. Each target is
        # its own j, so every written operand is 0 and no written byte starts a pattern:
        # E8 at 2 (table0, j 3), E9 at 7 (table1, j 8), 0F 85 at 12-13 (table1, j 14),
        # 48 80 05 at 18-20 (table2, j 21), then 3 raw bytes
        main = b"\x90\x90\xe8\xe9\x0f\x85\x48\x80\x05\x90\x91\x92"
        flags = [1, 1, 0, 1, 1, 1, 0] + [1, 1, 1, 1]
        out, _ = self.run_request(flags, main, tables(21, 8, 14, 3), 28)
        z = bytes(4)
        self.assertEqual(out, b"\x90\x90\xe8" + z + b"\xe9" + z + b"\x0f\x85" + z
                         + b"\x48\x80\x05" + z + b"\x90\x91\x92")

    def test_refused(self):
        main = b"\x90\x90\xe8" + b"\x90" * 9
        # table0 holds 2 entries but only one is used
        with self.assertRaisesRegex(RolzError, "used up exactly"):
            self.run_request([1, 1, 1, 0, 0, 1], b"\x90\x90\xe8" + b"\x90" * 5,
                             tables(1, 2), 16)
        # a rewrite with an empty table
        with self.assertRaisesRegex(RolzError, "past its end"):
            self.run_request([1, 0, 0, 0, 1], main + b"\x90" * 4, b"", 16)
        # tables larger than the request
        with self.assertRaisesRegex(RolzError, "larger than"):
            self.run_request([1, 5, 0, 0, 0], b"", b"", 16)

    def test_header_resets_base(self):
        # "MZ", u16 0 at j-8, u16 0 at j-2 for j = 10: base = ~9; the E8 at 10 then gets
        # 0x100 - (11 + ~9) = 0xFF
        main = b"MZ" + bytes(8) + b"\xe8" + bytes(9)
        out, calls = self.run_request([1, 1, 0, 0, 0, 1], main, tables(0x100), 24)
        self.assertEqual(out[11:15], (0xFF).to_bytes(4, "little"))
        self.assertEqual(calls.base, ~9 + 24)


def _record(new, stage1_tail, total, window, crc, file_bytes=None):
    d = w.directory(window, total, crc if crc is not None else zlib.crc32(file_bytes or b""))
    dops, _ = w.lz_ops(w.literals(d), len(d))
    requests = -(-len(new) // r.REQUEST)
    fl = w.Script([0] * requests)
    m = [128]
    for _ in range(requests):
        fl.model_bit(m, 0)
    s1 = w.Script(stage1_tail)
    g0, g1, g2 = [128] * 64, [128] * 64, [128] * 64
    count = s1.gamma(g0)
    s1.gamma(g0)
    for _ in range(count):
        s1.gamma(g0)
        s1.gamma(g1)
        s1.gamma(g2)
    assert s1.done()
    mops, _ = w.lz_ops(w.literals(new), len(new), window, True)
    chunks = [(4, w.packet(w.encode(mops)))] if mops else []
    chunks += [(3, w.packet(w.encode(fl.ops)))] if fl.ops else []
    chunks += [(1, w.packet(w.encode(s1.ops))), (0, w.packet(w.encode(dops)))]
    return w.record(w.container(chunks))


class Records(unittest.TestCase):
    def test_files(self):
        for data in (b"", b"one byte?", os.urandom(70_000), bytes(140_000)):
            rec = w.stored_file(data)
            self.assertEqual(decode(rec), data)
            self.assertEqual(decode(rec, chunk=777), data)

    def test_two_records(self):
        a, b = os.urandom(3000), b"second"
        self.assertEqual(decode(w.stored_file(a) + w.stored_file(b)), a + b)

    def test_copy(self):
        # 512 new bytes, then one copy at 512 of everything so far: s = 8, src = (2 - 2) << 8,
        # length (1 + 1) << 8
        new = os.urandom(512)
        file = new + new
        tail = w.gamma(1) + w.gamma(512) + w.gamma(512) + w.gamma(2) + w.gamma(1)
        rec = _record(new, tail, 1024, 1024, None, file)
        self.assertEqual(decode(rec), file)
        # an overlapping copy: src 256, length 512 from position 512 (distance 256)
        tail = w.gamma(1) + w.gamma(512) + w.gamma(512) + w.gamma(1) + w.gamma(1)
        file = new + new[256:] + new[256:]
        rec = _record(new, tail, 1024, 1024, None, file)
        self.assertEqual(decode(rec), file)

    def test_copy_refused(self):
        new = os.urandom(512)
        g = w.gamma
        bad = {  # name: (new bytes, stage1 values, file size, message)
            "src": (new, g(1) + g(512) + g(512) + g(3) + g(1), 1024, "outside the file"),
            "long": (new, g(1) + g(512) + g(512) + g(2) + g(3), 1024, "outside the file"),
            "past": (new, g(1) + g(512) + g(2000) + g(2) + g(0), 1024, "past the end"),
            "short": (new, g(0) + g(512), 1024, "more new bytes than there are"),
            "left": (os.urandom(768), g(1) + g(768) + g(512) + g(2) + g(1), 1024,
                     "more new bytes than the file holds"),
            "counts": (new, g(0) + g(512), 100, "larger than the file"),
        }
        for what, (data, tail, total, msg) in bad.items():
            rec = _record(data, tail, total, 1024, 0)
            with self.assertRaisesRegex(RolzError, msg, msg=what):
                decode(rec)

    def test_refused(self):
        data = os.urandom(5000)
        rec = w.stored_file(data)
        with self.assertRaisesRegex(RolzError, "CRC does not match"):
            decode(_record(data, w.gamma(0) + w.gamma(5000), 5000, 5120, 1))
        with self.assertRaisesRegex(RolzError, "5,242,880 bytes, over the 4,194,304 planned"):
            decode(w.stored_file(data, window=5 << 20), budget=4 << 20)
        with self.assertRaisesRegex(RolzError, "record header is cut off"):
            decode(rec + b"\0")
        with self.assertRaisesRegex(RolzError, "record length"):
            decode(rec[:-1])
        with self.assertRaisesRegex(RolzError, "record length"):
            decode((4).to_bytes(4, "little") + rec)
        with self.assertRaisesRegex(RolzError, "block's files"):
            decode(rec, limit=4999)
        self.assertEqual(decode(rec, limit=5000), data)

    def test_directory_refused(self):
        def dir_record(d):
            dops, _ = w.lz_ops(w.literals(d), len(d))
            s1 = w.Script(w.gamma(0) + w.gamma(0))
            g = [128] * 64
            s1.gamma(g)
            s1.gamma(g)
            return w.record(w.container([(1, w.packet(w.encode(s1.ops))),
                                         (0, w.packet(w.encode(dops)))]))

        cases = {
            "exactly one file": w.directory(1024, 5, 0, names=(b"a\0", b"b\0"),
                                            attrs=(0, 0), sizes=[1, 2]),
            "file count": w.directory(1024, 0, 0, names=(), attrs=(), sizes=[]),
            "larger than its window": w.directory(1024, 2000, 0),
        }
        for msg, d in cases.items():
            with self.assertRaisesRegex(RolzError, msg):
                decode(dir_record(d))
        # a directory with a size is refused too
        d = w.directory(1024, 5, 0, sizes=[3, 5])
        with self.assertRaisesRegex(RolzError, "exactly one file"):
            decode(dir_record(d))
        # a directory stream with one byte too many
        d = w.directory(1024, 0, 0, sizes=[0, 0])
        dops, _ = w.lz_ops(w.literals(d + b"x"), len(d) + 1)
        s1 = w.Script(w.gamma(0) + w.gamma(0))
        g = [128] * 64
        s1.gamma(g)
        s1.gamma(g)
        rec = w.record(w.container([(1, w.packet(w.encode(s1.ops))),
                                    (0, w.packet(w.encode(dops)))]))
        with self.assertRaisesRegex(RolzError, "not used up"):
            decode(rec)

    def test_flips_refused(self):
        rec = w.stored_file(os.urandom(2000))
        rng = random.Random(11)
        for at in rng.sample(range(len(rec)), 60) + [0, 3, 4, len(rec) - 1]:
            bad = bytearray(rec)
            bad[at] ^= 1 << rng.randrange(8)
            with self.assertRaises(RolzError, msg=at):
                decode(bytes(bad))


class Methods(unittest.TestCase):
    def test_parse(self):
        m = farc.parse_method(NAME)
        self.assertTrue(m.supported)
        self.assertEqual(m.kind, "rolz")
        for text, kind in ((f"srep:m3zf+dispack070+delta+{NAME}", "srep+x86+delta+rolz"),
                           (f"exe+{NAME}", "exe+rolz"), (f"lzma:1mb+{NAME}", "lzma+rolz")):
            m = farc.parse_method(text)
            self.assertTrue(m.supported, text)
            self.assertEqual(m.kind, kind)
        for text in (f"{NAME}:x", f"{NAME}:1023", f"{NAME}+lzma", f"{NAME}+{NAME}",
                     f"x86+{NAME}+delta", NAME.upper()):
            m = farc.parse_method(text)
            self.assertFalse(m.supported, text)
            self.assertNotIn(NAME, m.reason)
            self.assertNotIn(NAME, m.kind)

    def test_plan(self):
        m = farc.parse_method(f"srep+delta+{NAME}")
        p = fc.stage_plan(m, 10 ** 9)[-1]
        self.assertEqual((p.budget, p.memory), (r.WINDOW_CAP, r.memory_for(r.WINDOW_CAP)))
        p = fc.stage_plan(farc.parse_method(NAME), 10_000)[-1]
        self.assertEqual(p.budget, 10_240)
        p = fc.stage_plan(farc.parse_method(f"exe+{NAME}"), 1 << 40)[-1]
        self.assertEqual(p.budget, r.WINDOW_CAP)
        # the sample's window: about 2.6 times the window, plus fixed parts
        self.assertLess(r.memory_for(153_754_624), 520 << 20)

    def test_chain_from_file(self):
        data = bytes(b % 0xE0 for b in os.urandom(100_000))  # no E8/E9: exe keeps it
        rec = w.stored_file(data)

        def run(path_bytes, text, size=len(data), stop=lambda: None):
            path.write_bytes(path_bytes)
            fd = os.open(path, os.O_RDONLY)
            try:
                top = fc.open_chain(farc.parse_method(text), fd, 3, len(path_bytes) - 3, size,
                                    1 << 16, stop)
                out = bytearray()
                while piece := top.read(1 << 16):
                    out += piece
                return bytes(out)
            finally:
                os.close(fd)

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "b.bin"
            self.assertEqual(run(b"pad" + rec, NAME), data)
            self.assertEqual(run(b"pad" + rec, f"exe+{NAME}"), data)
            # the plan's window is the files' size: a larger window is refused
            with self.assertRaisesRegex(fc.CodecError, "rolz stage: .*planned"):
                run(b"pad" + w.stored_file(data, window=1 << 20), NAME)
            # damage comes back as CodecError with gitfurl's own text
            bad = bytearray(rec)
            bad[40] ^= 4
            with self.assertRaisesRegex(fc.CodecError, "rolz stage: damaged"):
                run(b"pad" + bytes(bad), NAME)
            # Stop is seen inside the block
            calls = []

            class Stopped(Exception):
                pass

            def stop():
                calls.append(1)
                if len(calls) > 3:
                    raise Stopped
            with self.assertRaises(Stopped):
                run(b"pad" + w.stored_file(bytes(300_000)), NAME, 300_000, stop)

    def test_extract(self):
        files = [F("game.dat", os.urandom(40_000)), F("readme.txt", b"hello " * 100)]
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            src = base / "src"
            src.mkdir()
            setup = src / "setup.exe"
            setup.write_bytes(b"MZ" + b"\0" * 100)
            prefix = base / "data" / "gitfurl" / "prefixes" / "p1"
            (prefix / "drive_c").mkdir(parents=True)
            with mock.patch.dict(ENCODERS, {NAME: lambda data, method: w.stored_file(data)}):
                (src / "a.arc").write_bytes(make_archive([(NAME, files)]))
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(base / "data"),
                                                "HOME": str(base)}):
                plan = extract.plan_extract(setup, "p1")
                text = "\n".join(plan.describe())
                self.assertNotIn(NAME, text)
                summary = "\n".join(farc.summarise(plan.archives[0].archive))
                self.assertIn("rolz", summary)
                self.assertNotIn(NAME, summary)
                res = extract.extract(plan)
            self.assertTrue(res.ok, res.lines())
            dest = prefix / "drive_c" / "Games" / "p1"
            for f in files:
                self.assertEqual((dest / f[1]).read_bytes(), f[2])


if __name__ == "__main__":
    unittest.main()
