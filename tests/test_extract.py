"""Native FreeArc extraction. Archives are built in memory (tests/farc_writer.py) and written to
a temporary folder for the test only; the "prefix" is a plain temporary folder."""

import contextlib
import io
import os
import random
import resource
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

try:
    from farc_writer import F, make_archive
except ImportError:  # python -m unittest tests.test_extract
    from tests.farc_writer import F, make_archive

from gitfurl import cli, extract
from gitfurl.extract import ExtractRefused, plan_extract

RND = random.Random(11)
BIG = bytes(RND.getrandbits(8) for _ in range(3 * extract.CHUNK // 2))
TEXT = b"line of text\n" * 4000


def blocks(method="lzma:1mb"):
    return [("storing", [F("readme.txt", b"hello\n"), F("empty.dat", b"")]),
            (method, [F("data", is_dir=True, dirnum=0), F("big.bin", BIG, dirnum=1),
                      F("text.txt", TEXT, dirnum=1)])], ("", "data")


def remove_deep(top):
    """Remove a deep folder tree without recursion. shutil.rmtree holds one fd per level (it
    runs out under a soft open-file limit of 1024), and on Python 3.11 both it and os.walk
    recurse once per level (RecursionError at about 1,000 levels)."""
    stack = [str(top)]
    while stack:
        path = stack[-1]
        with os.scandir(path) as it:
            subdirs = []
            for e in it:
                if e.is_dir(follow_symlinks=False):
                    subdirs.append(e.path)
                else:
                    os.unlink(e.path)
        if subdirs:
            stack.extend(subdirs)
        else:
            os.rmdir(path)
            stack.pop()


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.base = Path(self._tmp.name)
        self.src = self.base / "src"
        self.src.mkdir()
        self.setup = self.src / "setup.exe"
        self.setup.write_bytes(b"MZ" + b"\0" * 100)
        self.env = {"XDG_DATA_HOME": str(self.base / "data"), "HOME": str(self.base)}
        self.prefix = self.base / "data" / "gitfurl" / "prefixes" / "p1"
        (self.prefix / "drive_c").mkdir(parents=True)
        patcher = mock.patch.dict("os.environ", self.env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self._tmp.cleanup()

    def arc(self, name, data):
        (self.src / name).write_bytes(data)
        return self.src / name

    def plan(self):
        return plan_extract(self.setup, "p1")

    @property
    def dest(self):
        return self.prefix / "drive_c" / "Games" / "p1"

    def refused(self, fragment):
        with self.assertRaises(ExtractRefused) as cm:
            self.plan()
        self.assertIn(fragment, str(cm.exception))
        self.assertFalse((self.prefix / "drive_c" / "Games").exists())
        return str(cm.exception)


class Extracts(Base):
    def test_storing_and_lzma_files_are_written_with_their_crc_checked(self):
        self.arc("data1.bin", make_archive(*blocks()))
        plan = self.plan()
        self.assertEqual(plan.file_count, 4)
        self.assertEqual(plan.total_size, 6 + len(BIG) + len(TEXT))
        self.assertIsNone(plan.problem())
        text = "\n".join(plan.describe())
        self.assertIn("The installer is not run", text)
        self.assertIn(r"C:\Games\p1", text)
        self.assertIn("memory: up to", text)
        seen = []
        res = extract.extract(plan, on_progress=lambda done, total: seen.append(done))
        self.assertTrue(res.ok, res.lines())
        self.assertEqual(res.files, 4)
        self.assertEqual((self.dest / "readme.txt").read_bytes(), b"hello\n")
        self.assertEqual((self.dest / "empty.dat").read_bytes(), b"")
        self.assertEqual((self.dest / "data" / "big.bin").read_bytes(), BIG)
        self.assertEqual((self.dest / "data" / "text.txt").read_bytes(), TEXT)
        self.assertEqual(int((self.dest / "readme.txt").stat().st_mtime), 1_700_000_000)
        self.assertEqual(seen[-1], plan.total_size)
        self.assertIn("nothing was installed", "\n".join(res.lines()))

    def test_backslash_folders_and_deep_paths(self):
        deep = "/".join(["d"] * 1500)
        self.arc("a.arc", make_archive([("storing", [F("x\\y\\z.txt", b"z"), F(deep, b"deep")])]))
        try:
            res = extract.extract(self.plan())
            self.assertTrue(res.ok, res.lines())
            self.assertEqual((self.dest / "x" / "y" / "z.txt").read_bytes(), b"z")
            self.assertEqual(self.dest.joinpath(*deep.split("/")).read_bytes(), b"deep")
        finally:
            remove_deep(self.dest / "d")

    def test_several_archives_go_into_one_folder(self):
        self.arc("a.bin", make_archive([("storing", [F("one.txt", b"1")])]))
        self.arc("b.bin", make_archive([("lzma:1mb", [F("two.txt", b"2" * 100)])]))
        self.src.joinpath("small.txt").write_bytes(b"not an archive")
        plan = self.plan()
        self.assertEqual([a.path.name for a in plan.archives], ["a.bin", "b.bin"])
        self.assertTrue(extract.extract(plan).ok)
        self.assertEqual(sorted(p.name for p in self.dest.iterdir()), ["one.txt", "two.txt"])

    def test_the_installer_itself_can_be_the_archive(self):
        self.setup.write_bytes(make_archive([("storing", [F("f", b"x")])]))
        plan = self.plan()
        self.assertEqual([a.path for a in plan.archives], [self.setup])

    def test_crc_mismatch_is_reported_and_the_file_kept(self):
        data = bytearray(make_archive([("storing", [F("a.txt", b"AAAA"), F("b.txt", b"BBBB")])]))
        at = bytes(data).index(b"BBBB")
        data[at] = ord("X")
        self.arc("a.arc", bytes(data))
        res = extract.extract(self.plan())
        self.assertFalse(res.ok)
        self.assertEqual(res.crc_errors, ["b.txt"])
        self.assertEqual((self.dest / "b.txt").read_bytes(), b"XBBB")
        self.assertIn("failed the CRC-32 check (kept): b.txt", "\n".join(res.lines()))

    def test_lzma_block_shorter_than_its_files_is_an_error_not_a_crash(self):
        self.arc("a.arc", make_archive([("lzma:1mb", [F("a", b"a" * 100),
                                                      F("b", b"b" * 100, size=5000)])]))
        res = extract.extract(self.plan())
        self.assertIn("ended before", res.error)
        self.assertEqual(res.incomplete, "b")
        self.assertEqual((self.dest / "a").read_bytes(), b"a" * 100)
        self.assertIn("nothing was deleted", "\n".join(res.lines()))

    def test_damaged_lzma_data_is_an_error(self):
        data = bytearray(make_archive([("lzma:1mb", [F("a", BIG), F("b", TEXT)])]))
        self.arc("a.arc", bytes(data))
        blk = self.plan().archives[0].archive.blocks[0]
        # damage only the compressed data, so the index (read again before writing) is intact
        for i in range(blk.offset + 1000, blk.offset + blk.compsize // 2):
            data[i] ^= 0x5A
        self.arc("a.arc", bytes(data))
        res = extract.extract(self.plan())
        self.assertFalse(res.ok)
        self.assertTrue(res.crc_errors or "could not be decompressed" in (res.error or ""),
                        res.lines())

    def test_decoder_out_of_memory_is_reported_not_raised(self):
        self.arc("a.arc", make_archive([("lzma:1mb", [F("a", TEXT)])]))
        with mock.patch.object(extract.farc_codecs.lzma, "LZMADecompressor", side_effect=MemoryError):
            res = extract.extract(self.plan())
        self.assertIn("decoder could not be started", res.error)
        self.assertTrue(self.dest.is_dir())
        self.assertEqual(list(self.dest.iterdir()), [])  # the decoder comes before the file
        self.assertIn("nothing was deleted", "\n".join(res.lines()))

    def test_unexpected_error_while_writing_is_reported_not_raised(self):
        self.arc("a.arc", make_archive([("storing", [F("a", b"1")])]))
        plan = self.plan()
        with mock.patch.object(extract.os, "write", side_effect=RuntimeError("x")):
            res = extract.extract(plan)
        self.assertIn("unexpected error (RuntimeError)", res.error)
        self.assertEqual(res.incomplete, "a")

    def test_file_times_are_set_through_the_open_file(self):
        self.arc("a.arc", make_archive([("storing", [F("foo", b"1")])]))
        outside = self.base / "outside.txt"
        outside.write_text("x")
        os.utime(outside, (2_000_000_000, 2_000_000_000))
        real = os.utime

        def utime(target, *a, **k):
            self.assertIsInstance(target, int)  # an fd, not a name that could be swapped
            victim = self.dest / "foo"
            victim.unlink()
            os.link(outside, victim)
            return real(target, *a, **k)

        with mock.patch.object(extract.os, "utime", utime):
            extract.extract(self.plan())
        self.assertEqual(outside.stat().st_mtime, 2_000_000_000)

    def test_many_folders_and_deep_paths_under_a_low_open_file_limit(self):
        files = [F(f"dir{i:04d}/f.txt", b"x") for i in range(300)]
        files.append(F("/".join(["d"] * 400) + "/deep.txt", b"deep"))
        files.append(F(f"dir0000/{'e/' * 100}g.txt", b"g"))  # back to an evicted folder
        self.arc("a.arc", make_archive([("storing", files)]))
        plan = self.plan()
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        # well below the 300 folders and the 400-deep path; the old code kept one fd each
        limit = len(os.listdir("/proc/self/fd")) + extract._Tree.CACHE + 16
        if limit > 200:
            self.skipTest("too many fds open already")
        resource.setrlimit(resource.RLIMIT_NOFILE, (limit, hard))
        try:
            res = extract.extract(plan)
        finally:
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))
        self.assertTrue(res.ok, res.lines())
        self.assertEqual(res.files, 302)
        self.assertEqual((self.dest / "dir0299" / "f.txt").read_bytes(), b"x")
        self.assertEqual(self.dest.joinpath(*["d"] * 400, "deep.txt").read_bytes(), b"deep")
        self.assertEqual(self.dest.joinpath("dir0000", *["e"] * 100, "g.txt").read_bytes(),
                         b"g")
        remove_deep(self.dest / "d")

    def test_folders_that_differ_only_in_case_are_one_folder(self):
        self.arc("a.arc", make_archive([("storing", [F("Data", is_dir=True, dirnum=0),
                                                     F("x.txt", b"X", dirnum=1),
                                                     F("y.txt", b"Y", dirnum=2)])],
                                       folders=("", "Data", "DATA")))
        self.arc("b.arc", make_archive([("storing", [F("data/z.txt", b"Z")])]))
        plan = self.plan()
        self.assertIn("first spelling", "\n".join(plan.describe()))
        res = extract.extract(plan)
        self.assertTrue(res.ok, res.lines())
        self.assertEqual([p.name for p in self.dest.iterdir()], ["Data"])
        self.assertEqual(sorted(p.name for p in (self.dest / "Data").iterdir()),
                         ["x.txt", "y.txt", "z.txt"])

    def test_stop_during_a_file_keeps_the_partial_file(self):
        self.arc("a.arc", make_archive(*blocks()))

        class Cancel:
            calls = 0

            def is_set(self):
                self.calls += 1
                return self.calls > 20

        with mock.patch.object(extract, "CHUNK", 4096):
            res = extract.extract(self.plan(), cancel=Cancel())
        self.assertTrue(res.stopped)
        self.assertEqual(res.incomplete, "data/big.bin")
        size = (self.dest / "data" / "big.bin").stat().st_size
        self.assertTrue(0 < size < len(BIG), size)
        self.assertEqual((self.dest / "readme.txt").read_bytes(), b"hello\n")
        self.assertIn("incomplete file (kept): data/big.bin", "\n".join(res.lines()))

    def test_stop_ends_between_files_and_keeps_what_was_written(self):
        self.arc("a.arc", make_archive(*blocks()))
        cancel = threading.Event()
        res = extract.extract(self.plan(), cancel=cancel,
                              on_progress=lambda done, total: cancel.set())
        self.assertTrue(res.stopped)
        self.assertEqual(res.files, 1)
        self.assertTrue((self.dest / "readme.txt").exists())
        self.assertIn("Extraction stopped", res.lines()[0])

    def test_ctrl_c_is_reported_as_stopped(self):
        self.arc("a.arc", make_archive(*blocks()))

        def interrupt(done, total):
            raise KeyboardInterrupt

        res = extract.extract(self.plan(), on_progress=interrupt)
        self.assertTrue(res.stopped)


class Refusals(Base):
    def test_no_archive(self):
        self.refused("no FreeArc archive")

    def test_unhandled_method_and_encryption_are_refused_without_naming_them(self):
        self.arc("a.arc", make_archive([("zzzmethod:9", [F("a", b"1")])]))
        msg = self.refused(extract.farc.KIND_OTHER)
        self.assertNotIn("zzzmethod", msg)
        (self.src / "a.arc").unlink()
        self.arc("b.arc", make_archive([("lzma:1mb+aes-256/ctr:n1000", [F("a", b"1")])]))
        msg = self.refused(extract.farc.KIND_OTHER)
        self.assertNotIn("aes", msg)

    def test_unsafe_names(self):
        for bad in ("../up", "/abs", "C:\\win", "a/../../b", "ctl\x1b"):
            with self.subTest(bad=bad):
                self.arc("a.arc", make_archive([("storing", [F(bad, b"1")])]))
                msg = self.refused("not safe to extract")
                self.assertNotIn("\x1b", msg)

    def test_names_that_differ_only_in_case(self):
        self.arc("a.arc", make_archive([("storing", [F("Readme.txt", b"1")])]))
        self.arc("b.arc", make_archive([("storing", [F("README.TXT", b"2")])]))
        self.refused("letter case ignored")

    def test_a_folder_and_a_file_that_differ_only_in_case(self):
        self.arc("a.arc", make_archive([("storing", [F("Data/x", b"1"), F("DATA", b"2")])]))
        self.refused("letter case ignored")

    def test_names_windows_cannot_use(self):
        for bad in ("foo.", "foo ", "d./x", "CON", "con.txt", "Lpt1", "nul .x", "ab:c",
                    "a?b", 'q"', "a|b", "a<b", "a>b", "a*b", "..."):
            with self.subTest(bad=bad):
                self.arc("a.arc", make_archive([("storing", [F(bad, b"1")])]))
                self.refused("Windows cannot use")
        self.arc("a.arc", make_archive([("storing", [F("console.txt", b"1"), F("COM10", b"2"),
                                                     F(".hidden", b"3"), F("a.b.c", b"4")])]))
        self.assertEqual(self.plan().file_count, 4)

    def test_too_many_folders(self):
        self.arc("a.arc", make_archive([("storing", [F(f"f{i}/x", b"") for i in range(5)])]))
        with mock.patch.object(extract, "MAX_FOLDERS", 3):
            self.refused("more than 3 folders")

    def test_deep_and_many_paths_are_planned_in_linear_time(self):
        # each entry a distinct chain 1,000 deep: quadratic prefix tables took seconds and
        # hundreds of MiB here; the name table takes one step per part
        deep = "d/" * 1000
        self.arc("a.arc", make_archive([("storing", [F(f"{i:x}/{deep}f", b"")
                                                     for i in range(200)])]))
        plan = self.plan()
        self.assertEqual(len(plan.folders), 1 + 200 * 1001)
        self.assertEqual(plan.parts(len(plan.folders) - 1), ["c7", *["d"] * 1000])

    def test_a_file_and_a_folder_with_the_same_name(self):
        self.arc("a.arc", make_archive([("storing", [F("x", b"1"), F("x/y", b"2")])]))
        self.refused("inside a file")
        self.arc("a.arc", make_archive([("storing", [F("x/y", b"2"), F("x", b"1")])]))
        self.refused("same file or folder")

    def test_name_too_long(self):
        self.arc("a.arc", make_archive([("storing", [F("n" * 300, b"1")])]))
        self.refused("too long")

    def test_unreadable_archive(self):
        self.arc("a.arc", make_archive([("storing", [F("a", b"1")])])[:-3])
        self.refused("a.arc")


class Problems(Base):
    def setUp(self):
        super().setUp()
        self.arc("a.arc", make_archive([("storing", [F("a.txt", b"1")])]))

    def test_missing_prefix(self):
        os.rmdir(self.prefix / "drive_c")
        os.rmdir(self.prefix)
        plan = self.plan()
        self.assertIn("prefix create p1", plan.problem())
        with self.assertRaises(ExtractRefused):
            extract.extract(plan)

    def test_existing_destination_is_never_written_into(self):
        self.dest.mkdir(parents=True)
        (self.dest / "a.txt").write_bytes(b"mine")
        plan = self.plan()
        self.assertIn("already exists", plan.problem())
        with self.assertRaises(ExtractRefused):
            extract.extract(plan)
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"mine")

    def statvfs(self, frsize=4096, bavail=10**9, files=10**6, favail=10**6):
        st = mock.Mock(f_frsize=frsize, f_bsize=frsize, f_bavail=bavail, f_files=files,
                       f_favail=favail)
        return mock.patch.object(extract.os, "statvfs", return_value=st)

    def test_not_enough_space(self):
        with self.statvfs(bavail=0):
            self.assertIn("not enough free space", self.plan().problem())

    def test_space_counts_whole_blocks_and_folders(self):
        self.arc("a.arc", make_archive([("storing", [F(f"d{i}/f", b"x") for i in range(100)])]))
        plan = self.plan()
        self.assertEqual(plan.total_size, 100)
        # 201 blocks needed: 100 one-byte files, 100 folders, the destination
        with self.statvfs(bavail=200):
            self.assertIn("not enough free space", plan.problem())
        with self.statvfs(bavail=201):
            self.assertIsNone(plan.problem())

    def test_not_enough_inodes(self):
        with self.statvfs(favail=1):
            self.assertIn("not enough free inodes", self.plan().problem())
        with self.statvfs(files=0, favail=0):  # no inode count (btrfs): not checked
            self.assertIsNone(self.plan().problem())

    def test_refusal_after_creating_games_says_so(self):
        real = extract._open_dir

        def racing(parent, name):
            if name == "p1":
                raise FileNotFoundError(2, "No such file or directory")
            return real(parent, name)

        plan = self.plan()
        with mock.patch.object(extract, "_open_dir", racing), \
                self.assertRaises(ExtractRefused) as cm:
            extract.extract(plan)
        games = self.prefix / "drive_c" / "Games"
        self.assertEqual(cm.exception.created, [games, self.dest])
        self.assertIn("No file was written; empty folders created and kept",
                      cm.exception.outcome())
        self.assertNotIn("Nothing was written", cm.exception.outcome())
        self.assertEqual(list(self.dest.iterdir()), [])

    def test_symlinked_games_folder_is_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (self.prefix / "drive_c" / "Games").symlink_to(outside)
        with self.assertRaises(ExtractRefused):
            extract.extract(self.plan())
        self.assertEqual(list(outside.iterdir()), [])

    def test_archive_changed_after_the_plan(self):
        plan = self.plan()
        self.arc("a.arc", make_archive([("storing", [F("a.txt", b"2")])]))
        with self.assertRaises(ExtractRefused) as cm:
            extract.extract(plan)
        self.assertIn("changed after the plan", str(cm.exception))
        self.assertFalse(self.dest.exists())


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Cli(Base):
    def setUp(self):
        super().setUp()
        self.arc("a.arc", make_archive(*blocks()))

    def test_dry_run_writes_nothing(self):
        code, out, _ = run_cli("extract", str(self.setup), "--prefix", "p1", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("DRY RUN", out)
        self.assertIn("The installer is not run", out)
        self.assertFalse((self.prefix / "drive_c" / "Games").exists())

    def test_without_a_terminal_nothing_is_written(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, _, _ = run_cli("extract", str(self.setup), "--prefix", "p1")
        self.assertEqual(code, 2)
        self.assertFalse((self.prefix / "drive_c" / "Games").exists())

    def test_yes_extracts(self):
        code, out, _ = run_cli("extract", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 0, out)
        self.assertIn("Extraction finished: 4 files", out)
        self.assertEqual((self.dest / "data" / "big.bin").read_bytes(), BIG)

    def test_refusal_exit_status(self):
        (self.src / "a.arc").unlink()
        code, _, err = run_cli("extract", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was written", err)

    def test_root_is_refused(self):
        with mock.patch("gitfurl.cli.running_as_root", return_value=True):
            code, _, _ = run_cli("extract", str(self.setup), "--prefix", "p1", "--yes")
        self.assertEqual(code, 1)


if __name__ == "__main__":
    with contextlib.suppress(SystemExit):
        unittest.main()
