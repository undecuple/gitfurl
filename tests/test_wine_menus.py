"""Wine's menu builder is off for every Wine process gitfurl starts in its prefixes
(WINEDLLOVERRIDES gets winemenubuilder.exe=d, merged with what is there). Fakes only; no Wine."""

import contextlib
import io
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from gitfurl import cli
from gitfurl.prefixes import (
    MENU_BUILDER_OFF,
    MENU_LINE,
    merge_dll_overrides,
    plan_create,
    with_wineboot_env,
    without_wine_menus,
)
from gitfurl.runner import InstallPlan, build_plan

OFF = "winemenubuilder.exe=d"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class MergeTests(unittest.TestCase):
    def test_no_existing_value(self):
        self.assertEqual(MENU_BUILDER_OFF, OFF)
        self.assertEqual(merge_dll_overrides(None, OFF), OFF)
        self.assertEqual(merge_dll_overrides("", OFF), OFF)
        self.assertEqual(merge_dll_overrides(" ; ;", OFF), OFF)

    def test_an_existing_value_is_kept_and_joined_with_a_semicolon(self):
        self.assertEqual(merge_dll_overrides("mscoree,mshtml=", OFF),
                         f"mscoree,mshtml=;{OFF}")
        self.assertEqual(merge_dll_overrides("d3d9=n,b;dinput8=n", OFF),
                         f"d3d9=n,b;dinput8=n;{OFF}")

    def test_already_present_is_not_added_again(self):
        for value in (OFF, f"mscoree,mshtml=;{OFF}", "WineMenuBuilder.EXE=d",
                      "winemenubuilder=d", "mscoree,winemenubuilder.exe=d"):
            with self.subTest(value=value):
                self.assertEqual(merge_dll_overrides(value, OFF), value)
        once = merge_dll_overrides("mscoree,mshtml=", OFF)
        self.assertEqual(merge_dll_overrides(once, OFF), once)
        self.assertEqual(merge_dll_overrides(once, OFF).count("winemenubuilder"), 1)

    def test_another_mode_for_the_menu_builder_is_replaced(self):
        self.assertEqual(merge_dll_overrides("winemenubuilder.exe=n", OFF), OFF)
        self.assertEqual(merge_dll_overrides("x,winemenubuilder.exe=b;y=n", OFF),
                         f"x=b;y=n;{OFF}")

    def test_several_entries_and_multi_dll_entries(self):
        self.assertEqual(merge_dll_overrides(OFF, "mscoree,mshtml="), f"{OFF};mscoree,mshtml=")
        self.assertEqual(merge_dll_overrides("mscoree=", "mscoree,mshtml="),
                         "mscoree=;mshtml=")
        self.assertEqual(merge_dll_overrides(None, "mscoree,mshtml=", OFF),
                         f"mscoree,mshtml=;{OFF}")

    def test_env_helpers(self):
        base = {"WINEPREFIX": "/p"}
        got = without_wine_menus(base)
        self.assertEqual(got, {"WINEPREFIX": "/p", "WINEDLLOVERRIDES": OFF})
        self.assertEqual(base, {"WINEPREFIX": "/p"})  # a copy
        self.assertEqual(without_wine_menus({"WINEDLLOVERRIDES": "a=n"}),
                         {"WINEDLLOVERRIDES": f"a=n;{OFF}"})
        self.assertEqual(without_wine_menus(got), got)
        self.assertEqual(with_wineboot_env({"WINEDLLOVERRIDES": OFF})["WINEDLLOVERRIDES"],
                         f"{OFF};mscoree,mshtml=")

    def test_the_plan_line(self):
        self.assertIn("Wine's menu builder is off", MENU_LINE)
        self.assertIn("so Wine adds no menu entries or file types to your desktop", MENU_LINE)
        self.assertIn(f"WINEDLLOVERRIDES={OFF}", MENU_LINE)


class RouteEnvTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = Path(tmp.name)
        self.setup = self.base / "setup.exe"
        self.setup.write_bytes(b"MZ" + b"\0" * 64)
        self.proton = self.base / "proton-dir"
        self.proton.mkdir()
        (self.proton / "proton").write_text("")
        self.data = self.base / "d"
        patcher = mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(self.data),
                                                 "XDG_STATE_HOME": str(self.base / "s")})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_every_plan_has_the_override(self):
        for kw in ({}, {"runtime": "proton", "proton_path": str(self.proton)},
                   {"portable": True}, {"wine_addons": True}):
            with self.subTest(**kw):
                plan = build_plan(self.setup, "p1", **kw)
                self.assertEqual(plan.env_overrides["WINEDLLOVERRIDES"], OFF)

    def test_plan_create_always_has_it(self):
        for extra in (None, {}, {"WINEDLLOVERRIDES": "mscoree,mshtml="},
                      {"PROTONPATH": "/p", "GAMEID": "umu-0"}):
            with self.subTest(extra=extra):
                env = plan_create("p1", "/x/wine", extra_env=extra)["env_overrides"]
                self.assertIn(OFF, env["WINEDLLOVERRIDES"].split(";"))
                self.assertEqual(env["WINEDLLOVERRIDES"].count("winemenubuilder"), 1)

    def test_prefix_create_wine(self):
        done = subprocess.CompletedProcess([], 0)
        with mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.prefixes.run_in_group", return_value=done) as run:
            code, out, _ = run_cli("prefix", "create", "t", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["env"]["WINEDLLOVERRIDES"],
                         f"mscoree,mshtml=;{OFF}")
        self.assertIn(MENU_LINE, out)

    def test_install_wine_and_proton_set_it_for_wineboot_and_setup(self):
        done = subprocess.CompletedProcess([], 0)
        cases = (([], "/x/wine", [f"{OFF};mscoree,mshtml=", OFF]),
                 (["--runtime", "proton", "--proton-path", str(self.proton)], "/x/umu-run",
                  [OFF, OFF]))
        for n, (extra, tool, want) in enumerate(cases):
            with self.subTest(tool=tool), \
                    mock.patch.dict("os.environ", {"WINEDLLOVERRIDES": "from-shell"}), \
                    mock.patch("shutil.which", side_effect=lambda name: f"/x/{name}"), \
                    mock.patch("subprocess.run", return_value=done) as r1, \
                    mock.patch("gitfurl.prefixes.run_in_group", return_value=done) as r2, \
                    mock.patch("gitfurl.runner._run_in_group", return_value=done) as r3:
                code, out, _ = run_cli("install", str(self.setup), "--prefix", f"p{n}",
                                       "--yes", "--create-prefix", "--no-memory-limit",
                                       "--route", "wine", *extra)
                self.assertEqual(code, 0, out)
                calls = [c for m in (r2, r3, r1) for c in m.call_args_list
                         if c.args and c.args[0][:1] == [tool]]
                self.assertEqual([c.args[0][1] for c in calls],
                                 ["wineboot", str(self.setup.resolve())])
                self.assertEqual([c.kwargs["env"]["WINEDLLOVERRIDES"] for c in calls], want)
                self.assertIn(MENU_LINE, out)

    def test_dry_run_states_it(self):
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p", "--dry-run",
                               "--no-memory-limit", "--route", "wine")
        self.assertEqual(code, 0)
        self.assertIn(f"env:         WINEDLLOVERRIDES={OFF}", out)
        self.assertIn(f"desktop:     {MENU_LINE}", out)

    def test_execute_adds_it_to_a_plan_without_it(self):
        from gitfurl.runner import RunOptions, run_with_prefix

        prefix = self.data / "gitfurl" / "prefixes" / "p1"
        (prefix / "drive_c").mkdir(parents=True)
        plan = InstallPlan(argv=["wine", str(self.setup)], prefix=prefix, cwd=self.base,
                           env_overrides={"WINEPREFIX": str(prefix)})
        seen = []

        def fake(argv, **kw):
            seen.append(kw["env"])
            return subprocess.CompletedProcess(argv, 0)

        run_with_prefix(plan, "/x/wine", False, None, fake, options=RunOptions())
        self.assertEqual([e["WINEDLLOVERRIDES"] for e in seen], [OFF])


if __name__ == "__main__":
    unittest.main()
