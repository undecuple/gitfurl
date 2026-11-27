"""FreeArc's x86 code filter (method text dispack070 or dispack; "x86" in gitfurl's own text).

Synthetic tests: hand-made instructions with their streams laid out by hand from the
specification, the test-only encoder in tests/farc_writer.py (which mirrors the decoder and uses
its tables), framing, damage, caps, method strings and chains through a file and an archive.

The specification's vectors (made with FreeArc's own encoder; not kept in the repository) are
read only when GITFURL_X86_VECTORS names their folder (the one holding v/ and bad/); otherwise
those tests are skipped.
"""

import hashlib
import lzma
import os
import random
import tempfile
import tracemalloc
import unittest
from pathlib import Path
from unittest import mock

try:
    from farc_writer import F, make_archive, pack, x86_block, x86_encode
except ImportError:  # python -m unittest tests.test_farc_x86
    from tests.farc_writer import F, make_archive, pack, x86_block, x86_encode

from gitfurl import extract, farc
from gitfurl import farc_codecs as fc
from gitfurl.farc_codecs import CodecError

BASE = fc.X86_BASE
VECTORS = os.environ.get("GITFURL_X86_VECTORS")


class Mem:
    """An in-memory stage input that hands out at most `step` bytes per read."""

    def __init__(self, data: bytes, step: int = 1 << 20):
        self.data, self.at, self.step = data, 0, step

    def read(self, n: int) -> bytes:
        out = self.data[self.at:self.at + min(n, self.step)]
        self.at += len(out)
        return out


def stage(data: bytes, block: int = 8 << 20, cap: int | None = None, chunk: int = 1 << 20,
          step: int = 1 << 20, limit: int | None = None, stop=None):
    return fc.X86Stage(Mem(data, step), block, block if cap is None else cap, chunk, limit,
                       stop)


def drain(st, n: int = 1 << 20) -> bytes:
    out = bytearray()
    while True:
        piece = st.read(n)
        if not piece:
            return bytes(out)
        assert len(piece) <= n
        out += piece


def decode(data: bytes, block: int = 8 << 20, **kw) -> bytes:
    return drain(stage(data, block, **kw))


def u32(v: int) -> bytes:
    return v.to_bytes(4, "little")


def streams(items: dict) -> list:
    """19 streams from {index: hex text}."""
    return [bytearray.fromhex(items.get(i, "")) for i in range(fc.X86_STREAMS)]


def frame(sts: list, dlen: int) -> bytes:
    """One code block frame from its streams."""
    body = b"".join(u32(len(s)) for s in sts) + b"".join(sts)
    return u32(fc.X86_EXE) + u32(dlen) + u32(len(body)) + body


def unfilter(items: dict, dlen: int, base: int = BASE) -> bytes:
    return bytes(fc.x86_unfilter(streams(items), dlen, base))


def codeish(n: int, seed: int = 1) -> bytes:
    """Bytes that parse as many kinds of instruction (and some that do not)."""
    r = random.Random(seed)
    pool = list(range(256)) + [0xE8, 0xE9, 0x0F, 0x66, 0x8B, 0x89, 0x83, 0xC3, 0x55, 0x74] * 6
    return bytes(r.choice(pool) for _ in range(n))


# The specification's stream layout, by hand: 85 bytes of instructions at BASE.
HAND_IN = bytes.fromhex(
    "55" "8b4308" "e8f7ffffff" "e8f20f0000" "e8edffffff" "66b83412" "8b0424" "8b0578563412"
    "c7442404efbeadde" "0f84d2000000" "ebfe" "ce" "c3" "cc" "0000004033000040"
    "f7c078563412" "e8ba0f0000" "9a112233445566" "8b8044332211" "6a7f")
HAND = {
    0: "55 8b43 e8 e8 e8 66b8 8b04 8b05 c744 0f84 eb f1ce c3 cc ce01 f7c0 e8 9a 8b80 6a",
    1: "24 24",  # SIB of 8b 04 and c7 44
    2: "01 00 02 01 00 04",  # call targets: FT[0], new, FT[1], table: FT[0], new; FT[3]
    6: "08",  # 8-bit displacement, rm 3
    7: "04",  # ... rm 4 (after a SIB)
    11: "fe",
    12: "7f",
    13: "1234 2211",  # mov ax (after 0x66), then the extra 16 bits of 9a
    14: "deadbeef 12345678",
    15: "11223344",
    16: "12345678",  # mod 0 rm 5
    17: "40001000 40000033",
    18: "40000100 66554433",  # 0f 84's absolute target; 9a's 32 bits as they are
}


