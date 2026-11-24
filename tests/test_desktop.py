"""The optional application-menu entry (gitfurl cli desktop-entry). Temporary folders only."""

import io
import os
import stat
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock


from gitfurl import cli, desktop


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class DesktopEntryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.data = self.base / "data"
        self.bin = self.base / "my bin"
        self.bin.mkdir()
        fake = self.bin / "gitfurl"
        fake.write_text("#!/bin/sh\n")
        fake.chmod(0o700)
        self.env = {"XDG_DATA_HOME": str(self.data), "HOME": str(self.base),
                    "PATH": str(self.bin)}
        patcher = mock.patch.dict("os.environ", self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.entry = self.data / "applications" / "gitfurl.desktop"
        self.icon = self.data / "icons" / "hicolor" / "64x64" / "apps" / "gitfurl.png"

    def test_dry_run_changes_nothing(self):
        code, out, _ = run_cli("desktop-entry", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"write the menu entry {self.entry}", out)
        self.assertIn(str(self.bin / "gitfurl"), out)
        self.assertFalse(self.data.exists())

    def test_write_then_remove(self):
        code, out, _ = run_cli("desktop-entry", "--yes")
        self.assertEqual(code, 0, out)
        text = self.entry.read_text()
        self.assertIn('Exec="' + str(self.bin / "gitfurl") + '"', text)  # a space: quoted
        self.assertIn(desktop.MARKER, text.splitlines())
        self.assertEqual(self.icon.read_bytes(), desktop.PACKAGE_ICON.read_bytes())
        for p in (self.entry, self.icon):
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o644)
        self.assertEqual([p.name for p in self.entry.parent.iterdir()], ["gitfurl.desktop"])
        code, out, _ = run_cli("desktop-entry", "--yes")  # again: replaces its own files
        self.assertEqual(code, 0)
        code, out, _ = run_cli("desktop-entry", "--remove", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(self.entry.exists())
        self.assertFalse(self.icon.exists())

    def test_files_it_did_not_write_are_left(self):
        self.entry.parent.mkdir(parents=True)
        self.entry.write_text("[Desktop Entry]\nName=mine\n")
        code, _out, err = run_cli("desktop-entry", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("was not written by gitfurl", err)
        code, _out, err = run_cli("desktop-entry", "--remove", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(self.entry.read_text(), "[Desktop Entry]\nName=mine\n")
        self.entry.unlink()
        self.icon.parent.mkdir(parents=True)
        self.icon.write_bytes(b"another icon")
        code, _out, err = run_cli("desktop-entry", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("is not gitfurl's icon", err)
        code, out, _ = run_cli("desktop-entry", "--remove", "--yes")
        self.assertEqual(code, 0)
        self.assertIn("left", out)
        self.assertEqual(self.icon.read_bytes(), b"another icon")

    def test_a_link_is_not_followed(self):
        target = self.base / "elsewhere.desktop"
        target.write_text("x\n" + desktop.MARKER + "\n")
        self.entry.parent.mkdir(parents=True)
        os.symlink(target, self.entry)
        code, _out, err = run_cli("desktop-entry", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("could not be read safely", err)
        self.assertTrue(self.entry.is_symlink())
        self.assertEqual(target.read_text(), "x\n" + desktop.MARKER + "\n")

    def test_no_terminal_and_no_yes_is_not_approval(self):
        with mock.patch("sys.stdin.isatty", return_value=False):
            code, out, _ = run_cli("desktop-entry")
        self.assertEqual(code, 2)
        self.assertIn("Not approved", out)
        self.assertFalse(self.data.exists())

    def test_root_is_refused(self):
        with mock.patch("gitfurl.cli.running_as_root", return_value=True):
            code, _out, _err = run_cli("desktop-entry", "--yes")
        self.assertEqual(code, 1)
        self.assertFalse(self.data.exists())

    def test_exec_quoting_and_fallback(self):
        self.assertEqual(desktop._exec_quote("/usr/bin/gitfurl"), "/usr/bin/gitfurl")
        # Two levels (Desktop Entry spec): quoting escapes, then the string-value escape
        # doubles every backslash. In the file: "/a b/\\$x\\"\\`\\\\\\\\%%"
        self.assertEqual(desktop._exec_quote('/a b/$x"`\\%'),
                         '"/a b/\\\\$x\\\\"\\\\`\\\\\\\\%%"')
        with self.assertRaises(desktop.DesktopRefused):
            desktop._exec_quote("/a\nb")
        argv, workdir = desktop.launch_argv({"PATH": str(self.base / "empty")})
        self.assertEqual(argv[1:], ["-m", "gitfurl"])
        self.assertTrue(Path(argv[0]).is_absolute())
        self.assertTrue((Path(workdir) / "gitfurl" / "__init__.py").is_file())  # a checkout
        self.assertIn("Path=" + workdir, desktop.entry_text(argv, workdir))

    def test_a_venv_python_link_is_kept(self):
        venv = self.base / "venv" / "bin"
        venv.mkdir(parents=True)
        os.symlink(sys.executable, venv / "python")
        with mock.patch("sys.executable", str(venv / "python")):
            argv, _w = desktop.launch_argv({"PATH": str(self.base / "empty")})
        self.assertEqual(argv[0], str(venv / "python"))

    def test_plan_lists_new_folders_and_replacing(self):
        code, out, _ = run_cli("desktop-entry", "--dry-run")
        self.assertIn(f"create the folder {self.data}", out)
        self.assertIn(f"create the folder {self.icon.parent}", out)
        self.assertIn("write the menu entry", out)
        self.assertIn('Exec="' + str(self.bin / "gitfurl") + '"', out)
        run_cli("desktop-entry", "--yes")
        code, out, _ = run_cli("desktop-entry", "--dry-run")
        self.assertNotIn("create the folder", out)
        self.assertIn("replace gitfurl's menu entry", out)
        self.assertIn("replace gitfurl's icon", out)

    def test_a_failed_step_reports_what_was_done(self):
        real = desktop._write

        def failing(path, data):
            if path.name == "gitfurl.desktop":
                raise PermissionError(13, "Permission denied", str(path))
            real(path, data)

        with mock.patch("gitfurl.desktop._write", side_effect=failing):
            code, out, err = run_cli("desktop-entry", "--yes")
        self.assertEqual(code, 1)
        self.assertIn(f"already done: wrote {self.icon}", out)
        self.assertIn("--remove' takes away what was written", err)
        code, out, _ = run_cli("desktop-entry", "--remove", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(self.icon.exists())

    def test_remove_takes_leftover_temp_files_and_an_older_icon(self):
        run_cli("desktop-entry", "--yes")
        self.icon.write_bytes(b"an icon from an older gitfurl")  # beside gitfurl's entry
        left = self.entry.parent / ".gitfurl.desktop.gitfurl-4242"
        left.write_text("partial")
        other = self.entry.parent / ".gitfurl.desktop.gitfurl-x"
        other.write_text("not ours")
        code, out, _ = run_cli("desktop-entry", "--yes")  # rewriting is allowed
        self.assertEqual(code, 0, out)
        self.icon.write_bytes(b"an icon from an older gitfurl")
        code, out, _ = run_cli("desktop-entry", "--remove", "--yes")
        self.assertEqual(code, 0)
        self.assertFalse(self.icon.exists())
        self.assertFalse(left.exists())
        self.assertTrue(other.exists())  # not one of gitfurl's names

    def test_data_home(self):
        self.assertEqual(desktop.data_home({"HOME": "/h"}), Path("/h/.local/share"))
        self.assertEqual(desktop.data_home({"HOME": "/h", "XDG_DATA_HOME": "rel"}),
                         Path("/h/.local/share"))  # a relative value is ignored (XDG spec)
        with self.assertRaises(desktop.DesktopRefused):
            desktop.data_home({})


if __name__ == "__main__":
    unittest.main()
