"""Keep-bad-file-CRC opt-in, using only tiny archives built by the tests."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from gitfurl import cli, extract, farc_codecs, route, timeline
from gitfurl.detect import detect

try:
    from farc_writer import F, make_archive
except ImportError:
    from tests.farc_writer import F, make_archive

WARNING = extract.KEEP_BAD_CRC_WARNING


def bad_crc(*which):
    """A dir_mutator that flips the stored CRC-32 of the files at the given positions from
    the end (1 = the last file). The CRCs are the last four bytes per file of the index."""
    def mutate(plain):
        data = bytearray(plain)
        for k in which:
            data[-4 * k] ^= 1
        return bytes(data)
    return mutate


class KeepBadFileCRC(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.path = self.base / "data.arc"
        self.env = {"XDG_DATA_HOME": str(self.base / "data"),
                    "XDG_STATE_HOME": str(self.base / "state"), "HOME": str(self.base)}
        self.prefix = self.base / "data/gitfurl/prefixes/p1"
        (self.prefix / "drive_c").mkdir(parents=True)
        self.dest = self.prefix / "drive_c/Games/p1"
        # b.txt (the last file) gets a wrong stored CRC; a.txt stays right
        self.blocks = [("storing", [F("a.txt", b"hello"), F("b.txt", b"world")])]

    def write(self, blocks=None, **kwargs):
        kwargs.setdefault("dir_mutator", bad_crc(1))
        self.path.write_bytes(make_archive(blocks or self.blocks, **kwargs))
        return self.path

    def plan(self, keep=True, **kwargs):
        return extract.plan_extract(self.path, "p1", self.env, keep_bad_file_crc=keep, **kwargs)

    def stored(self):
        return zlib.crc32(b"world") ^ 1, zlib.crc32(b"world")

    # --- the extraction itself ---

    def test_default_removes_the_file_and_suggests_the_flag(self):
        self.write()
        plan = self.plan(keep=False)
        self.assertNotIn(WARNING, "\n".join(plan.describe()))
        res = extract.extract(plan)
        self.assertFalse(res.ok)
        self.assertEqual(res.crc_errors, ["b.txt"])
        self.assertEqual(res.crc_kept, [])
        self.assertFalse((self.dest / "b.txt").exists())
        self.assertEqual((self.dest / "a.txt").read_bytes(), b"hello")
        text = "\n".join(res.lines())
        self.assertIn("1 file failed the CRC-32 check (removed again): b.txt", text)
        self.assertIn(extract.KEEP_BAD_CRC_SUGGESTION, text)
        self.assertIn("--keep-bad-file-crc", text)
        self.assertNotIn("every one of", text)  # a.txt matched, so not the whole archive
        stored, computed = self.stored()
        self.assertEqual(res.crc_details, [("b.txt", stored, computed, "removed again")])

    def test_flag_keeps_the_file_and_lists_it(self):
        self.write()
        plan = self.plan()
        described = plan.describe()
        self.assertIn(f"  {WARNING}", described[:2])  # near the top, loud
        self.assertIn("a file that does not match is KEPT", "\n".join(described))
        res = extract.extract(plan)
        self.assertTrue(res.ok)
        self.assertEqual(res.crc_errors, [])
        self.assertEqual(res.crc_kept, ["b.txt"])
        self.assertEqual(res.files, 2)
        self.assertEqual((self.dest / "b.txt").read_bytes(), b"world")
        stored, computed = self.stored()
        text = "\n".join(res.lines())
        self.assertIn(f"WARNING: 1 file kept although the CRC-32 does not match the archive "
                      f"(--keep-bad-file-crc): b.txt (stored {stored:08X}, computed "
                      f"{computed:08X})", text)
        self.assertNotIn(extract.KEEP_BAD_CRC_SUGGESTION, text)

    def test_end_text_names_the_first_few_then_a_count(self):
        files = [F(f"f{i}.bin", bytes([i]) * 3) for i in range(8)]
        self.write([("storing", files)], dir_mutator=bad_crc(*range(1, 9)))
        res = extract.extract(self.plan())
        self.assertEqual(len(res.crc_kept), 8)
        line = next(ln for ln in res.lines() if ln.startswith("WARNING"))
        self.assertIn("8 files kept", line)
        self.assertEqual(line.count("stored "), extract.SHOWN_KEPT)
        self.assertIn("and 3 more (every one is in the log)", line)

    def test_every_file_of_an_archive_failed_is_noted(self):
        for keep in (False, True):
            with self.subTest(keep=keep):
                if self.dest.exists():
                    for p in self.dest.iterdir():
                        p.unlink()
                    self.dest.rmdir()
                self.write(dir_mutator=bad_crc(1, 2))
                res = extract.extract(self.plan(keep=keep))
                self.assertEqual(res.all_failed, [("data.arc", 2)])
                self.assertIn("note: every one of the 2 files in data.arc failed its CRC-32 "
                              "check", res.lines())

    def test_size_mismatch_still_fails_with_the_flag(self):
        self.write([("lzma:1mb", [F("a", b"a" * 100), F("b", b"b" * 100, size=5000)])])
        res = extract.extract(self.plan())
        self.assertFalse(res.ok)
        self.assertIn("ended before", res.error)
        self.assertEqual(res.crc_kept, [])
        self.assertEqual(res.incomplete, "b")

    def test_decoder_error_still_fails_with_the_flag(self):
        self.write()
        plan = self.plan()

        class Broken:
            def read(self, n):
                raise farc_codecs.CodecError("damaged data (synthetic)")

        with mock.patch.object(farc_codecs, "open_chain", return_value=Broken()):
            res = extract.extract(plan)
        self.assertFalse(res.ok)
        self.assertIn("could not be decompressed", res.error)
        self.assertEqual(res.crc_kept, [])
        self.assertEqual(res.files, 0)

    def test_existing_destination_is_still_refused(self):
        self.write()
        self.dest.mkdir(parents=True)
        with self.assertRaisesRegex(extract.ExtractRefused, "already exists"):
            extract.extract(self.plan())

    def test_bad_index_warning_is_reworded_when_files_are_kept(self):
        self.write(bad_dir_crc=True)
        off = self.plan(keep=False, accept_bad_index_crc=True)
        on = self.plan(accept_bad_index_crc=True)
        self.assertIn("a file that fails is not kept", off.warnings[0])
        self.assertIn("a file that fails is KEPT (--keep-bad-file-crc)", on.warnings[0])
        self.assertTrue(extract.extract(on).ok)  # the recheck compares the archive's own text

    # --- route, log and timeline ---

    def test_native_route_log_and_timeline(self):
        self.write()
        det = detect(self.path)
        choice = route.choose_route(self.path, "p1", det, env=self.env, keep_bad_file_crc=True)
        self.assertEqual(choice.route, "native")
        self.assertTrue(choice.extract.keep_bad_file_crc)
        plan = route.NativeInstallPlan(self.path, "p1", self.prefix, choice, False)
        self.assertEqual(plan.describe()[0], WARNING)
        tl = timeline.Timeline()
        timeline.plan_entries(tl, det, choice, self.prefix, True, False)
        run = route.run_native(plan, env=self.env, timeline=tl)
        tl.end("Done")
        self.assertEqual(run.status, 0)
        stored, computed = self.stored()
        log = run.log_path.read_text()
        self.assertIn(f"CRC-32 mismatch: b.txt: stored {stored:08X}, computed "
                      f"{computed:08X}; kept", log)
        self.assertEqual(stat.S_IMODE(run.log_path.stat().st_mode), 0o600)
        rows = [(e.kind, e.what) for e in tl.entries()]
        self.assertIn(("note", "Warning: files whose CRC-32 does not match the archive are "
                               "KEPT"), rows)
        self.assertIn(("note", "Warning: 1 files kept although their CRC-32 does not match "
                               "the archive"), rows)
        self.assertIn("Warning: 1 files kept", tl.path.read_text())

    def test_native_run_without_the_flag_suggests_it(self):
        self.write(dir_mutator=bad_crc(1, 2))
        det = detect(self.path)
        choice = route.choose_route(self.path, "p1", det, env=self.env)
        plan = route.NativeInstallPlan(self.path, "p1", self.prefix, choice, False)
        tl = timeline.Timeline()
        run = route.run_native(plan, env=self.env, timeline=tl)
        tl.end("Done")
        self.assertEqual(run.status, 1)
        self.assertIn(extract.KEEP_BAD_CRC_SUGGESTION, run.lines)
        self.assertIn("removed again", run.log_path.read_text())
        rows = {e.what: e.why for e in tl.entries()}
        self.assertIn("--keep-bad-file-crc",
                      rows["Suggestion: 2 files failed the CRC-32 check"])
        self.assertIn("Every file of data.arc failed its CRC-32 check", rows)

    # --- command line ---

    def test_cli_flags_default_off(self):
        parser = cli.build_parser()
        for cmd in ("extract", "install"):
            args = [cmd, str(self.path), "--prefix", "p1"]
            self.assertFalse(parser.parse_args(args).keep_bad_file_crc)
            self.assertTrue(parser.parse_args(args + ["--keep-bad-file-crc"]).keep_bad_file_crc)

    def test_cli_extract_dry_run_then_logged_extraction(self):
        self.write()
        args = ["extract", str(self.path), "--prefix", "p1", "--keep-bad-file-crc"]
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(args + ["--dry-run"]), 0)
            self.assertIn(WARNING, out.getvalue())
            self.assertFalse(self.dest.exists())
            self.assertFalse((self.base / "state/gitfurl/logs").exists())
            with mock.patch.object(cli, "running_as_root", return_value=False):
                self.assertEqual(cli.main(args + ["--yes"]), 0)
        self.assertIn("WARNING: 1 file kept", out.getvalue())
        self.assertEqual((self.dest / "b.txt").read_bytes(), b"world")
        logs = list((self.base / "state/gitfurl/logs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("CRC-32 mismatch: b.txt", logs[0].read_text())
        tls = list((self.base / "state/gitfurl/logs").glob("*" + timeline.SUFFIX))
        self.assertEqual(len(tls), 1)
        self.assertIn("1 files kept with a CRC-32 mismatch", tls[0].read_text())

    def test_cli_extract_without_flag_suggests_it(self):
        self.write()
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), \
                mock.patch.object(cli, "running_as_root", return_value=False):
            self.assertEqual(cli.main(["extract", str(self.path), "--prefix", "p1",
                                       "--yes"]), 1)
        self.assertIn(extract.KEEP_BAD_CRC_SUGGESTION, out.getvalue())
        self.assertFalse((self.dest / "b.txt").exists())

    def test_cli_declined_approval_creates_nothing(self):
        self.write()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(cli, "running_as_root", return_value=False), \
                mock.patch.object(cli, "_confirm", return_value=2):
            self.assertEqual(cli.main(["extract", str(self.path), "--prefix", "p1",
                                       "--keep-bad-file-crc"]), 2)
        self.assertFalse((self.base / "state").exists())
        self.assertFalse((self.prefix / "drive_c/Games").exists())

    def test_cli_install_dry_run_names_the_warning(self):
        self.write()
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["install", str(self.path), "--prefix", "p1",
                                       "--dry-run", "--no-memory-limit",
                                       "--keep-bad-file-crc"]), 0)
        self.assertIn("route: native", out.getvalue())
        self.assertIn(WARNING, out.getvalue())
        self.assertFalse(self.dest.exists())

    def test_cli_install_skips_cleanup_when_files_were_kept(self):
        self.write()
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out), \
                mock.patch.object(cli, "running_as_root", return_value=False), \
                mock.patch.object(cli, "perform_cleanup") as cleanup:
            status = cli.main(["install", str(self.path), "--prefix", "p1", "--yes",
                               "--no-memory-limit", "--keep-bad-file-crc",
                               "--cleanup", "installer"])
        self.assertEqual(status, 0, out.getvalue())
        cleanup.assert_not_called()
        self.assertIn("cleanup: skipped, files whose CRC-32 does not match were kept",
                      out.getvalue())
        self.assertTrue(self.path.exists())

    # --- the window ---

    def test_window_switch_passes_the_choice(self):
        from gitfurl.gui import window_route

        self.write()
        _, off, _ = window_route(self.path, "p1", env=self.env)
        _, on, _ = window_route(self.path, "p1", env=self.env, keep_bad_file_crc=True)
        self.assertFalse(off.extract.keep_bad_file_crc)
        self.assertTrue(on.extract.keep_bad_file_crc)

    def test_window_words_point_at_the_switches(self):
        from gitfurl import gui

        self.write(bad_dir_crc=True, dir_mutator=None)
        text = gui.window_route_text(self.path, "p1", self.env)
        self.assertNotIn("--accept-bad-index-crc", text)
        self.assertIn(f"turn on '{gui.BAD_INDEX_CRC_TITLE}' in Options", text)
        said = gui.window_words(extract.KEEP_BAD_CRC_SUGGESTION)
        self.assertNotIn("--keep-bad-file-crc", said)
        self.assertIn(f"'{gui.KEEP_BAD_CRC_TITLE}' turned on in Options", said)
        self.assertNotIn("--keep-bad-file-crc", gui.window_words(WARNING))

    def test_window_switches_reset_on_another_installer(self):
        from gitfurl.gui import crc_switches_reset

        self.assertFalse(crc_switches_reset(None, "/a/setup.exe"))  # the first one seen
        self.assertFalse(crc_switches_reset("/a/setup.exe", "/a/setup.exe "))
        self.assertTrue(crc_switches_reset("/a/setup.exe", "/b/setup.exe"))
        self.assertTrue(crc_switches_reset("/a/setup.exe", ""))

    def test_window_end_text_and_cleanup_with_kept_files(self):
        from gitfurl import gui

        self.write()
        det = detect(self.path)
        choice = route.choose_route(self.path, "p1", det, env=self.env, keep_bad_file_crc=True)
        plan = route.NativeInstallPlan(self.path, "p1", self.prefix, choice, False)
        run = route.run_native(plan, env=self.env)
        outcome = route.NativeOutcome(0, None, run)
        self.assertEqual(gui.kept_bad_crc(outcome), 1)
        self.assertFalse(gui.native_cleanup_allowed(outcome, False))
        text, colour = gui.native_end_text(outcome, "skipped", [])
        self.assertEqual(colour, "warn")
        self.assertIn("Cleanup skipped: files whose CRC-32 does not match were kept.", text)
        self.assertIn(f"the '{gui.KEEP_BAD_CRC_TITLE}' switch", text)


if __name__ == "__main__":
    unittest.main()
