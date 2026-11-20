"""Tests for the read-only FreeArc index reader.

Every archive here is built in memory by tests/farc_writer.py (storing, and raw LZMA1 from
Python's own lzma module) and written to a temporary folder only for the test. No archive
file is kept in the repository. A writer and a reader made from the same reading of the
format agree with each other; that alone does not show they agree with FreeArc itself.
"""

import lzma
import os
import random
import stat
import tempfile
import unicodedata
import unittest
import zlib
from pathlib import Path
from unittest import mock

from gitfurl import farc
from gitfurl.farc import KIND_LZMA, KIND_OTHER, KIND_STORING, FarcError

try:  # pytest puts tests/ on sys.path; `python -m unittest tests.test_farc` does not
    from farc_writer import SIG, F, cstr, local_descriptor, make_archive, sample_blocks, u32, varint
except ImportError:
    from tests.farc_writer import (
        SIG,
        F,
        cstr,
        local_descriptor,
        make_archive,
        sample_blocks,
        u32,
        varint,
    )


def repeated_dir_archive(copies: int, padding: int) -> bytes:
    """One lzma directory block (no files) with `padding` bytes counted in its compsize,
    listed `copies` times in the footer. Built for the tests only, in memory."""
    dplain = b"\0\0"  # no data blocks, no folders
    f = [{"id": lzma.FILTER_LZMA1, "dict_size": 1 << 20, "lc": 3, "lp": 0, "pb": 2}]
    dcomp = lzma.compress(dplain, format=lzma.FORMAT_RAW, filters=f) + b"\0" * padding
    header = SIG + b"\0\0\0\0"
    out = bytearray(header)
    dir_pos = len(out)
    out += dcomp
    fpos = len(out)
    fp = varint(1 + copies) + varint(1) + cstr("storing") + varint(fpos) + varint(8)
    fp += varint(8) + u32(zlib.crc32(header))
    one = varint(3) + cstr("lzma") + varint(fpos - dir_pos) + varint(len(dplain))
    one += varint(len(dcomp)) + u32(zlib.crc32(dplain))
    fp += one * copies + b"\0" + varint(0)
    return bytes(out + fp + local_descriptor(4, "storing", len(fp), len(fp), zlib.crc32(fp)))


# --- tests ------------------------------------------------------------------------------------
class FarcTestBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.n = 0

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, data: bytes, name: str | None = None) -> Path:
        self.n += 1
        p = self.dir / (name or f"t{self.n}.arc")
        p.write_bytes(data)
        return p

    def read(self, data: bytes) -> farc.Archive:
        return farc.read_archive(self.write(data))

    def refused(self, data: bytes, fragment: str = "") -> str:
        with self.assertRaises(FarcError) as cm:
            self.read(data)
        self.assertIn(fragment, str(cm.exception))
        return str(cm.exception)