class Blocks(unittest.TestCase):
    def test_hand_made_instructions(self):
        self.assertEqual(unfilter(HAND, len(HAND_IN)), HAND_IN)

    def test_targets_that_wrap_and_a_target_of_zero(self):
        """Spec 6.15: a call past 2^32, then a call to 0, which is a zero entry of the
        function table (position 1, after the block's start was pushed)."""
        items = {0: "e8 e8", 2: "00 03", 17: "00000010"}
        self.assertEqual(unfilter(items, 10, 0xFFFFFFF0).hex(), "e81b000000" "e806000000")

    def test_index_255_after_255_targets(self):
        """Spec 6.17: the oldest of 255 new targets is at position 254 (index byte 255)."""
        calls = b""
        for k in range(256):
            at = 5 * k
            target = BASE + 0x100000 + (0 if k == 255 else 16 * k)
            calls += b"\xe8" + ((target - (BASE + at + 5)) & 0xFFFFFFFF).to_bytes(4, "little")
        blk = x86_block(calls, BASE, tables=False)
        sts = fc.x86_split(blk)
        self.assertEqual(sts[2][-1], 255)
        self.assertEqual(len(sts[17]), 4 * 255)
        self.assertEqual(bytes(fc.x86_unfilter(sts, len(calls), BASE)), calls)

    def test_jump_tables_and_prefixes(self):
        table = b"".join(u32(BASE + 4 * k) for k in range(300))  # groups of 256 and 44
        data = table + bytes.fromhex("6666 90 66e8 00000000 660f1f00 c3 cc cc 55 c2 0800")
        blk = x86_block(data, BASE)
        sts = fc.x86_split(blk)
        self.assertEqual(bytes(sts[0][:2]), b"\xce\xff")
        self.assertEqual(bytes(sts[0][2:4]), b"\xce\x2b")
        self.assertEqual(bytes(fc.x86_unfilter(sts, len(data), BASE)), data)

    def test_invalid_opcodes_are_refused(self):
        """Spec 6.16: an opcode whose kind is E, reached by the decoder."""
        for op in ("0f00c0", "0f0b", "0fff", "f6c8", "f7c8", "fed0", "ffd8", "ffff", "66ce",
                   "66f1", "660f05"):
            with self.subTest(op=op), self.assertRaisesRegex(CodecError, "never writes"):
                unfilter({0: op}, 8)

    def test_damaged_blocks(self):
        n = len(HAND_IN)
        cases = {
            "do not match": [{**HAND, 12: "7f7f"}, {**HAND, 17: HAND[17] + "00000000"},
                             {**HAND, 0: HAND[0][:-2]}],
            "ends early": [{**HAND, 12: ""}, {**HAND, 2: "01 00 02"}, {**HAND, 0: "ce"}],
            "whole items": [{**HAND, 14: "deadbeef 123456"}, {**HAND, 13: "12"}],
        }
        for words, variants in cases.items():
            for items in variants:
                with self.subTest(words=words), self.assertRaisesRegex(CodecError, words):
                    unfilter(items, n)
        with self.assertRaisesRegex(CodecError, "less than its size"):
            unfilter(HAND, n + 1)
        with self.assertRaisesRegex(CodecError, "more than its size"):
            unfilter(HAND, n - 1)

    def test_output_far_past_the_size_is_seen_within_a_round(self):
        items = {0: "ceff" * 4000, 2: "01" * 256 * 4000}
        with self.assertRaisesRegex(CodecError, "more than its size"), \
                mock.patch.object(fc, "X86_ROUND", 64):
            fc.x86_unfilter(streams(items), 100, BASE)

    def test_random_blocks_round_trip(self):
        r = random.Random(3)
        for t in range(150):
            data = codeish(r.randrange(0, 4000), t)
            base = r.choice([BASE, 0xBFFFFFF0, 0xFFFFFF00, 0])
            with self.subTest(t=t):
                sts = fc.x86_split(x86_block(data, base))
                self.assertEqual(bytes(fc.x86_unfilter(sts, len(data), base)), data)

    def test_random_streams_never_escape_as_another_error(self):
        r = random.Random(4)
        for t in range(1500):
            sts = [bytearray(r.randbytes(r.choice([0, 0, 4, 8, 16, r.randrange(64)])))
                   for _ in range(fc.X86_STREAMS)]
            sts[0] = bytearray(r.randbytes(r.randrange(1, 64)))
            dlen = r.randrange(0, 200)
            try:
                out = fc.x86_unfilter(sts, dlen, BASE)
            except CodecError:
                continue
            self.assertEqual(len(out), dlen)

    def test_size_table(self):
        blk = x86_block(HAND_IN, BASE)
        self.assertEqual(len(fc.x86_split(blk)), 19)
        for bad in (blk[:75], blk + b"\0", blk[:-1]):
            with self.assertRaises(CodecError):
                fc.x86_split(bad)


