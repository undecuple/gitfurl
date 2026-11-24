"""Wine's Mono and Gecko prompts when gitfurl creates a prefix: off by default on the Wine
route (WINEDLLOVERRIDES=mscoree,mshtml= for wineboot only), kept with --wine-addons, and never
touched for Proton. Fakes only; no Wine."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gitfurl import cli
import subprocess

from gitfurl.prefixes import QUIET_WINEBOOT_ENV, wineboot_env, wineboot_line, with_wineboot_env
from gitfurl.route import NATIVE_WINEBOOT_ENV


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class WinebootEnvTests(unittest.TestCase):
    def test_choices(self):
        self.assertEqual(wineboot_env(), {"WINEDLLOVERRIDES": "mscoree,mshtml="})
        self.assertEqual(wineboot_env("wine", addon_prompts=True), {})
        self.assertEqual(wineboot_env("proton"), {})
        self.assertIs(NATIVE_WINEBOOT_ENV, QUIET_WINEBOOT_ENV)
        wineboot_env()["X"] = "1"  # a copy: the constant is never changed
        self.assertEqual(QUIET_WINEBOOT_ENV, {"WINEDLLOVERRIDES": "mscoree,mshtml="})

    def test_lines(self):
        self.assertIn("--wine-addons", wineboot_line())
        self.assertIn("downloads nothing", wineboot_line())
        self.assertIn("may show", wineboot_line("wine", True))
        self.assertIsNone(wineboot_line("proton"))
        self.assertIn("my switch", wineboot_line(option="my switch"))


class MergeTests(unittest.TestCase):
    def test_an_existing_override_is_kept(self):
        got = with_wineboot_env({"WINEDLLOVERRIDES": "winemenubuilder.exe=d", "A": "1"})
        self.assertEqual(got, {"WINEDLLOVERRIDES": "winemenubuilder.exe=d;mscoree,mshtml=",
                               "A": "1"})
        self.assertEqual(with_wineboot_env({"A": "1"}), {"A": "1", **QUIET_WINEBOOT_ENV})
        self.assertEqual(with_wineboot_env({"A": "1"}, "wine", True), {"A": "1"})


class DryRunTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.setup = self.base / "setup.exe"
        self.setup.write_bytes(b"MZ" + b"\0" * 64)
        patcher = mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(self.base / "d"),
                                                 "XDG_STATE_HOME": str(self.base / "s")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_dry_run_says_how_a_new_prefix_is_booted(self):
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p", "--dry-run",
                               "--no-memory-limit", "--route", "wine")
        self.assertEqual(code, 0)
        self.assertIn("if created:  wineboot: WINEDLLOVERRIDES=mscoree,mshtml=", out)
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p", "--dry-run",
                               "--no-memory-limit", "--route", "wine", "--wine-addons")
        self.assertIn("if created:  wineboot: Wine may show", out)

    def test_dry_run_for_an_existing_prefix_has_no_wineboot_line(self):
        (self.base / "d" / "gitfurl" / "prefixes" / "p" / "drive_c").mkdir(parents=True)
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p", "--dry-run",
                               "--no-memory-limit", "--route", "wine")
        self.assertEqual(code, 0)
        self.assertNotIn("if created:", out)


class InstallCreateTests(DryRunTests):
    def run_install(self, *extra):
        done = subprocess.CompletedProcess([], 0)
        with mock.patch.dict("os.environ", {"WINEDLLOVERRIDES": "from-shell"}), \
                mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("subprocess.run", return_value=done) as r1, \
                mock.patch("gitfurl.prefixes.run_in_group", return_value=done) as r2, \
                mock.patch("gitfurl.runner._run_in_group", return_value=done) as r3:
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p", "--yes",
                                   "--create-prefix", "--no-memory-limit", "--route", "wine",
                                   *extra)
        calls = [c for m in (r1, r2, r3) for c in m.call_args_list]
        boot = [c for c in calls if c.args and c.args[0][1:2] == ["wineboot"]]
        self.assertEqual(code, 0, out)
        self.assertEqual(len(boot), 1)
        return out, boot[0].kwargs["env"]

    def test_wine_addons_keeps_wines_dialogs_on_install(self):
        out, env = self.run_install("--wine-addons")
        self.assertIn("Wine may show", out)
        self.assertNotIn("WINEDLLOVERRIDES", env)  # neither gitfurl's nor the shell's

    def test_default_install_boots_quietly(self):
        out, env = self.run_install()
        self.assertEqual(env["WINEDLLOVERRIDES"], "mscoree,mshtml=")


class WindowTests(DryRunTests):
    def setUp(self):
        super().setUp()
        try:
            from gitfurl import gui
        except ImportError as exc:  # no tkinter
            self.skipTest(str(exc))
        self.gui = gui

    def test_window_plan_names_its_own_switch(self):
        text, err = self.gui.describe_plan(str(self.setup), "p", memory_limit=False)
        self.assertIsNone(err)
        self.assertIn("if created:  wineboot: WINEDLLOVERRIDES=mscoree,mshtml=", text)
        self.assertIn(self.gui.WINE_ADDONS_TITLE, text)
        self.assertNotIn("--wine-addons", text)
        text, _ = self.gui.describe_plan(str(self.setup), "p", memory_limit=False,
                                         wine_addons=True)
        self.assertIn("Wine may show", text)
        self.assertIsNone(self.gui.boot_line("proton", False))


if __name__ == "__main__":
    unittest.main()