class ValidArchives(FarcTestBase):
    def test_store(self):
        blocks, folders = sample_blocks("storing")
        arc = self.read(make_archive(blocks, folders))
        self.assertEqual(arc.sfx_size, 0)
        self.assertFalse(arc.locked)
        self.assertEqual(len(arc.entries), 4)
        self.assertEqual([e.path for e in arc.entries], [".", "a.bin", "sub/b.txt", "sub"])
        self.assertEqual(arc.total_size, 3000 + 650)
        self.assertEqual([b.method.kind for b in arc.blocks], [KIND_STORING])
        self.assertEqual(arc.blocks[0].origsize, arc.blocks[0].compsize)
        lines = farc.summarise(arc)
        self.assertEqual(lines[0], "2 files and 1 folder, 3.6 KiB in total")
        self.assertEqual(lines[1], "1 data block: 1 storing, 0 lzma")
        self.assertIn("locked: no", lines)
        self.assertFalse(any("self-extracting" in s or "safe" in s for s in lines))

    def test_lzma_data_and_index(self):
        blocks, folders = sample_blocks("lzma:1mb:normal:32")
        blocks.append(("lzma:2mb:lc0:lp2:pb0", [F("c.txt", b"abc" * 4000)]))
        arc = self.read(make_archive(blocks, folders, index="lzma:1mb", locked=True))
        self.assertEqual([b.method.kind for b in arc.blocks], [KIND_LZMA, KIND_LZMA])
        self.assertEqual(arc.blocks[1].method.lzma,
                         {"dict_size": 2 << 20, "lc": 0, "lp": 2, "pb": 0})
        self.assertLess(arc.blocks[1].compsize, arc.blocks[1].origsize)
        self.assertTrue(arc.locked)
        lines = farc.summarise(arc)
        self.assertEqual(lines[1], "2 data blocks: 0 storing, 2 lzma")
        self.assertIn("locked: yes", lines)

    def test_index_with_large_declared_dictionary_decodes(self):
        # The dictionary given for the index is capped to its output size before decoding.
        blocks, folders = sample_blocks()
        arc = self.read(make_archive(blocks, folders, index="lzma:1gb"))
        self.assertEqual(len(arc.entries), 4)

    def test_sfx_prefix(self):
        stub = b"MZ" + bytes(random.Random(1).getrandbits(8) for _ in range(5000))
        blocks, folders = sample_blocks()
        arc = self.read(make_archive(blocks, folders, sfx=stub))
        self.assertEqual(arc.sfx_size, len(stub))
        self.assertEqual(arc.blocks[0].offset, len(stub) + 8)
        self.assertTrue(any("after a 4.9 KiB program" in s for s in farc.summarise(arc)))

    def test_varint_wide_form(self):
        blocks, folders = sample_blocks()
        arc = self.read(make_archive(blocks, folders, wide=True))
        self.assertEqual(arc.total_size, 3650)
        b = farc._Buf(varint(5, wide=True) + varint(1 << 60) + varint(300) + varint(0)
                      + varint((1 << 56) - 1))
        self.assertEqual([b.varint() for _ in range(5)], [5, 1 << 60, 300, 0, (1 << 56) - 1])

    def test_varint_lengths(self):
        for v in (0, 1, 127, 128, 16383, 16384, (1 << 49) - 1, 1 << 49, (1 << 56) - 1):
            enc = varint(v)
            b = farc._Buf(enc)
            self.assertEqual(b.varint(), v)
            self.assertEqual(b.pos, len(enc))

    def test_footer_signature_inside_crc_is_skipped(self):
        # A later "ArC\x01" (here inside the method text) fails its CRC and must not hide
        # the real footer descriptor that starts before it.
        desc = local_descriptor(4, "zz" + SIG.decode("latin-1") + "zz", 10, 10, 0)
        tail = b"\x90" * 50 + desc
        self.assertGreater(tail.rfind(SIG), 50)
        self.assertEqual(farc._footer_descriptor(tail)[:2], (50, 4))

    def test_has_signature(self):
        blocks, folders = sample_blocks()
        self.assertTrue(farc.has_signature(self.write(make_archive(blocks, folders))))
        self.assertFalse(farc.has_signature(self.write(b"MZ" + b"\0" * 100)))
        self.assertFalse(farc.has_signature(self.dir / "missing.arc"))