class Framing(unittest.TestCase):
    def test_empty_and_header_only(self):
        self.assertEqual(decode(b""), b"")
        self.assertEqual(decode(u32(16384)), b"")

    def test_raw_data_and_code_frames_round_trip(self):
        noise = b"NOIS" + random.Random(5).randbytes(16380)
        codes = (codeish(16384), codeish(16384, 2), codeish(16384, 3))
        data = b"".join((codes[0], codes[1], noise, u32(fc.X86_DATA) + noise[4:],
                         u32(fc.X86_TAGS + 5) + noise[4:], codes[2], b"tail"))
        enc = x86_encode(data, code=lambda p: p in codes)
        self.assertEqual(enc.count(noise[4:]), 3)  # raw, then two data frames
        for step, chunk, n in ((1 << 20, 1 << 20, 1 << 20), (7, 13, 5), (4096, 1000, 999)):
            with self.subTest(step=step, chunk=chunk):
                self.assertEqual(drain(stage(enc, step=step, chunk=chunk), n), data)

    def test_several_blocks_advance_the_address(self):
        data = codeish(150_000)
        enc = x86_encode(data, "dispack070:64kb")
        self.assertEqual(decode(enc, 64 << 10), data)
        with self.assertRaisesRegex(CodecError, "does not allow"):
            decode(x86_encode(data), 64 << 10)  # one block of 150,000 bytes

    def test_the_address_drops_by_2_gib_past_0xc0000000(self):
        """Spec 6.14: a stage that starts just below the wrap point."""
        zeros = bytes(16384)
        data = zeros + codeish(20_000)
        enc = x86_encode(data, code=lambda p: p != zeros, base=0xBFFFC000)
        st = stage(enc)
        st.base = 0xBFFFC000
        self.assertEqual(drain(st), data)
        self.assertEqual(st.base, BASE + 20_000)
        st = stage(enc)
        st.base = 0xBFFFC000
        with mock.patch.object(fc, "X86_WRAP", 1 << 40):
            self.assertNotEqual(drain(st), data)

    def test_damaged_frames(self):
        code = codeish(3000)
        good = x86_encode(code)
        blk = x86_block(code, BASE)
        cases = {
            "reserved tag": [u32(16384) + u32(t) for t in range(fc.X86_TAGS, fc.X86_TAGS + 16)
                             if t not in (fc.X86_DATA, fc.X86_EXE)],
            "chunk size": [u32(3), u32(0), u32((8 << 20) + 1), u32(0xFFFFFFFF)],
            "never writes": [u32(16384) + u32(fc.X86_DATA) + u32(0),
                             u32(100) + u32(fc.X86_DATA) + u32(101) + bytes(101)],
            "does not allow": [u32(16384) + u32(fc.X86_EXE) + u32((8 << 20) + 1) + u32(76),
                               u32(16384) + u32(fc.X86_EXE) + u32(5)
                               + u32(fc.x86_input_cap(8 << 20) + 1),
                               u32(16384) + u32(fc.X86_EXE) + u32(0) + u32(75) + bytes(75),
                               u32(16384) + u32(fc.X86_EXE) + u32(1 << 31) + u32(76)],
            "do not add up": [u32(16384) + u32(fc.X86_EXE) + u32(len(code)) + u32(len(blk) + 1)
                              + blk + b"\0"],
            "ended inside": [good[:2], good[:6], good[:10], good[:-1], good + b"\xe1\x3a",
                             u32(16384) + u32(fc.X86_DATA) + u32(10) + bytes(9),
                             u32(16384) + bytes(100)],
        }
        for words, variants in cases.items():
            for data in variants:
                with self.subTest(words=words, data=data[:12].hex()), \
                        self.assertRaisesRegex(CodecError, words):
                    decode(data)

    def test_every_cut_is_an_error_or_ends_at_a_frame(self):
        code, raw = codeish(16384), bytes(4) + os.urandom(16380)
        enc = x86_encode(code + raw + b"short", code=lambda p: p == code)
        first = 4 + 12 + len(x86_block(code, BASE))
        ends = {0: b"", 4: b"", first: code, first + 16384: code + raw}
        self.assertEqual(len(enc), first + 16384 + 8 + 5)
        cuts = set(range(0, len(enc), 61)) | set(range(len(enc) - 40, len(enc)))
        cuts |= {e + d for e in ends for d in range(-5, 6) if 0 <= e + d < len(enc)}
        for cut in sorted(cuts):
            try:
                out = decode(enc[:cut])
            except CodecError:
                self.assertNotIn(cut, ends)
                continue
            self.assertIn(cut, ends, cut)
            self.assertEqual(out, ends[cut])
        for cut in ends:
            self.assertEqual(decode(enc[:cut]), ends[cut])

    def test_single_byte_flips_never_escape_as_another_error(self):
        enc = bytearray(x86_encode(codeish(600) + b"\x00" * 10))
        for i in range(len(enc)):
            for x in (0x01, 0x80, 0xFF):
                enc[i] ^= x
                try:
                    decode(bytes(enc))
                except CodecError:
                    pass
                enc[i] ^= x

    def test_limit_stops_before_the_next_frame(self):
        data = codeish(100_000)
        enc = x86_encode(data, "dispack070:32kb")
        cut = enc[:len(enc) // 2]  # damaged from the middle of the second block on
        self.assertEqual(drain(stage(cut, 32 << 10, limit=30_000)), data[:32 << 10])
        with self.assertRaises(CodecError):
            drain(stage(cut, 32 << 10))

    def test_a_block_past_the_cap_is_refused_before_it_is_read(self):
        data = codeish(50_000)
        enc = x86_encode(data, "dispack070:64kb")
        with self.assertRaisesRegex(CodecError, "runs past the files"):
            drain(stage(enc, 64 << 10, cap=40_000))

    def test_stop_is_checked_inside_a_block(self):
        data = codeish(200_000)
        enc = x86_encode(data)
        calls = []

        def stop():
            calls.append(1)
            if len(calls) > 20:
                raise KeyboardInterrupt

        st = stage(enc, stop=stop)
        with self.assertRaises(KeyboardInterrupt), mock.patch.object(fc, "X86_ROUND", 512):
            st.read(1 << 20)
        self.assertEqual(st.total, 0)  # inside the first (and only) block


class Methods(unittest.TestCase):
    def test_parse(self):
        ok = {"dispack070": 8 << 20, "dispack": 8 << 20, "dispack070:x": 8 << 20,
              "dispack070:24": 16 << 20, "dispack070:64kb": 64 << 10, "dispack:b1mb": 1 << 20,
              "dispack070:x:b=2mb": 2 << 20, "dispack070:8mb:4mb": 4 << 20,
              "dispack070:1gb": 1 << 30, "dispack070:16384b": 16384, "dispack070:8mb:x": 8 << 20}
        for text, size in ok.items():
            with self.subTest(text=text):
                m = farc.parse_method(text)
                self.assertEqual(m.kind, "x86")
                self.assertEqual(m.stages[0].size, size)
                self.assertNotIn("dispack", repr(m))
        chains = {"dispack070+lzma:8mb": "x86+lzma", "exe+dispack070:x+lzma": "exe+x86+lzma",
                  "rep:1mb+dispack070+delta+lzma": "rep+x86+delta+lzma"}
        for text, kind in chains.items():
            self.assertEqual(farc.parse_method(text).kind, kind)
        bad = ["dispack070:", "dispack070:b", "dispack070:0b", "dispack070:2gb",
               "dispack070:31", "dispack070:8q", "dispack070:xx", "dispack070:y", "Dispack070",
               "dispack07", "dispack070x", "exe2", "x86", "dispack070:b8zz"]
        for text in bad:
            with self.subTest(text=text):
                m = farc.parse_method(text)
                self.assertFalse(m.supported)
                self.assertEqual(m.kind, farc.KIND_OTHER)
                self.assertNotIn(text, m.reason + repr(m))
                self.assertNotIn("dispack", m.reason)

    def test_memory_plan(self):
        p = fc.stage_plan(farc.parse_method("dispack070+lzma:8mb"), 1 << 30)
        enc = fc.x86_input_cap(8 << 20)
        self.assertEqual((p[0].budget, p[0].memory), (8 << 20, (8 << 20) + enc + enc // 2
                                                      + fc.X86_SLACK))
        self.assertIsNone(p[1].limit)  # x86 changes sizes
        small = fc.stage_plan(farc.parse_method("dispack070"), 1000)[0]
        self.assertEqual((small.budget, small.limit), (1000, 1000))
        e = fc.stage_plan(farc.parse_method("exe+dispack070"), 1000)[1]
        self.assertEqual(e.limit, 1000 + fc.EXE_SLACK)

    def test_peak_memory_stays_within_the_plan(self):
        data = codeish(1 << 20) * 2
        enc = x86_encode(data, "dispack070:2mb")
        method = farc.parse_method("dispack070:2mb")
        plan = fc.chain_memory(method, len(data), 1 << 16)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blk"
            path.write_bytes(enc)
            fd = os.open(path, os.O_RDONLY)
            try:
                tracemalloc.start()
                top = fc.open_chain(method, fd, 0, len(enc), len(data), 1 << 16, lambda: None)
                h = hashlib.sha256()
                while piece := top.read(1 << 16):
                    h.update(piece)
                peak = tracemalloc.get_traced_memory()[1]
            finally:
                tracemalloc.stop()
                os.close(fd)
        self.assertEqual(h.digest(), hashlib.sha256(data).digest())
        self.assertLess(peak, plan)
        self.assertGreater(peak, 0.4 * plan)  # the figure is not far above what is used


class Chains(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def decode(self, method: str, enc: bytes, size: int, chunk: int = 1 << 20) -> bytes:
        path = Path(self._tmp.name) / "blk"
        path.write_bytes(b"pad" + enc)
        fd = os.open(path, os.O_RDONLY)
        try:
            top = fc.open_chain(farc.parse_method(method), fd, 3, len(enc), size, chunk,
                                lambda: None)
            out = bytearray()
            while len(out) < size and (piece := top.read(min(chunk, size - len(out)))):
                out += piece
            return bytes(out)
        finally:
            os.close(fd)

    def test_chains_through_a_file(self):
        data = codeish(70_000) + os.urandom(20_000) + codeish(9_000, 7)
        for method in ("dispack070", "dispack070+lzma:1mb", "dispack070:64kb+lzma:1mb",
                       "exe+dispack070+lzma:1mb", "dispack070+rep:1mb+lzma:1mb",
                       "rep:1mb+dispack070+delta+lzma:1mb"):
            for chunk in (1 << 20, 777):
                with self.subTest(method=method, chunk=chunk):
                    enc = pack(method, data)
                    self.assertEqual(self.decode(method, enc, len(data), chunk), data)

    def test_extraction_of_an_x86_archive(self):
        base = Path(self._tmp.name)
        src = base / "src"
        src.mkdir()
        setup = src / "setup.exe"
        setup.write_bytes(b"MZ" + b"\0" * 100)
        prefix = base / "data" / "gitfurl" / "prefixes" / "p1"
        (prefix / "drive_c").mkdir(parents=True)
        files = [F("game.exe", codeish(120_000)), F("data.bin", os.urandom(30_000)),
                 F("empty", b"")]
        (src / "a.arc").write_bytes(make_archive([("dispack070+lzma:1mb", files),
                                                  ("exe+dispack070:x:64kb",
                                                   [F("tools.dll", codeish(50_000, 9))])]))
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(base / "data"),
                                            "HOME": str(base)}):
            plan = extract.plan_extract(setup, "p1")
            text = "\n".join(plan.describe())
            self.assertNotIn("dispack", text)
            self.assertIn("x86", text)
            summary = "\n".join(farc.summarise(plan.archives[0].archive))
            self.assertIn("1 x86+lzma, 1 exe+x86", summary)
            self.assertNotIn("dispack", summary)
            res = extract.extract(plan)
        self.assertTrue(res.ok, res.lines())
        dest = prefix / "drive_c" / "Games" / "p1"
        for f in files:
            self.assertEqual((dest / f[1]).read_bytes(), f[2])


