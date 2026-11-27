"""Portable install (bwrap). Fakes only: no Wine, no real installer, no real bwrap."""

import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest import mock

from gitfurl import cli
from gitfurl.portable import (
    LIMITS,
    build_portable_argv,
    check_portable_supported,
    describe_portable,
)
from gitfurl.runner import RunControl, RunOptions, build_plan, execute

BWRAP = "/usr/bin/bwrap"
ENV = {"HOME": "/home/u", "DISPLAY": ":0"}


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def fake_fs(present=("/tmp/.X11-unix", "/run/user", "/run/dbus")):
    """Make os.path.isdir/exists/isfile say yes for the listed paths only (others are real)."""
    real_isdir, real_exists, real_isfile = os.path.isdir, os.path.exists, os.path.isfile
    with mock.patch("os.path.isdir", lambda p: p in present or real_isdir(p)), \
            mock.patch("os.path.exists", lambda p: p in present or real_exists(p)), \
            mock.patch("os.path.isfile", lambda p: p in present or real_isfile(p)):
        yield


def make_plan(d, name="p1", wine="/usr/bin/wine", **kw):
    installer = Path(d) / "inst" / "setup.exe"
    installer.parent.mkdir(exist_ok=True)
    installer.write_bytes(b"")
    plan = build_plan(installer, name, env={"XDG_DATA_HOME": str(Path(d) / "data")},
                      portable=True, **kw)
    return installer, replace(plan, argv=[wine, *plan.argv[1:]])


def flag_pairs(argv, flag, n=2):
    return [tuple(argv[i + 1:i + 1 + n]) for i, a in enumerate(argv) if a == flag]


class ArgvShapeTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.d = self._d.name
        self.installer, self.plan = make_plan(self.d)

    def tearDown(self):
        self._d.cleanup()

    def argv(self, env=None):
        with fake_fs():
            return build_portable_argv(self.plan, BWRAP, self.installer, env or ENV)

    def test_isolation_flags_and_wine_argv_wrapped(self):
        a = self.argv()
        self.assertEqual(a[0], BWRAP)
        for flag in ("--die-with-parent", "--new-session", "--unshare-pid", "--unshare-ipc",
                     "--unshare-uts", "--unshare-net"):
            self.assertIn(flag, a)
        self.assertIn(("/", "/"), flag_pairs(a, "--ro-bind"))
        self.assertEqual(a[a.index("--") + 1:], ["/usr/bin/wine", str(self.installer)])
        self.assertEqual(a[a.index("--chdir") + 1], str(self.installer.parent))

    def test_prefix_is_writable_and_comes_after_the_home_tmpfs(self):
        a = self.argv()
        prefix = str(self.plan.prefix)
        self.assertIn((prefix, prefix), flag_pairs(a, "--bind"))
        self.assertEqual(flag_pairs(a, "--bind"), [(prefix, prefix)])  # the only writable bind
        home_tmpfs = [i for i, x in enumerate(a) if x == "--tmpfs" and a[i + 1] == "/home/u"]
        self.assertEqual(len(home_tmpfs), 1)
        self.assertGreater(a.index("--bind"), home_tmpfs[0])

    def test_installer_folder_is_read_only_and_after_tmp_tmpfs(self):
        a = self.argv()
        folder = str(self.installer.parent)
        self.assertIn((folder, folder), flag_pairs(a, "--ro-bind"))
        self.assertNotIn(folder, [p[0] for p in flag_pairs(a, "--bind")])
        tmp = next(i for i, x in enumerate(a) if x == "--tmpfs" and a[i + 1] == "/tmp")
        pos = next(i for i, x in enumerate(a) if x == "--ro-bind" and a[i + 1] == folder)
        self.assertGreater(pos, tmp)

    def test_nothing_else_is_bound(self):
        a = self.argv(dict(ENV, DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1/bus"))
        binds = flag_pairs(a, "--bind") + flag_pairs(a, "--ro-bind") + \
            flag_pairs(a, "--dev-bind-try")
        sources = {s for s, _ in binds}
        self.assertEqual(sources, {"/", str(self.plan.prefix), str(self.installer.parent),
                                   "/tmp/.X11-unix", "/dev/dri"})
        joined = " ".join(a)
        for bad in ("/.wine", "dbus", "/run/user/1/bus", "--share-net", "pulse", "pipewire"):
            self.assertNotIn(bad, joined.replace("--tmpfs /run/dbus", ""))
        self.assertIn("--clearenv", a)  # the bus address is not passed: the env starts empty
        self.assertIn(("/run/user",), flag_pairs(a, "--tmpfs", 1))  # session bus hidden
        self.assertIn(("/run/dbus",), flag_pairs(a, "--tmpfs", 1))  # system bus hidden

    def test_home_and_xdg_point_at_the_temporary_home(self):
        a = self.argv()
        sets = dict(flag_pairs(a, "--setenv"))
        self.assertEqual(sets["HOME"], "/home/u")  # the real path, which is a tmpfs inside
        for k in ("XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME", "XDG_CONFIG_HOME"):
            self.assertTrue(sets[k].startswith("/home/u/"), k)
        self.assertEqual(sets["DISPLAY"], ":0")

    def test_menu_builder_override_is_in_the_plan(self):
        self.assertEqual(self.plan.env_overrides["WINEDLLOVERRIDES"], "winemenubuilder.exe=d")
        self.assertIn("WINEPREFIX", self.plan.env_overrides)
        self.assertTrue(self.plan.portable)

    def test_x_authority_and_wayland_only_when_present(self):
        env = dict(ENV, XAUTHORITY="/home/u/.Xauth", WAYLAND_DISPLAY="wayland-0",
                   XDG_RUNTIME_DIR="/run/user/1000")
        with fake_fs(("/tmp/.X11-unix", "/run/user", "/run/dbus", "/home/u/.Xauth",
                      "/run/user/1000/wayland-0")):
            a = build_portable_argv(self.plan, BWRAP, self.installer, env)
        ro = flag_pairs(a, "--ro-bind")
        self.assertIn(("/home/u/.Xauth", "/tmp/.gitfurl-xauthority"), ro)
        self.assertIn(("/run/user/1000/wayland-0", "/run/user/1000/wayland-0"), ro)
        self.assertEqual(dict(flag_pairs(a, "--setenv"))["XDG_RUNTIME_DIR"], "/run/user/1000")
        a = self.argv()  # no Wayland variables
        self.assertNotIn("WAYLAND_DISPLAY", " ".join(a))
        self.assertNotIn("XDG_RUNTIME_DIR", dict(flag_pairs(a, "--setenv")))

    def test_environment_is_cleared_and_only_the_allowlist_is_set(self):
        env = dict(ENV, PATH="/usr/bin", LANG="C.UTF-8", LC_TIME="C", TZ="UTC",
                   GITHUB_TOKEN="ghp_secret", AWS_SECRET_ACCESS_KEY="aws_secret",
                   SSH_AUTH_SOCK="/run/user/1/ssh", KRB5CCNAME="FILE:/tmp/k",
                   GPG_AGENT_INFO="/x", WINEARCH="win32", WINEDEBUG="+all")
        with fake_fs():
            a = build_portable_argv(self.plan, BWRAP, self.installer, env,
                                    {**self.plan.env_overrides, "WINEDEBUG": "-all"})
        clear = a.index("--clearenv")
        self.assertLess(clear, a.index("--setenv"))  # cleared before anything is set
        sets = dict(flag_pairs(a, "--setenv"))
        joined = " ".join(a)
        for secret in ("ghp_secret", "aws_secret", "/run/user/1/ssh", "FILE:/tmp/k",
                       "GITHUB_TOKEN", "AWS_SECRET", "SSH_AUTH_SOCK", "KRB5CCNAME",
                       "GPG_AGENT_INFO", "WINEARCH", "+all"):
            self.assertNotIn(secret, joined)
        self.assertEqual(sets["PATH"], "/usr/bin")
        self.assertEqual((sets["LANG"], sets["LC_TIME"], sets["TZ"]), ("C.UTF-8", "C", "UTC"))
        self.assertEqual(sets["WINEPREFIX"], str(self.plan.prefix))
        self.assertEqual(sets["WINEDLLOVERRIDES"], "winemenubuilder.exe=d")
        self.assertEqual(sets["WINEDEBUG"], "-all")  # the plan's value, not the shell's
        self.assertEqual(set(sets), {"PATH", "LANG", "LC_TIME", "TZ", "DISPLAY", "HOME",
                                     "XDG_CACHE_HOME", "XDG_DATA_HOME", "XDG_STATE_HOME",
                                     "XDG_CONFIG_HOME", "TMPDIR", "WINEPREFIX",
                                     "WINEDLLOVERRIDES", "WINEDEBUG"})

    def test_home_must_be_a_clean_absolute_path(self):
        for home in ("//home/u", "/home/u/../u", "/home//u", "home/u", "/", "", "/home/./u"):
            with self.subTest(home=home), fake_fs(), self.assertRaises(ValueError):
                build_portable_argv(self.plan, BWRAP, self.installer, dict(ENV, HOME=home))
        with fake_fs():  # a trailing slash is the same folder
            a = build_portable_argv(self.plan, BWRAP, self.installer, dict(ENV, HOME="/home/u/"))
        self.assertEqual(dict(flag_pairs(a, "--setenv"))["HOME"], "/home/u")

    def test_symlinked_home_hides_the_real_folder(self):
        link = {"/home/u": "/var/home/u"}
        with fake_fs(), mock.patch("os.path.realpath", lambda x: link.get(x, x)):
            a = build_portable_argv(self.plan, BWRAP, self.installer, ENV)
        self.assertIn(("/var/home/u",), flag_pairs(a, "--tmpfs", 1))
        self.assertEqual(dict(flag_pairs(a, "--setenv"))["HOME"], "/home/u")
        p = replace(self.plan, argv=["/var/home/u/bin/wine", str(self.installer)])
        with fake_fs(), mock.patch("os.path.realpath", lambda x: link.get(x, x)), \
                self.assertRaises(ValueError):
            build_portable_argv(p, BWRAP, self.installer, ENV)

    def test_dash_leading_installer_name_comes_after_the_separator(self):
        odd = self.installer.parent / "--bind"
        p = replace(self.plan, argv=["/usr/bin/wine", str(odd)])
        with fake_fs():
            a = build_portable_argv(p, BWRAP, odd, ENV)
        sep = a.index("--")
        self.assertEqual(a[sep + 1:], ["/usr/bin/wine", str(odd)])
        self.assertNotIn(str(odd), a[:sep])

    def test_no_display_is_stated_not_hidden(self):
        with fake_fs():
            lines = describe_portable(self.plan, BWRAP, self.installer, {"HOME": "/home/u"})
        self.assertTrue(any("window cannot be shown" in x for x in lines))

    def test_installer_folder_containing_home_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            build_portable_argv(self.plan, BWRAP, Path("/home/setup.exe"), {"HOME": "/home/u"})
        self.assertIn("home folder", str(cm.exception))

    def test_prefix_nested_in_installer_folder_stays_writable_last(self):
        inner = replace(self.plan, prefix=self.installer.parent / "pfx")
        with fake_fs():
            a = build_portable_argv(inner, BWRAP, self.installer, ENV)
        outer, deep = str(self.installer.parent), str(inner.prefix)
        self.assertLess(a.index(outer), a.index(deep, a.index(outer) + 1))


class RefusalTests(unittest.TestCase):
    def setUp(self):
        self._d = tempfile.TemporaryDirectory()
        self.installer, self.plan = make_plan(self._d.name)

    def tearDown(self):
        self._d.cleanup()

    def test_default_is_not_portable(self):
        plain = build_plan(self.installer, "p1")
        self.assertFalse(plain.portable)
        # The menu builder is off for every run now, portable or not.
        self.assertEqual(plain.env_overrides["WINEDLLOVERRIDES"], "winemenubuilder.exe=d")
        args = cli.build_parser().parse_args(["install", "x.exe"])
        self.assertFalse(args.portable)

    def test_no_bwrap_refuses_without_fallback(self):
        with self.assertRaises(ValueError) as cm:
            check_portable_supported(self.plan, None)
        self.assertIn("bwrap", str(cm.exception))
        self.assertIn("will not fall back", str(cm.exception))

    def test_proton_refused(self):
        p = replace(self.plan, runtime="proton")
        with self.assertRaises(ValueError) as cm:
            check_portable_supported(p, BWRAP)
        self.assertIn("wine runtime", str(cm.exception))

    def test_root_refused(self):
        with mock.patch("gitfurl.portable.running_as_root", return_value=True), \
                self.assertRaises(ValueError) as cm:
            check_portable_supported(self.plan, BWRAP)
        self.assertIn("root", str(cm.exception))

    def test_wine_inside_home_is_refused(self):
        p = replace(self.plan, argv=["/home/u/bin/wine", str(self.installer)])
        with mock.patch("os.path.realpath", lambda x: x), self.assertRaises(ValueError) as cm:
            check_portable_supported(p, BWRAP, ENV, self.installer)
        self.assertIn("inside your home folder", str(cm.exception))

    def test_wine_symlink_in_home_is_resolved(self):
        p = replace(self.plan, argv=["/home/u/.nix-profile/bin/wine", str(self.installer)])
        link = {"/home/u/.nix-profile/bin/wine": "/nix/store/x/bin/wine"}
        with fake_fs(), mock.patch("os.path.realpath", lambda x: link.get(x, x)):
            a = build_portable_argv(p, BWRAP, self.installer, ENV)
        self.assertEqual(a[a.index("--") + 1], "/nix/store/x/bin/wine")


class CliTests(unittest.TestCase):
    def setUp(self):
        self._d, self._data, self._state = (tempfile.TemporaryDirectory() for _ in range(3))
        self.f = Path(self._d.name) / "setup.exe"
        self.f.write_bytes(b"")
        self.env = {"XDG_DATA_HOME": self._data.name, "XDG_STATE_HOME": self._state.name}

    def tearDown(self):
        for d in (self._d, self._data, self._state):
            d.cleanup()

    def test_dry_run_prints_bwrap_argv_and_creates_nothing(self):
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("gitfurl.portable.find_bwrap", return_value=BWRAP), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            code, out, _ = run_cli("install", str(self.f), "--dry-run", "--portable",
                                   "--prefix", "p1")
        self.assertEqual(code, 0)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertIn("bwrap command: /usr/bin/bwrap --die-with-parent", out)
        self.assertIn("--unshare-net", out)
        self.assertIn("WINEDLLOVERRIDES=winemenubuilder.exe=d", out)
        self.assertIn("not a guarantee", out)
        self.assertEqual(list(Path(self._data.name).iterdir()), [])
        self.assertEqual(list(Path(self._state.name).iterdir()), [])

    def test_dry_run_without_bwrap_is_an_error(self):
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("gitfurl.portable.find_bwrap", return_value=None):
            code, _, err = run_cli("install", str(self.f), "--dry-run", "--portable")
        self.assertEqual(code, 1)
        self.assertIn("bwrap", err)

    def test_proton_with_portable_is_refused(self):
        with mock.patch.dict("os.environ", self.env):
            code, _, err = run_cli("install", str(self.f), "--dry-run", "--portable",
                                   "--runtime", "proton", "--proton-path", "/nonexistent")
        self.assertEqual(code, 1)
        self.assertTrue(err)

    def test_approval_text_states_what_is_and_is_not_isolated(self):
        (Path(self._data.name) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.portable.find_bwrap", return_value=BWRAP), \
                mock.patch("sys.stdin.isatty", return_value=False), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            code, out, _ = run_cli("install", "--no-memory-limit", str(self.f), "--prefix", "p1", "--portable")
        self.assertEqual(code, 2)  # not approved
        run.assert_not_called()
        popen.assert_not_called()
        for needle in ("Network: off", "Writable: the dedicated prefix", "not a guarantee",
                       "not a security boundary", "wineserver -k is not run",
                       "can use that connection", "the run's log file", "/dev/dri",
                       "X authority cookie", "Environment: cleared",
                       "Delete the prefix if you do not trust the installer",
                       "Other host sockets may stay reachable"):
            self.assertIn(needle, out)
        self.assertIn(LIMITS, out)

    def test_real_run_without_bwrap_runs_nothing(self):
        (Path(self._data.name) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.portable.find_bwrap", return_value=None), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            code, _, err = run_cli("install", "--no-memory-limit", str(self.f), "--prefix", "p1", "--portable", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Nothing was run", err)
        run.assert_not_called()
        popen.assert_not_called()
        self.assertEqual(list(Path(self._state.name).iterdir()), [])


def script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


class ExecuteTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name)
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.rec = self.t / "bwrap.args"
        self.server_rec = self.t / "wineserver.args"
        script(self.bin / "wineserver", f'echo "$@" > "{self.server_rec}"\n')
        self.wine = script(self.bin / "wine", "exit 0\n")
        self.home = self.t / "home"
        self.home.mkdir()
        self.installer, self.plan = make_plan(self.t, wine=self.wine)
        self.plan.prefix.mkdir(parents=True)
        self.env = {"PATH": str(self.bin), "HOME": str(self.home),
                    "XDG_STATE_HOME": str(self.t / "state")}

    def tearDown(self):
        self._t.cleanup()

    def test_wine_argv_is_wrapped_and_no_server_kill_after_a_normal_run(self):
        script(self.bin / "bwrap", f'printf "%s\\n" "$@" > "{self.rec}"\nexit 0\n')
        status, log = execute(self.plan, self.wine, self.env)
        self.assertEqual(status, 0)
        got = self.rec.read_text().splitlines()
        self.assertIn("--unshare-net", got)
        self.assertEqual(got[got.index("--") + 1:], [self.wine, str(self.installer)])
        self.assertIn("bwrap", Path(log).read_text().splitlines()[0])
        self.assertFalse(self.server_rec.exists())

    def test_stop_ends_the_group_and_does_not_run_wineserver_k(self):
        script(self.bin / "bwrap", f'printf "%s\\n" "$@" > "{self.rec}"\nexec {shutil.which("sleep")} 60\n')
        control = RunControl(grace_s=2)
        box = {}

        def go():
            box["r"] = execute(self.plan, self.wine, self.env,
                               options=RunOptions(control=control))

        th = threading.Thread(target=go)
        th.start()
        deadline = time.monotonic() + 20
        while not self.rec.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertTrue(self.rec.exists())
        time.sleep(0.2)
        control.request_stop()
        th.join(30)
        self.assertFalse(th.is_alive())
        self.assertTrue(control.cancelled)
        self.assertEqual(box["r"][0], 130)
        self.assertFalse(self.server_rec.exists())  # a normal wine run would have run it
        self.assertTrue(any("wineserver -k was not run" in n for n in control.notes))

    def test_missing_bwrap_refuses_before_running_anything(self):
        env = dict(self.env, PATH=str(self.t / "empty"))
        with self.assertRaises(ValueError):
            execute(self.plan, self.wine, env)
        self.assertEqual(list((self.t / "state").glob("**/*.log")) if (self.t / "state").exists()
                         else [], [])

    def test_non_portable_run_is_unchanged(self):
        plain = replace(self.plan, portable=False)
        seen = {}

        def fake(argv, **kw):
            seen["argv"] = argv
            return subprocess.CompletedProcess(argv, 0)

        execute(plain, self.wine, self.env, run=fake)
        self.assertEqual(seen["argv"], [self.wine, str(self.installer)])


class GuiTests(unittest.TestCase):
    def test_option_state_reasons(self):
        from gitfurl.gui import portable_option_state

        self.assertTrue(portable_option_state("wine", BWRAP)[0])
        ok, why = portable_option_state("wine", None)
        self.assertFalse(ok)
        self.assertIn("bwrap", why)
        ok, why = portable_option_state("proton", BWRAP)
        self.assertFalse(ok)
        self.assertIn("proton", why)

    def test_describe_plan_default_has_no_portable_text(self):
        from gitfurl.gui import describe_plan

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}):
                text, err = describe_plan(str(f), "p1")
        self.assertIsNone(err)
        self.assertNotIn("PORTABLE", text)
        self.assertIn("menu builder is off", text)  # every run, not only portable ones

    def test_describe_plan_portable_shows_limits_and_override(self):
        from gitfurl.gui import describe_plan

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "HOME": "/home/u"}), \
                    mock.patch("gitfurl.portable.find_bwrap", return_value=BWRAP):
                text, err = describe_plan(str(f), "p1", portable=True)
                self.assertIsNone(err)
                self.assertIn("WINEDLLOVERRIDES=winemenubuilder.exe=d", text)
                self.assertIn(LIMITS, text)
                self.assertIn("Network: off", text)
                self.assertEqual(list(Path(data).iterdir()), [])
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "HOME": "/home/u"}), \
                    mock.patch("gitfurl.portable.find_bwrap", return_value=None):
                text, err = describe_plan(str(f), "p1", portable=True)
        self.assertEqual(text, "")
        self.assertIn("bwrap", err)

    def test_switch_default_off_and_disabled_switch_does_not_toggle(self):
        if shutil.which("xvfb-run") is None:
            self.skipTest("xvfb-run not available")
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        code = (
            "import tkinter as tk\n"
            "from gitfurl import widgets as w\n"
            "root = tk.Tk()\n"
            "v = tk.BooleanVar(value=False)\n"
            "s = w.Switch(root, v)\n"
            "assert v.get() is False\n"
            "s.set_enabled(False)\n"
            "s._toggle()\n"
            "assert v.get() is False, 'disabled switch toggled'\n"
            "s.set_enabled(True)\n"
            "s._toggle()\n"
            "assert v.get() is True\n"
            "print('ok')\n"
        )
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
        proc = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertIn("ok", proc.stdout)


if __name__ == "__main__":
    unittest.main()