class Methods(FarcTestBase):
    def test_parse_method_table(self):
        ok = {"storing": KIND_STORING, "lzma": KIND_LZMA, "lzma:1gb": KIND_LZMA,
              "lzma:d64m:lc4:lp0:pb4": KIND_LZMA, "lzma:4mb:normal:bt4:128": KIND_LZMA,
              "lzma:mfbt4:fb32:mc16:a1": KIND_LZMA, "lzma:*zz": KIND_LZMA, "lzma:31": KIND_LZMA}
        bad = ["lzma:1025mb", "lzma:2gb", "lzma:lc5", "lzma:lc3:lp2",
               "lzma:pb9", "lzma:zz", "lzma:", "storing:1", "foo+lzma", "lzma+storing",
               "zzzmethod:42", "", "LZMA", "lzma:lc\u0663", "lzma:1mb+aes-256/ctr:n1000"]
        for text, kind in ok.items():
            self.assertEqual(farc.parse_method(text).kind, kind, text)
        for text in bad:
            m = farc.parse_method(text)
            self.assertFalse(m.supported, text)
            self.assertEqual(m.kind, KIND_OTHER)
        self.assertTrue(farc.parse_method("lzma:1mb+aes-256/ctr:n1000").encrypted)
        self.assertEqual(farc.parse_method("lzma:1gb").lzma["dict_size"], 1 << 30)

    def test_unsupported_method_reported_generically(self):
        unknown_method = "zzzmethod:42:qq"
        blocks = [(unknown_method, [F("x.bin", b"x" * 100)]), ("storing", [F("y.bin", b"y" * 10)])]
        arc = self.read(make_archive(blocks))
        self.assertEqual([b.method.kind for b in arc.blocks], [KIND_OTHER, KIND_STORING])
        lines = farc.summarise(arc)
        self.assertIn(f"2 data blocks: 1 storing, 0 lzma, 1 {KIND_OTHER}", lines)
        text = "\n".join(lines) + repr(arc.blocks)
        self.assertNotIn("zzzmethod", text)
        self.assertNotIn("qq", text)

    def test_chained_and_encrypted_data_methods(self):
        blocks = [("foo+lzma:1mb", [F("a", b"a" * 50)]),
                  ("lzma:1mb+aes-256/ctr:n1000:r0:cdeadbeef", [F("b", b"b" * 50)])]
        arc = self.read(make_archive(blocks))
        self.assertEqual([b.method.kind for b in arc.blocks], [KIND_OTHER, KIND_OTHER])
        self.assertTrue(arc.blocks[1].method.encrypted)
        text = "\n".join(farc.summarise(arc)) + repr(arc.blocks)
        for word in ("foo", "aes", "deadbeef", "ctr"):
            self.assertNotIn(word, text)

    def test_dictionary_over_cap_in_data_block(self):
        arc = self.read(make_archive([("lzma:2gb", [F("a", b"a" * 50)])]))
        self.assertEqual(arc.blocks[0].method.kind, KIND_OTHER)