# --- the specification's vectors (FreeArc's own encoder), only when they are given -------

IN_SHA256 = {
    "code32": "5c45e68eb4ae677b73bf2402b7d5710fdfaea1e84a1be4fb1d5818faba26acf6",
    "crafted": "bfc8a9b8d1167da106d6f5f3c657829254a2ccda205ce88ed5e18a95fb892503",
    "empty": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "mixed": "9ef8199d82581ca05911acf5720bccc2ba06d1de02afd06c08909eb7f83d295f",
    "random": "7926e7d2d18c0ee7cf421633201b173f725e0578c9dc79773c673eaf73e411e7",
    "tagcollide": "57bf6bc5ad12029079e303880e8d8eb2c7b1c761f91aab11f93eb8859b41d51c",
}
DP = {"code32": "code32", "code32_b64k": "code32", "crafted": "crafted", "empty": "empty",
      "mixed": "mixed", "mixed_read1000": "mixed", "random": "random",
      "tagcollide": "tagcollide"}
BAD = {  # file -> None (an error) or the number of bytes decoded, which must be a prefix
    "trunc_at_boundary": 65536, "trunc_in_tag": None, "trunc_mid_block": None,
    "unknown_tag": None, "chunk_header_too_big": None, "stream_sizes_mismatch": None,
    "op_byte_flipped": 176444, "op_byte_to_mov_imm32": None, "op_bytes_to_nops": None,
}


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(VECTORS, "GITFURL_X86_VECTORS is not set")
class SpecVectors(unittest.TestCase):
    def setUp(self):
        self.root = Path(VECTORS)

    def original(self, name: str) -> bytes:
        data = (self.root / "v" / f"{name}.in").read_bytes()
        self.assertEqual(sha(data), IN_SHA256[name])
        return data

    def test_framed_vectors(self):
        for name, orig in DP.items():
            with self.subTest(name=name):
                enc = (self.root / "v" / f"{name}.dp").read_bytes()
                want = self.original(orig)
                for step in (1 << 20, 1000):
                    self.assertEqual(sha(decode(enc, step=step, chunk=4096)), sha(want))
        b64k = (self.root / "v" / "code32_b64k.dp").read_bytes()
        self.assertEqual(sha(decode(b64k, 64 << 10)), IN_SHA256["code32"])
        with self.assertRaisesRegex(CodecError, "does not allow"):
            decode((self.root / "v" / "code32.dp").read_bytes(), 64 << 10)

    def test_bare_blocks(self):
        for name in ("crafted", "code32"):
            with self.subTest(name=name):
                raw = (self.root / "v" / f"{name}.raw").read_bytes()
                want = self.original(name)
                out = fc.x86_unfilter(fc.x86_split(raw), len(want), BASE)
                self.assertEqual(sha(out), IN_SHA256[name])
                with self.assertRaises(CodecError):  # spec 3.8: the stricter check
                    fc.x86_unfilter(fc.x86_split(raw), len(want) + 10, BASE)
                with self.assertRaises(CodecError):
                    fc.x86_unfilter(fc.x86_split(raw), len(want) - 1, BASE)
                # the test-only encoder gives FreeArc's own block, byte for byte
                self.assertEqual(x86_block(want, BASE), raw)

    def test_damaged_vectors(self):
        code32 = self.original("code32")
        for name, size in BAD.items():
            with self.subTest(name=name):
                enc = (self.root / "bad" / f"{name}.dp").read_bytes()
                if size is None:
                    with self.assertRaises(CodecError):
                        decode(enc)
                    continue
                out = decode(enc)
                self.assertEqual(len(out), size)
                if name == "op_byte_flipped":  # wrong data, which this stage cannot see
                    self.assertNotEqual(out, code32)
                    self.assertEqual(out[1:], code32[1:])
                else:
                    self.assertEqual(out, code32[:size])

    def test_through_a_chain(self):
        enc = (self.root / "v" / "code32_b64k.dp").read_bytes()
        want = self.original("code32")
        packed = lzma.compress(enc, format=lzma.FORMAT_RAW,
                               filters=[{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20}])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "blk"
            path.write_bytes(packed)
            fd = os.open(path, os.O_RDONLY)
            try:
                top = fc.open_chain(farc.parse_method("dispack070:64kb+lzma:1mb"), fd, 0,
                                    len(packed), len(want), 1 << 16, lambda: None)
                out = bytearray()
                while piece := top.read(1 << 16):
                    out += piece
            finally:
                os.close(fd)
        self.assertEqual(sha(out), IN_SHA256["code32"])


if __name__ == "__main__":
    unittest.main()
