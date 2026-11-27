"""Bad index CRC opt-in, using only tiny archives built by the tests."""

import contextlib
import io
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gitfurl import cli, extract, farc, route, timeline
from gitfurl.detect import detect

try:
    from farc_writer import F, SIG, local_descriptor, make_archive, u32
except ImportError:
    from tests.farc_writer import F, SIG, local_descriptor, make_archive, u32


class BadIndexCRC(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.path = self.base / "data.arc"
        self.env = {"XDG_DATA_HOME": str(self.base / "data"),
                    "XDG_STATE_HOME": str(self.base / "state"), "HOME": str(self.base)}
        self.prefix = self.base / "data/gitfurl/prefixes/p1"
        (self.prefix / "drive_c").mkdir(parents=True)
        self.blocks = [("storing", [F("a.txt", b"hello")])]

    def write(self, **kwargs):
        self.path.write_bytes(make_archive(self.blocks, **kwargs))
        return self.path

    def read(self, **kwargs):
        return farc.read_archive(self.path, **kwargs)

    def plan(self):
        return extract.plan_extract(self.path, "p1", self.env, accept_bad_index_crc=True)

    def test_index_default_refusal_and_explicit_warning(self):
        self.write(bad_dir_crc=True)
        with self.assertRaisesRegex(farc.FarcError, "index failed its CRC check.*--accept"):
            self.read()
        arc = self.read(accept_bad_index_crc=True)
        self.assertEqual(arc.entries[0].path, "a.txt")
        self.assertEqual(len(arc.warnings), 1)
        warning = arc.warnings[0]
        self.assertIn("stored ", warning)
        self.assertIn("computed ", warning)
        self.assertIn("each file is still checked against its own CRC", warning)
        self.assertIn("a file that fails is not kept", warning)
        self.assertIn(warning, "\n".join(farc.summarise(arc)))
        self.assertIn(warning, "\n".join(self.plan().describe()))
        result = extract.extract(self.plan())
        self.assertTrue(result.ok)
        self.assertEqual((result.dest / "a.txt").read_bytes(), b"hello")

    def test_file_crc_still_fails_and_file_is_removed(self):
        self.write(bad_dir_crc=True)
        data = bytearray(self.path.read_bytes())
        data[8] ^= 1  # stored file bytes, without changing its CRC in the directory
        self.path.write_bytes(data)
        result = extract.extract(self.plan())
        self.assertFalse(result.ok)
        self.assertEqual(len(result.crc_errors), 1)
        self.assertFalse((result.dest / "a.txt").exists())

    def test_footer_descriptor_default_refusal_and_opt_in(self):
        self.write(bad_desc_crc=True)
        with self.assertRaisesRegex(farc.FarcError, "footer descriptor failed.*--accept"):
            self.read()
        arc = self.read(accept_bad_index_crc=True)
        self.assertIn("footer descriptor failed its CRC check", arc.warnings[0])
        self.assertEqual(arc.entries[0].size, 5)

    def test_footer_block_crc_can_be_accepted(self):
        self.write()
        data = self.path.read_bytes()
        at = data.rfind(SIG)
        b = farc._Buf(data[at:-4])
        b.u32()
        kind, method, orig, comp = b.varint(), b.cstr(), b.varint(), b.varint()
        crc = b.u32()
        self.path.write_bytes(data[:at] + local_descriptor(kind, method, orig, comp, crc ^ 1))
        with self.assertRaisesRegex(farc.FarcError, "index failed"):
            self.read()
        self.assertIn("index failed", self.read(accept_bad_index_crc=True).warnings[0])

    def test_bad_structure_is_not_relaxed(self):
        for kwargs in ({"dir_raw": b"\xff", "bad_dir_crc": True},
                       {"offsets": [1 << 40], "bad_dir_crc": True},
                       {"dir_orig": 1, "bad_dir_crc": True},
                       {"dir_raw": b"\0\xff", "bad_dir_crc": True}):
            with self.subTest(kwargs=kwargs):
                self.write(**kwargs)
                with self.assertRaises(farc.FarcError):
                    self.read(accept_bad_index_crc=True)

    def test_bad_footer_fields_are_not_relaxed(self):
        self.write(bad_desc_crc=True)
        data = self.path.read_bytes()
        at = data.rfind(SIG)
        for suffix in (SIG + b"\xff" + u32(0),
                       local_descriptor(4, "storing", 1, len(data) * 10, 0, False),
                       local_descriptor(3, "storing", 1, 1, 0, False)):
            with self.subTest(suffix=suffix):
                self.path.write_bytes(data[:at] + suffix)
                with self.assertRaises(farc.FarcError):
                    self.read(accept_bad_index_crc=True)

    def test_current_crc_valid_candidate_keeps_precedence(self):
        # A later signature in a CRC-valid descriptor's method is not a descriptor.
        tail = local_descriptor(4, "storing" + SIG.decode("ascii"), 1, 1, 0)
        with mock.patch.object(farc, "FOOTER_SCAN", len(tail)):
            self.path.write_bytes(bytes(32) + tail)
            fd = os.open(self.path, os.O_RDONLY)
            try:
                reader = farc._Reader(fd, self.path.stat().st_size, accept_bad_index_crc=True)
                self.assertEqual(farc._footer_descriptor(tail, reader)[0], 0)
                self.assertEqual(reader.warnings, [])
            finally:
                os.close(fd)

    def test_recheck_refuses_changed_crc_warning(self):
        self.write(bad_dir_crc=True)
        plan = self.plan()
        self.write()  # same entries and blocks, but no longer the approved CRC warning
        with self.assertRaisesRegex(extract.ExtractRefused, "changed after the plan"):
            extract.extract(plan)
        self.assertFalse(plan.dest.exists())

    def test_cli_flags_default_off(self):
        parser = cli.build_parser()
        for cmd in ("extract", "install"):
            args = [cmd, str(self.path), "--prefix", "p1"]
            self.assertFalse(parser.parse_args(args).accept_bad_index_crc)
            self.assertTrue(parser.parse_args(args + ["--accept-bad-index-crc"])
                            .accept_bad_index_crc)

    def test_native_route_and_timeline_warning(self):
        self.write(bad_dir_crc=True)
        det = detect(self.path)
        self.assertEqual(route.choose_route(self.path, "p1", det, env=self.env).route, "wine")
        choice = route.choose_route(self.path, "p1", det, env=self.env,
                                    accept_bad_index_crc=True)
        self.assertEqual(choice.route, "native")
        tl = timeline.Timeline()
        timeline.plan_entries(tl, det, choice, self.prefix, True, False)
        warning = choice.extract.archives[0].archive.warnings[0]
        self.assertTrue(any(e.kind == "note" and e.why == warning for e in tl.entries()))
        plan = route.NativeInstallPlan(self.path, "p1", self.prefix, choice, False)
        self.assertIn(warning, "\n".join(plan.describe()))
        run = route.run_native(plan, env=self.env, timeline=tl)
        tl.end("Done")
        self.assertEqual(run.status, 0)
        self.assertIn(warning, run.log_path.read_text())
        self.assertIn(warning, tl.path.read_text())
        self.assertEqual(stat.S_IMODE(run.log_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(tl.path.stat().st_mode), 0o600)

    def test_cli_extract_dry_run_and_logged_extraction(self):
        self.write(bad_dir_crc=True)
        args = ["extract", str(self.path), "--prefix", "p1", "--accept-bad-index-crc"]
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(args + ["--dry-run"]), 0)
            self.assertFalse((self.base / "state/gitfurl/logs").exists())
            with mock.patch.object(cli, "running_as_root", return_value=False):
                self.assertEqual(cli.main(args + ["--yes"]), 0)
        self.assertIn("index failed its CRC check", out.getvalue())
        logs = list((self.base / "state/gitfurl/logs").glob("*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("index failed its CRC check", logs[0].read_text())

    def test_cli_extract_preserves_interrupt_after_unpack(self):
        self.write(bad_dir_crc=True)
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(cli, "running_as_root", return_value=False), \
                mock.patch.object(route, "listings", side_effect=KeyboardInterrupt):
            self.assertEqual(cli.main(["extract", str(self.path), "--prefix", "p1",
                                       "--accept-bad-index-crc", "--yes"]), 130)
        self.assertTrue((self.prefix / "drive_c/Games/p1/a.txt").exists())

    def test_cli_refused_approval_creates_nothing(self):
        self.write(bad_dir_crc=True)
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(io.StringIO()), \
                mock.patch.object(cli, "running_as_root", return_value=False), \
                mock.patch.object(cli, "_confirm", return_value=2):
            self.assertEqual(cli.main(["extract", str(self.path), "--prefix", "p1",
                                       "--accept-bad-index-crc"]), 2)
        self.assertFalse((self.base / "state").exists())
        self.assertFalse((self.prefix / "drive_c/Games").exists())

    def test_cli_install_dry_run_passes_option(self):
        self.write(bad_dir_crc=True)
        out = io.StringIO()
        with mock.patch.dict(os.environ, self.env), contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["install", str(self.path), "--prefix", "p1",
                                       "--dry-run", "--no-memory-limit",
                                       "--accept-bad-index-crc"]), 0)
        self.assertIn("route: native", out.getvalue())
        self.assertIn("index failed its CRC check", out.getvalue())

    def test_window_option_defaults_off_and_changes_native_choice(self):
        from gitfurl.gui import window_route

        self.write(bad_dir_crc=True)
        _, off, _ = window_route(self.path, "p1", env=self.env)
        _, on, _ = window_route(self.path, "p1", env=self.env, accept_bad_index_crc=True)
        self.assertEqual(off.route, "wine")
        self.assertEqual(on.route, "native")
        self.assertTrue(on.extract.accept_bad_index_crc)