class Refusals(FarcTestBase):
    def test_encrypted_index(self):
        blocks, folders = sample_blocks()
        msg = self.refused(make_archive(blocks, folders, index="aes-256/ctr:n1000:cfeed"),
                           "encrypted")
        self.assertNotIn("feed", msg)
        msg = self.refused(make_archive(blocks, folders,
                                        dir_index="lzma:1mb+blowfish:cfeed"), "encrypted")
        self.assertNotIn("feed", msg)

    def test_index_with_other_method(self):
        blocks, folders = sample_blocks()
        msg = self.refused(make_archive(blocks, folders, dir_index="zzzmethod:7"), KIND_OTHER)
        self.assertNotIn("zzz", msg)
        self.refused(make_archive(blocks, folders, index="lzma:2gb"), KIND_OTHER)

    def test_bad_crcs(self):
        blocks, folders = sample_blocks()
        self.refused(make_archive(blocks, folders, bad_dir_crc=True), "CRC")
        self.refused(make_archive(blocks, folders, bad_desc_crc=True), "CRC")
        data = bytearray(make_archive(blocks, folders, index="lzma:1mb"))
        footer_desc = data.rfind(SIG)
        data[footer_desc - 3] ^= 0x55  # inside the packed footer block
        self.refused(bytes(data))

    def test_truncated(self):
        blocks, folders = sample_blocks("lzma:1mb")
        data = make_archive(blocks, folders, index="lzma:1mb")
        for cut in (0, 1, 4, 15, 16, 100, len(data) // 2, len(data) - 30, len(data) - 5,
                    len(data) - 1):
            with self.subTest(cut=cut):
                self.refused(data[:cut])
        self.refused(data[200:])  # front cut: the index no longer points at the start

    def test_no_footer_in_last_4096_bytes(self):
        blocks, folders = sample_blocks()
        data = make_archive(blocks, folders) + b"\0" * 5000
        self.refused(data, "no FreeArc footer")
        self.refused(b"\0" * 10000, "no FreeArc footer")

    def test_not_regular_files(self):
        blocks, folders = sample_blocks()
        real = self.write(make_archive(blocks, folders))
        link = self.dir / "link.arc"
        os.symlink(real, link)
        with self.assertRaises(FarcError):
            farc.read_archive(link)
        self.assertFalse(farc.has_signature(link))
        fifo = self.dir / "fifo.arc"
        os.mkfifo(fifo)
        with self.assertRaises(FarcError):  # must not block waiting for a writer
            farc.read_archive(fifo)
        self.assertFalse(farc.has_signature(fifo))
        self.assertTrue(stat.S_ISFIFO(os.lstat(fifo).st_mode))
        with self.assertRaises(FarcError):
            farc.read_archive(self.dir)
        with self.assertRaises(FarcError):
            farc.read_archive(self.dir / "missing.arc")
        with self.assertRaises(FarcError):
            farc.read_archive("bad\0name")

    def test_oversized_counts(self):
        blocks, folders = sample_blocks()
        self.refused(make_archive(blocks, folders,
                                  dir_raw=varint(farc.MAX_BLOCKS + 1)), "more than")
        self.refused(make_archive(blocks, folders,
                                  dir_raw=varint(1) + varint(farc.MAX_FILES + 1)), "more than")
        two = varint(2) + varint(farc.MAX_FILES // 2 + 1) * 2
        self.refused(make_archive(blocks, folders, dir_raw=two), "more than")
        folders_raw = varint(0) + varint(farc.MAX_FILES + 1)
        self.refused(make_archive(blocks, folders, dir_raw=folders_raw), "more than")
        self.refused(make_archive(blocks, folders, footer_raw=varint(farc.MAX_BLOCKS + 1)),
                     "more than")
        # a believable count with nothing behind it runs off the end, it is not guessed
        self.refused(make_archive(blocks, folders, dir_raw=varint(1) + varint(50_000)),
                     "damaged")

    def test_oversized_control_blocks(self):
        blocks, folders = sample_blocks()
        self.refused(make_archive(blocks, folders, dir_orig=farc.MAX_CONTROL_BLOCK + 1),
                     "larger than")
        self.refused(make_archive(blocks, folders, footer_orig=1 << 40), "larger than")
        data = make_archive(blocks, folders)
        with mock.patch.object(farc, "MAX_CONTROL_BLOCK", 40):
            self.refused(data, "larger than")
        with mock.patch.object(farc, "MAX_INDEX_TOTAL", 60):
            self.refused(make_archive(blocks, folders), "larger than")

    def test_lzma_index_cannot_inflate_past_declared_size(self):
        blocks, folders = sample_blocks()
        # The directory claims fewer bytes than its LZMA stream holds: output stops at the
        # claim, so the CRC (of the full text) fails instead of reading more.
        self.refused(make_archive(blocks, folders, dir_index="lzma:1mb", dir_orig=10), "CRC")

    def test_huge_declared_file_size_is_only_reported(self):
        blocks = [("lzma:1mb", [F("big", b"tiny", size=4 << 30)])]
        arc = self.read(make_archive(blocks))
        self.assertEqual(arc.total_size, 4 << 30)
        self.assertIn("4.0 GiB", farc.summarise(arc)[0])

    def test_stored_sizes_must_agree(self):
        self.refused(make_archive([("storing", [F("a", b"abc", size=4)])]), "do not agree")

    def test_block_positions(self):
        blocks = [("storing", [F("a", b"a" * 100)]), ("storing", [F("b", b"b" * 100)])]
        data = make_archive(blocks)
        arc = self.read(data)
        dir_pos = arc.blocks[1].offset + 100
        self.refused(make_archive(blocks, offsets=[dir_pos - 8, dir_pos - 50]), "overlap")
        self.refused(make_archive(blocks, offsets=[10_000_000, 100]), "outside")
        self.refused(make_archive(blocks, offsets=[dir_pos - 8, 50]), "outside")

    def test_bad_folder_number(self):
        self.refused(make_archive([("storing", [F("a", b"a", dirnum=5)])]), "folder number")

    def test_no_directory_block(self):
        blocks, folders = sample_blocks()
        self.refused(make_archive(blocks, folders, omit_dir=True), "no directory block")

    def test_one_directory_block_listed_many_times_is_refused_before_decoding(self):
        # Review finding: each listing re-read the same block, so a small footer cost hours.
        self.assertEqual(self.read(repeated_dir_archive(1, 0)).entries, [])
        with mock.patch.object(farc.lzma, "LZMADecompressor",
                               side_effect=AssertionError("decoded")):
            self.refused(repeated_dir_archive(2, 0), "overlap")
            self.refused(repeated_dir_archive(600, 4096), "overlap")

    def test_bytes_read_count_towards_the_index_cap(self):
        # A directory block whose compsize is mostly padding: small decoded size, large read.
        self.read(repeated_dir_archive(1, 5000))
        with mock.patch.object(farc, "MAX_INDEX_TOTAL", 4000):
            self.refused(repeated_dir_archive(1, 5000), "larger than")

    def test_a_caller_can_lower_the_index_cap_but_not_raise_it(self):
        data = repeated_dir_archive(1, 5000)
        path = self.dir / "low.arc"
        path.write_bytes(data)
        self.assertIsInstance(farc.read_archive(path, index_limit=1 << 20), farc.Archive)
        with self.assertRaisesRegex(farc.FarcError, "larger than"):
            farc.read_archive(path, index_limit=4000)
        with mock.patch.object(farc, "MAX_INDEX_TOTAL", 4000), \
                self.assertRaisesRegex(farc.FarcError, "larger than"):
            farc.read_archive(path, index_limit=1 << 30)

    def test_shared_folder_name_cannot_multiply_into_huge_paths(self):
        # Review finding: one long folder name shared by many files was joined once per
        # file, so a ~1 KB archive grew to gigabytes of text.
        folder = "f" * farc.MAX_NAME
        files = [F(str(i), b"", dirnum=0) for i in range(300)]
        data = make_archive([("storing", files)], folders=(folder,), index="lzma:1mb")
        self.assertLess(len(data), 4096)
        self.assertEqual(len(self.read(data).entries), 300)
        with mock.patch.object(farc, "MAX_PATH_TOTAL", 100 * (farc.MAX_NAME + 4)):
            self.refused(data, "longer than gitfurl reads")
        # and at the real cap, a little over MAX_PATH_TOTAL / MAX_NAME files is enough
        n = farc.MAX_PATH_TOTAL // farc.MAX_NAME + 1
        files = [F("", b"", dirnum=0) for _ in range(n)]
        self.refused(make_archive([("storing", files)], folders=(folder,), index="lzma:1mb"),
                     "longer than gitfurl reads")

    def test_name_length_is_capped(self):
        ok = "n" * farc.MAX_NAME
        self.assertEqual(self.read(make_archive([("storing", [F(ok, b"1")])])).entries[0].path,
                         ok)
        self.refused(make_archive([("storing", [F(ok + "n", b"1")])]), "longer than 4096")
        self.refused(make_archive([("storing", [F("a", b"1")])], folders=("d" * 5000,)),
                     "longer than 4096")
        self.refused(make_archive([("storing" + "x" * 5000, [F("a", b"1")])]),
                     "longer than 4096")


class Names(FarcTestBase):
    def test_unsafe_names_counted_and_cleaned(self):
        files = [F("ok.txt", b"1"), F("/abs", b"2"), F("../up", b"3"), F("C:win", b"4"),
                 F("a\\b", b"5"), F("ctl\x01\x1b[31m", b"6"), F("", b"7"), F("f", b"8", 1),
                 F("g", b"9", 2), F(".", is_dir=True), F("deep/./ok", b"0")]
        arc = self.read(make_archive([("storing", files)], folders=("", "..", "x/D:")))
        flagged = [e.path for e in arc.entries if farc.unsafe_name(e.path)]
        self.assertEqual(flagged, ["/abs", "../up", "C:win", "ctl\x01\x1b[31m", "",
                                   "../f", "x/D:/g"])
        line = next(s for s in farc.summarise(arc) if "safe" in s)
        self.assertTrue(line.startswith("7 names would not be safe to extract as stored: "))
        self.assertEqual(line.count("'"), 2 * farc.MAX_EXAMPLES)
        self.assertTrue(line.endswith(", ..."))
        self.assertTrue(all(c.isprintable() for c in line))

    def test_relative_parts(self):
        rp = farc.relative_parts
        self.assertEqual(rp("a\\b/c"), ["a", "b", "c"])
        self.assertEqual(rp("./x//y/./z/"), ["x", "y", "z"])
        self.assertEqual(rp("."), [])
        for bad in ("", "/a", "\\a", "a\\..\\b", "a/../b", "C:x", "a/c:/b", "a\x00b",
                    "a\x7fb"):
            self.assertIsNone(rp(bad), bad)

    def test_long_name_capped(self):
        arc = self.read(make_archive([("storing", [F("/" + "n" * 4000, b"1")])]))
        line = farc.summarise(arc)[-1]
        self.assertLess(len(line), 200)

    def test_control_pattern_is_exactly_unicode_cc(self):
        cc = {c for c in map(chr, range(0x110000)) if unicodedata.category(c) == "Cc"}
        self.assertEqual(cc, {c for c in map(chr, range(0x110000))
                              if farc._CONTROL.fullmatch(c)})

    def test_clean_cuts_before_it_looks(self):
        from gitfurl.detect import clean

        self.assertEqual(clean("abc", 3), "abc")
        self.assertEqual(clean("\x1b\x01\x02x", 3), "???...")
        long ="\x1b" * (8 << 20)
        self.assertEqual(clean(long, 5), "?????...")

    def test_bad_utf8_is_replaced(self):
        arc = self.read(make_archive([("storing", [F(b"\xff\xfeok", b"1")])]))
        self.assertEqual(arc.entries[0].path, "\ufffd\ufffdok")
        self.assertFalse(farc.unsafe_name(arc.entries[0].path))


class Fuzz(FarcTestBase):
    """Damaged input of any kind must end in an Archive or a FarcError, nothing else."""

    def check(self, data: bytes):
        try:
            arc = self.read(data)
        except FarcError:
            return
        farc.summarise(arc)

    def bases(self):
        blocks, folders = sample_blocks()
        lz, _ = sample_blocks("lzma:1mb")
        return [make_archive(blocks, folders), make_archive(lz, folders, index="lzma:1mb"),
                make_archive(blocks, folders, sfx=b"MZ" + b"\x90" * 300, locked=True)]

    def test_mutated_files(self):
        rnd = random.Random(20261119)
        bases = self.bases()
        for i in range(300):
            data = bytearray(rnd.choice(bases))
            op = rnd.randrange(4)
            if op == 0:
                for _ in range(rnd.randint(1, 8)):
                    data[rnd.randrange(len(data))] = rnd.getrandbits(8)
            elif op == 1:
                data = data[:rnd.randrange(len(data))]
            elif op == 2:
                p = rnd.randrange(len(data))
                data[p:p] = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 16)))
            else:
                data = data[-rnd.randint(1, 600):]
            with self.subTest(i=i, op=op):
                self.check(bytes(data))

    def test_mutated_index_with_valid_crcs(self):
        # Mutate the decoded directory and footer, then rebuild with matching CRCs, so the
        # parser itself (not only the CRC check) sees the damage.
        rnd = random.Random(42)
        blocks, folders = sample_blocks()

        def mutate(plain: bytes) -> bytes:
            raw = bytearray(plain)
            for _ in range(rnd.randint(1, 6)):
                if rnd.random() < 0.8 and raw:
                    raw[rnd.randrange(len(raw))] = rnd.getrandbits(8)
                else:
                    raw = raw[:rnd.randrange(len(raw) + 1)]
            return bytes(raw)

        for i in range(300):
            index = rnd.choice(["storing", "lzma:1mb"])
            with self.subTest(i=i):
                self.check(make_archive(blocks, folders, dir_mutator=mutate, index=index))
        for i in range(200):
            raw = bytes(rnd.getrandbits(8) for _ in range(rnd.randint(1, 80)))
            with self.subTest(footer=i):
                self.check(make_archive(blocks, folders, footer_raw=raw))

    def test_random_garbage(self):
        rnd = random.Random(3)
        for i in range(200):
            n = rnd.randint(0, 5000)
            data = bytearray(rnd.getrandbits(8) for _ in range(n))
            if n > 20 and rnd.random() < 0.5:
                p = rnd.randrange(max(1, n - 40), n - 4)
                data[p:p + 4] = SIG
            with self.subTest(i=i):
                self.check(bytes(data))


if __name__ == "__main__":
    unittest.main()
