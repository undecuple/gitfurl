import io
import contextlib
import os
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

from gitfurl import cli, doctor
from gitfurl.prefixes import prefix_path, validate_prefix_name


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def patched_runs(result=None):
    """One mock for both subprocess.run (prefix creation) and the installer launcher."""
    run = mock.Mock(return_value=result or subprocess.CompletedProcess([], 0))
    with mock.patch("subprocess.run", run), mock.patch("gitfurl.runner._run_in_group", run):
        yield run


class DoctorTests(unittest.TestCase):
    def test_missing_wine(self):
        with mock.patch("shutil.which", return_value=None):
            r = doctor.probe_tool("wine", ["--version"])
        self.assertEqual(r.problem, "not found on PATH")

    def test_version_timeout(self):
        with mock.patch("shutil.which", return_value="/x/wine"), mock.patch(
            "subprocess.run", side_effect=subprocess.TimeoutExpired("wine", 5)
        ):
            r = doctor.probe_tool("wine", ["--version"])
        self.assertIn("timed out", r.problem)

    def test_version_ok(self):
        done = subprocess.CompletedProcess([], 0, stdout="wine-9.0\n", stderr="")
        with mock.patch("shutil.which", return_value="/x/wine"), mock.patch(
            "subprocess.run", return_value=done
        ):
            r = doctor.probe_tool("wine", ["--version"])
        self.assertEqual(r.version, "wine-9.0")

    def test_unexpected_output(self):
        done = subprocess.CompletedProcess([], 1, stdout="", stderr="")
        with mock.patch("shutil.which", return_value="/x/wine"), mock.patch(
            "subprocess.run", return_value=done
        ):
            r = doctor.probe_tool("wine", ["--version"])
        self.assertIn("unexpected", r.problem)


class InstallDryRunTests(unittest.TestCase):
    def test_missing_installer(self):
        code, _, err = run_cli("install", "/nonexistent/setup.exe", "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("not found", err)

    def test_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            code, _, err = run_cli("install", d, "--dry-run")
        self.assertEqual(code, 1)
        self.assertIn("regular file", err)

    def test_spaces_and_non_ascii(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "my set up ü.exe"
            f.write_bytes(b"")
            with mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
                code, out, _ = run_cli("install", str(f), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("ü", out)
        run.assert_not_called()
        popen.assert_not_called()

    def test_dry_run_creates_nothing(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}):
                code, _, _ = run_cli("install", str(f), "--dry-run", "--prefix", "p1")
            self.assertEqual(code, 0)
            self.assertEqual(list(Path(data).iterdir()), [])
            self.assertEqual([p.name for p in Path(d).iterdir()], ["setup.exe"])

    def test_bad_prefix_name_reported(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            code, _, err = run_cli("install", str(f), "--dry-run", "--prefix", "../x")
        self.assertEqual(code, 1)
        self.assertIn("invalid prefix name", err)


class InstallRunTests(unittest.TestCase):
    def _setup(self, d, data, state):
        f = Path(d) / "set up ü.exe"
        f.write_bytes(b"")
        (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
        return f

    def test_requires_explicit_prefix(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            with mock.patch("subprocess.run") as run:
                code, _, err = run_cli("install", str(f), "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--prefix", err)
        run.assert_not_called()

    def test_not_approved_without_tty(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f = self._setup(d, data, state)
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("sys.stdin.isatty", return_value=False), \
                    mock.patch("subprocess.run") as run:
                code, _, _ = run_cli("install", str(f), "--prefix", "p1")
            self.assertEqual(list(Path(state).iterdir()), [])
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_missing_prefix_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("subprocess.run") as run:
                code, _, err = run_cli("install", str(f), "--prefix", "nope", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        run.assert_not_called()

    def test_approved_run_uses_argv_log_and_exit_status(self):
        done = subprocess.CompletedProcess([], 3)
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f = self._setup(d, data, state)
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state,
                                                "SECRET_TOKEN": "hunter2"}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    patched_runs(done) as run:
                code, out, _ = run_cli("install", str(f), "--prefix", "p1", "--yes")
            logs = list((Path(state) / "gitfurl" / "logs").iterdir())
            self.assertEqual(len(logs), 1)
            self.assertEqual(logs[0].stat().st_mode & 0o777, 0o600)
            text = logs[0].read_text()
            self.assertIn("exit status 3", text)
            self.assertNotIn("hunter2", text)
        self.assertEqual(code, 1)
        self.assertIn("status 3", out)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/x/wine", str(f.resolve())])
        kw = run.call_args.kwargs
        self.assertNotIn("shell", kw)
        self.assertEqual(kw["env"]["WINEPREFIX"].rsplit("/", 1)[-1], "p1")


class PrefixCreateTests(unittest.TestCase):
    def test_no_wine_changes_nothing(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": data}
        ), mock.patch("shutil.which", return_value=None):
            code, _, err = run_cli("prefix", "create", "abc", "--yes")
            self.assertEqual(list(Path(data).iterdir()), [])
        self.assertEqual(code, 1)
        self.assertIn("wine not found", err)

    def test_not_approved_without_tty(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": data}
        ), mock.patch("shutil.which", return_value="/x/wine"), mock.patch(
            "sys.stdin.isatty", return_value=False
        ), mock.patch("subprocess.run") as run:
            code, _, _ = run_cli("prefix", "create", "abc")
            self.assertEqual(list(Path(data).iterdir()), [])
        self.assertEqual(code, 2)
        run.assert_not_called()

    def test_create_runs_argv_with_scoped_env(self):
        done = subprocess.CompletedProcess([], 0)
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": data}
        ), mock.patch("shutil.which", return_value="/x/wine"), mock.patch(
            "subprocess.run", return_value=done
        ) as run:
            code, _, _ = run_cli("prefix", "create", "abc", "--yes")
            target = Path(data) / "gitfurl" / "prefixes" / "abc"
            self.assertTrue(target.is_dir())
            self.assertEqual(target.stat().st_mode & 0o777, 0o700)
            import os
            self.assertNotIn("WINEPREFIX", os.environ)
        self.assertEqual(code, 0)
        argv = run.call_args.args[0]
        self.assertEqual(argv, ["/x/wine", "wineboot", "--init"])
        self.assertEqual(run.call_args.kwargs["env"]["WINEPREFIX"], str(target))
        self.assertNotIn("shell", run.call_args.kwargs)

    def test_existing_path_is_not_touched(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": data}
        ), mock.patch("shutil.which", return_value="/x/wine"), mock.patch(
            "subprocess.run"
        ) as run:
            existing = Path(data) / "gitfurl" / "prefixes" / "abc"
            existing.mkdir(parents=True)
            (existing / "keep").write_text("x")
            code, _, _ = run_cli("prefix", "create", "abc", "--yes")
            self.assertEqual((existing / "keep").read_text(), "x")
        self.assertEqual(code, 1)
        run.assert_not_called()

    def test_traversal_name_rejected_before_anything(self):
        code, _, _ = run_cli("prefix", "create", "../evil", "--yes")
        self.assertEqual(code, 1)

    def test_list(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": data}
        ):
            (Path(data) / "gitfurl" / "prefixes" / "one").mkdir(parents=True)
            code, out, _ = run_cli("prefix", "list")
        self.assertEqual(code, 0)
        self.assertIn("one", out)


class PrefixTests(unittest.TestCase):
    def test_invalid_names(self):
        for bad in ["", "..", "../x", "a/b", ".hidden", "a b", "x" * 65, "/abs"]:
            with self.assertRaises(ValueError, msg=bad):
                validate_prefix_name(bad)

    def test_valid_name_stays_under_root(self):
        p = prefix_path("good-1", {"XDG_DATA_HOME": "/data"})
        self.assertEqual(str(p), "/data/gitfurl/prefixes/good-1")

    def test_never_defaults_to_dot_wine(self):
        p = prefix_path("default", {"HOME": "/home/u"})
        self.assertNotIn(".wine", p.parts)


if __name__ == "__main__":
    unittest.main()


class GuiLogicTests(unittest.TestCase):
    def test_plan_needs_installer_and_prefix(self):
        from gitfurl.gui import describe_plan

        self.assertTrue(describe_plan("", "p")[1])
        self.assertTrue(describe_plan("/x", "")[1])

    def test_plan_preview_has_no_side_effects(self):
        from gitfurl.gui import describe_plan

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            f = Path(d) / "set up ü.exe"
            f.write_bytes(b"")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}), \
                    mock.patch("subprocess.run") as run:
                text, err = describe_plan(str(f), "p1")
            self.assertIsNone(err)
            self.assertIn("WINEPREFIX", text)
            self.assertEqual(list(Path(data).iterdir()), [])
            run.assert_not_called()

    def test_bad_prefix_name_is_an_error(self):
        from gitfurl.gui import describe_plan

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            self.assertIn("invalid prefix name", describe_plan(str(f), "../x")[1])


class AppEntryTests(unittest.TestCase):
    def test_no_args_opens_gui(self):
        from gitfurl import app

        with mock.patch("gitfurl.gui.main", return_value=0) as gui:
            self.assertEqual(app.main([]), 0)
        gui.assert_called_once()

    def test_cli_is_opt_in(self):
        from gitfurl import app

        with mock.patch("gitfurl.gui.main") as gui, mock.patch("gitfurl.cli.main", return_value=0) as c:
            self.assertEqual(app.main(["cli", "doctor"]), 0)
        c.assert_called_once_with(["doctor"])
        gui.assert_not_called()

    def test_bare_command_does_not_run_cli(self):
        from gitfurl import app

        with mock.patch("gitfurl.cli.main") as c, mock.patch("gitfurl.gui.main") as gui:
            code = app.main(["doctor"])
        self.assertEqual(code, 2)
        c.assert_not_called()
        gui.assert_not_called()


class CreateIfMissingTests(unittest.TestCase):
    def _env(self, d, data, state):
        f = Path(d) / "setup.exe"
        f.write_bytes(b"")
        return f, mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state})

    def test_cli_creates_prefix_then_runs(self):
        done = subprocess.CompletedProcess([], 0)
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f, env = self._env(d, data, state)
            with env, mock.patch("shutil.which", return_value="/x/wine"), \
                    patched_runs(done) as run:
                code, out, _ = run_cli("install", str(f), "--prefix", "new", "--create-prefix", "--yes")
            self.assertTrue((Path(data) / "gitfurl" / "prefixes" / "new").is_dir())
        self.assertEqual(code, 0)
        self.assertIn("will be created first", out)
        argvs = [c.args[0] for c in run.call_args_list]
        self.assertEqual(argvs[0], ["/x/wine", "wineboot", "--init"])
        self.assertEqual(argvs[1], ["/x/wine", str(f.resolve())])

    def test_installer_not_run_if_prefix_creation_fails(self):
        done = subprocess.CompletedProcess([], 5)
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f, env = self._env(d, data, state)
            with env, mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("subprocess.run", return_value=done) as run:
                code, _, err = run_cli("install", str(f), "--prefix", "new", "--create-prefix", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("installer was not run", err)
        self.assertEqual(run.call_count, 1)

    def test_without_flag_still_refuses_missing_prefix(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f, env = self._env(d, data, state)
            with env, mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("subprocess.run") as run:
                code, _, err = run_cli("install", str(f), "--prefix", "new", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("--create-prefix", err)
        run.assert_not_called()

    def test_run_with_prefix_respects_create_flag(self):
        from gitfurl.runner import build_plan, run_with_prefix

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"")
            plan = build_plan(f, "x", {"XDG_DATA_HOME": data})
            with self.assertRaises(FileNotFoundError):
                run_with_prefix(plan, "/x/wine", False, {"XDG_DATA_HOME": data})


class CleanupTests(unittest.TestCase):
    def _folder(self, d):
        f = Path(d) / "inst"
        (f / "MD5").mkdir(parents=True)
        (f / "setup.exe").write_bytes(b"x")
        (f / "a.bin").write_bytes(b"x")
        (f / "MD5" / "s.md5").write_bytes(b"x")
        return f

    def test_default_none_deletes_nothing(self):
        from gitfurl.cleanup import perform_cleanup, plan_cleanup

        with tempfile.TemporaryDirectory() as d:
            f = self._folder(d)
            plan = plan_cleanup(f / "setup.exe", "none")
            self.assertEqual(perform_cleanup(plan, 0), (0, []))
            self.assertTrue((f / "setup.exe").exists())

    def test_installer_mode_deletes_only_the_file(self):
        from gitfurl.cleanup import perform_cleanup, plan_cleanup

        with tempfile.TemporaryDirectory() as d:
            f = self._folder(d)
            perform_cleanup(plan_cleanup(f / "setup.exe", "installer"), 0)
            self.assertFalse((f / "setup.exe").exists())
            self.assertTrue((f / "a.bin").exists())

    def test_folder_mode_removes_folder(self):
        from gitfurl.cleanup import perform_cleanup, plan_cleanup

        with tempfile.TemporaryDirectory() as d:
            f = self._folder(d)
            n, problems = perform_cleanup(plan_cleanup(f / "setup.exe", "folder"), 0)
            self.assertEqual((n, problems), (3, []))
            self.assertFalse(f.exists())

    def test_nonzero_status_deletes_nothing(self):
        from gitfurl.cleanup import perform_cleanup, plan_cleanup

        with tempfile.TemporaryDirectory() as d:
            f = self._folder(d)
            self.assertEqual(perform_cleanup(plan_cleanup(f / "setup.exe", "folder"), 1), (0, []))
            self.assertTrue((f / "a.bin").exists())

    def test_symlink_is_unlinked_not_followed(self):
        from gitfurl.cleanup import perform_cleanup, plan_cleanup

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            f = self._folder(d)
            precious = Path(other) / "keep.txt"
            precious.write_text("keep")
            (f / "link").symlink_to(precious)
            (f / "dirlink").symlink_to(other)
            perform_cleanup(plan_cleanup(f / "setup.exe", "folder"), 0)
            self.assertTrue(precious.exists())
            self.assertFalse(f.exists())

    def test_refuses_home_and_prefix_folders(self):
        from gitfurl.cleanup import plan_cleanup

        with tempfile.TemporaryDirectory() as d:
            f = self._folder(d)
            with mock.patch.dict("os.environ", {"HOME": str(f)}), self.assertRaises(ValueError):
                plan_cleanup(f / "setup.exe", "folder")
            with self.assertRaises(ValueError):
                plan_cleanup(f / "setup.exe", "folder", protect=[f / "MD5"])
            for mode in ("folder", "installer"):
                with self.assertRaises(ValueError):
                    plan_cleanup(Path("/usr/bin/true"), mode)

    def test_cli_cleanup_after_success_only(self):
        done0, done3 = subprocess.CompletedProcess([], 0), subprocess.CompletedProcess([], 3)
        for done, should_exist in ((done0, False), (done3, True)):
            with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                    tempfile.TemporaryDirectory() as state:
                (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
                f = Path(d) / "setup.exe"
                f.write_bytes(b"x")
                with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                        mock.patch("shutil.which", return_value="/x/wine"), \
                        patched_runs(done):
                    run_cli("install", str(f), "--prefix", "p1", "--yes", "--cleanup", "installer")
                self.assertEqual(f.exists(), should_exist)


class ChangesTests(unittest.TestCase):
    def test_reports_prefix_host_and_registry_changes_without_contents(self):
        from gitfurl.changes import compare, take_snapshot

        with tempfile.TemporaryDirectory() as d:
            home, prefix = Path(d) / "home", Path(d) / "prefix"
            (home / ".local" / "share" / "applications").mkdir(parents=True)
            (prefix / "drive_c").mkdir(parents=True)
            (prefix / "user.reg").write_text("[Software\\\\Old] 1\n")
            before = take_snapshot(prefix, home=home)
            (prefix / "drive_c" / "game.exe").write_text("secret contents")
            (prefix / "user.reg").write_text("[Software\\\\Old] 1\n[Software\\\\NewVendor] 2\n")
            (home / ".local" / "share" / "applications" / "game.desktop").write_text("x")
            report = compare(before, take_snapshot(prefix, home=home))
            text = report.describe()
            self.assertTrue(any(p.endswith("game.exe") for p in report.prefix_new))
            self.assertTrue(any(p.endswith("game.desktop") for p in report.host_new))
            self.assertTrue(any("NewVendor" in k for k in report.keys_new))
            self.assertNotIn("secret contents", text)

    def test_no_host_change_is_stated(self):
        from gitfurl.changes import compare, take_snapshot

        with tempfile.TemporaryDirectory() as d:
            home, prefix = Path(d) / "home", Path(d) / "prefix"
            home.mkdir()
            prefix.mkdir()
            s = take_snapshot(prefix, home=home)
            self.assertIn("No changes found outside", compare(s, take_snapshot(prefix, home=home)).describe())

    def test_cli_show_changes_is_opt_in(self):
        done = subprocess.CompletedProcess([], 0)
        outs = {}
        for flag in ([], ["--show-changes"]):
            with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                    tempfile.TemporaryDirectory() as state:
                (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
                f = Path(d) / "setup.exe"
                f.write_bytes(b"x")
                with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                        mock.patch("shutil.which", return_value="/x/wine"), \
                        patched_runs(done):
                    outs[bool(flag)] = run_cli("install", str(f), "--prefix", "p1", "--yes", *flag)[1]
        self.assertNotIn("registry keys", outs[False])
        self.assertIn("registry keys", outs[True])


class ProgressTests(unittest.TestCase):
    def _fake_proc(self, root, pid, target, pos):
        (root / str(pid) / "fd").mkdir(parents=True)
        (root / str(pid) / "fdinfo").mkdir(parents=True)
        (root / str(pid) / "fd" / "7").symlink_to(target)
        (root / str(pid) / "fdinfo" / "7").write_text(f"pos:\t{pos}\nflags:\t0100000\n")

    def test_payload_files_skip_installer_and_small_files(self):
        from gitfurl.progress import payload_files

        with tempfile.TemporaryDirectory() as d:
            setup = Path(d) / "setup.exe"
            setup.write_bytes(b"x" * (2 << 20))
            (Path(d) / "a.bin").write_bytes(b"x" * (2 << 20))
            (Path(d) / "note.txt").write_bytes(b"x")
            self.assertEqual([p.name for p in payload_files(setup)], ["a.bin"])

    def test_read_positions_from_fake_proc(self):
        from gitfurl.progress import read_positions

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as proc:
            f = Path(d) / "a.bin"
            f.write_bytes(b"x")
            self._fake_proc(Path(proc), 123, f, 5000)
            self._fake_proc(Path(proc), 456, f, 9000)  # highest wins
            self.assertEqual(read_positions([f], proc), {str(f): 9000})
            self.assertEqual(read_positions([Path(d) / "other.bin"], proc), {})

    def test_sample_percent_eta_and_line(self):
        from gitfurl.progress import Sample

        s = Sample(elapsed_s=100, read_bytes=25_000_000_000, total_bytes=100_000_000_000,
                   written_bytes=10_000_000_000, rate_bps=250e6)
        self.assertAlmostEqual(s.percent, 25.0)
        self.assertAlmostEqual(s.eta_s, 300.0)
        self.assertIn("25.0%", s.line())
        self.assertIn("estimate", s.line())
        early = Sample(5, 10, 100_000_000_000, 0, 1e6)
        self.assertIsNone(early.eta_s)
        self.assertIn("no ETA yet", early.line())
        self.assertIn("no payload files", Sample(1, 0, 0, 0, 0).line())

    def test_run_reports_progress_and_writes_event_log_without_env_values(self):
        from gitfurl.runner import RunOptions, build_plan, execute

        samples = []

        def slow_run(argv, **kw):
            import time

            time.sleep(0.5)
            return subprocess.CompletedProcess(argv, 0)

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            (Path(d) / "p.bin").write_bytes(b"x" * (2 << 20))
            env = {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state, "SECRET_TOKEN": "hunter2"}
            plan = build_plan(f.resolve(), "p", env)
            plan.prefix.mkdir(parents=True)
            status, log = execute(plan, "/x/wine", env, slow_run,
                                  RunOptions(on_progress=samples.append, progress_interval=0.1,
                                             detailed_log=True, wine_debug="err+all"))
            events = Path(log).with_suffix(".events.jsonl")
            text = events.read_text()
            self.assertEqual(events.stat().st_mode & 0o777, 0o600)
        self.assertEqual(status, 0)
        self.assertTrue(samples)
        self.assertNotIn("hunter2", text)
        kinds = [__import__("json").loads(line)["event"] for line in text.splitlines()]
        self.assertEqual(kinds[0], "start")
        self.assertEqual(kinds[-1], "exit")
        self.assertIn("progress", kinds)
        self.assertIn("WINEDEBUG", text)

    def test_wine_debug_value_is_validated(self):
        from gitfurl.runner import validate_wine_debug

        self.assertEqual(validate_wine_debug("err+all,fixme-all"), "err+all,fixme-all")
        for bad in ["", "a b", "x;rm -rf", "$(id)", "a" * 201]:
            with self.assertRaises(ValueError):
                validate_wine_debug(bad)

    def test_off_by_default_no_event_log_no_progress(self):
        done = subprocess.CompletedProcess([], 0)
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    patched_runs(done):
                _, out, _ = run_cli("install", str(f), "--prefix", "p1", "--yes")
            names = [p.name for p in (Path(state) / "gitfurl" / "logs").iterdir()]
        self.assertEqual(len(names), 1)
        self.assertTrue(names[0].endswith(".log"))
        self.assertNotIn("[progress]", out)

    def test_cli_rejects_bad_wine_debug(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("subprocess.run") as run:
                code, _, err = run_cli("install", str(f), "--prefix", "p1", "--yes",
                                       "--wine-debug", "x;y")
        self.assertEqual(code, 1)
        self.assertIn("invalid --wine-debug", err)
        run.assert_not_called()


class RuntimeAndExeTests(unittest.TestCase):
    def _proton_dir(self, d):
        p = Path(d) / "GE-Proton"
        p.mkdir()
        (p / "proton").write_text("#!/bin/sh\n")
        return p

    def test_proton_plan_uses_umu_and_env(self):
        from gitfurl.runner import build_plan

        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            pp = self._proton_dir(d)
            plan = build_plan(f.resolve(), "p", {"XDG_DATA_HOME": d}, "proton", str(pp))
        self.assertEqual(plan.argv[0], "umu-run")
        self.assertEqual(plan.env_overrides["PROTONPATH"], str(pp.resolve()))
        self.assertEqual(plan.env_overrides["GAMEID"], "umu-0")
        self.assertIn("network", " ".join(plan.prerequisites))

    def test_proton_needs_a_folder_and_never_downloads(self):
        from gitfurl.runner import validate_proton_path

        for bad in (None, "", "/nonexistent"):
            with self.assertRaises(ValueError):
                validate_proton_path(bad)
        with tempfile.TemporaryDirectory() as d, self.assertRaises(ValueError):
            validate_proton_path(d)  # a folder without a 'proton' file

    def test_unknown_runtime_rejected(self):
        from gitfurl.runner import runtime_env

        with self.assertRaises(ValueError):
            runtime_env("docker", None)

    def test_cli_proton_run_creates_prefix_with_umu_then_runs(self):
        done = subprocess.CompletedProcess([], 0)
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            pp = self._proton_dir(d)
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                    mock.patch("shutil.which", side_effect=lambda n: f"/x/{n}"), \
                    patched_runs(done) as run:
                code, _, _ = run_cli("install", str(f), "--prefix", "n", "--create-prefix", "--yes",
                                     "--runtime", "proton", "--proton-path", str(pp))
        self.assertEqual(code, 0)
        first, second = run.call_args_list[0], run.call_args_list[1]
        self.assertEqual(first.args[0], ["/x/umu-run", "wineboot", "--init"])
        self.assertEqual(first.kwargs["env"]["GAMEID"], "umu-0")
        self.assertEqual(second.args[0], ["/x/umu-run", str(f.resolve())])
        self.assertEqual(second.kwargs["env"]["PROTONPATH"], str(pp.resolve()))

    def test_cli_proton_without_path_runs_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            with mock.patch.dict("os.environ", {}, clear=False), \
                    mock.patch("subprocess.run") as run:
                os_env = __import__("os").environ
                os_env.pop("PROTONPATH", None)
                code, _, err = run_cli("install", str(f), "--dry-run", "--runtime", "proton")
        self.assertEqual(code, 1)
        self.assertIn("Proton folder", err)
        run.assert_not_called()

    def test_find_game_executables_ranks_and_filters(self):
        from gitfurl.changes import find_game_executables

        with tempfile.TemporaryDirectory() as d:
            prefix = Path(d)
            files = {
                "drive_c/Program Files/Game/game.exe": 5000,
                "drive_c/Program Files/Game/unins000.exe": 9000,
                "drive_c/Program Files/Game/UnityCrashHandler64.exe": 800,
                "drive_c/Program Files/Game/launcher.exe": 100,
                "drive_c/Program Files/Common Files/x/tool.exe": 7000,
                "drive_c/windows/system32/notepad.exe": 8000,
                "drive_c/users/u/AppData/Local/Temp/is-1/setup.tmp.exe": 9999,
                "drive_c/Program Files/Game/data.dll": 99999,
            }
            paths = []
            for rel, n in files.items():
                f = prefix / rel
                f.parent.mkdir(parents=True, exist_ok=True)
                f.write_bytes(b"x" * n)
                paths.append(str(f))
            found = find_game_executables(prefix, paths)
        self.assertEqual([Path(p).name for p, _ in found], ["game.exe", "launcher.exe"])

    def test_find_exe_is_opt_in_and_never_runs_it(self):
        for flag, expect in (([], False), (["--find-exe"], True)):
            with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                    tempfile.TemporaryDirectory() as state:
                prefix = Path(data) / "gitfurl" / "prefixes" / "p1"
                (prefix / "drive_c").mkdir(parents=True)
                f = Path(d) / "setup.exe"
                f.write_bytes(b"x")

                def fake_run(argv, prefix=prefix, **kw):
                    game = prefix / "drive_c" / "Program Files" / "G" / "g.exe"
                    game.parent.mkdir(parents=True, exist_ok=True)
                    game.write_bytes(b"x" * 100)
                    return subprocess.CompletedProcess(argv, 0)

                with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                        mock.patch("shutil.which", return_value="/x/wine"), \
                        patched_runs() as run:
                    run.side_effect = fake_run
                    _, out, _ = run_cli("install", str(f), "--prefix", "p1", "--yes", *flag)
                self.assertEqual(run.call_count, 1)  # only the installer; the program is not run
                self.assertEqual("g.exe" in out, expect)


class ReviewFixTests(unittest.TestCase):
    def test_root_is_refused_for_real_actions_only(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            with mock.patch("gitfurl.cli.running_as_root", return_value=True), \
                    mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("subprocess.run") as run:
                code1, _, err1 = run_cli("install", str(f), "--prefix", "p1", "--yes")
                code2, _, err2 = run_cli("prefix", "create", "new", "--yes")
                code3, _, _ = run_cli("install", str(f), "--prefix", "p1", "--dry-run")
                run.assert_not_called()  # refused or dry: nothing was started
                code4, _, _ = run_cli("doctor")  # read-only probes are still allowed
        self.assertEqual((code1, code2), (1, 1))
        self.assertIn("as root", err1 + err2)
        self.assertEqual((code3, code4), (0, 0))

    def test_inherited_wine_variables_are_not_passed_on(self):
        from gitfurl.prefixes import child_env

        env = child_env({"PATH": "/bin", "WINEARCH": "win32", "WINEDLLOVERRIDES": "x=n",
                         "WINEPREFIX": "/home/u/.wine"}, {"WINEPREFIX": "/p", "WINEDEBUG": "err+all"})
        self.assertEqual(env, {"PATH": "/bin", "WINEPREFIX": "/p", "WINEDEBUG": "err+all"})

    def test_cli_run_strips_inherited_wine_variables(self):
        done = subprocess.CompletedProcess([], 0)
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state,
                                                "WINEDLLOVERRIDES": "mscoree=d"}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    patched_runs(done) as run:
                run_cli("install", str(f), "--prefix", "p1", "--yes")
        self.assertNotIn("WINEDLLOVERRIDES", run.call_args.kwargs["env"])

    def test_cleanup_refuses_general_purpose_user_folders(self):
        from gitfurl.cleanup import plan_cleanup

        with tempfile.TemporaryDirectory() as home:
            for name in ("Downloads", "Desktop", "Documents"):
                folder = Path(home) / name
                folder.mkdir()
                (folder / "setup.exe").write_bytes(b"x")
                (folder / "unrelated.pdf").write_bytes(b"x")
                with mock.patch.dict("os.environ", {"HOME": home}):
                    with self.assertRaises(ValueError) as ctx:
                        plan_cleanup(folder / "setup.exe", "folder")
                    self.assertIn("general-purpose", str(ctx.exception))
                    # the single-file mode is still allowed there
                    self.assertEqual(plan_cleanup(folder / "setup.exe", "installer").files,
                                     [(folder / "setup.exe").resolve()])
            own = Path(home) / "Downloads" / "my-installer"
            own.mkdir()
            (own / "setup.exe").write_bytes(b"x")
            with mock.patch.dict("os.environ", {"HOME": home}):
                self.assertEqual(len(plan_cleanup(own / "setup.exe", "folder").files), 1)

    def test_doctor_reports_tkinter(self):
        from gitfurl import doctor

        with mock.patch("importlib.util.find_spec", return_value=None):
            r = doctor.probe_tkinter()
        self.assertIn("python3-tk", r.problem)
        self.assertIn("tkinter", doctor.format_report(
            {"platform": "x", "wine": r, "bwrap": r, "umu": r, "tkinter": r}))

    def test_version_is_the_same_everywhere(self):
        import tomllib

        import gitfurl

        data = tomllib.loads((Path(__file__).resolve().parent.parent / "pyproject.toml").read_text())
        self.assertEqual(data["project"]["version"], gitfurl.__version__)

    def test_window_opens_headless(self):
        import shutil as sh
        import sys

        if sh.which("xvfb-run") is None:
            self.skipTest("xvfb-run not available")
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        env = {**__import__("os").environ, "PYTHONPATH": str(Path(__file__).resolve().parent.parent / "src")}
        with tempfile.TemporaryDirectory() as home:
            env.update(HOME=home, XDG_DATA_HOME=home + "/d", XDG_STATE_HOME=home + "/s")
            proc = subprocess.run(["xvfb-run", "-a", "timeout", "5", sys.executable, "-m", "gitfurl"],
                                  env=env, capture_output=True, text=True, check=False, timeout=60)
        self.assertEqual(proc.returncode, 124, proc.stderr[-500:])  # still open when timeout stopped it


class StartupFailureTests(unittest.TestCase):
    def test_no_display_gives_a_message_not_a_traceback(self):
        import sys

        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        env = {k: v for k, v in __import__("os").environ.items() if k != "DISPLAY"}
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
        proc = subprocess.run([sys.executable, "-m", "gitfurl"], env=env, capture_output=True,
                              text=True, check=False, timeout=60)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not open a window", proc.stdout)
        self.assertNotIn("Traceback", proc.stderr)


class ActivityTests(unittest.TestCase):
    def _proc(self, root, pid, comm, prefix, utime, stime, env_extra=b""):
        d = Path(root) / str(pid)
        d.mkdir()
        (d / "environ").write_bytes(b"HOME=/x\0WINEPREFIX=" + str(prefix).encode() + b"\0" + env_extra)
        fields = ["S"] + ["0"] * 10 + [str(utime), str(stime)] + ["0"] * 10
        (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(fields))

    def test_counts_only_processes_in_the_prefix_and_names_helpers(self):
        from gitfurl.progress import prefix_activity

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as pre:
            self._proc(root, 10, "wineserver", pre, 5, 5)
            self._proc(root, 11, "setup.exe", pre, 100, 20)
            self._proc(root, 12, "my helper (x)", pre, 50, 30)  # name with space and parentheses
            self._proc(root, 13, "other.exe", "/elsewhere", 999, 999)
            count, names, ticks = prefix_activity(Path(pre), root)
        self.assertEqual(count, 3)
        self.assertEqual(names, ["my helper (x)", "setup.exe"])  # wineserver is infrastructure
        self.assertEqual(ticks, 10 + 120 + 80)

    def test_nothing_is_kept_from_the_environment_but_the_match(self):
        from gitfurl.progress import prefix_activity

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as pre:
            self._proc(root, 11, "setup.exe", pre, 1, 1, env_extra=b"SECRET_TOKEN=abc\0")
            result = prefix_activity(Path(pre), root)
        self.assertNotIn("abc", repr(result))
        self.assertNotIn("SECRET_TOKEN", repr(result))

    def test_sample_line_shows_cpu_and_helpers_and_a_quiet_hint(self):
        from gitfurl.progress import Sample

        busy = Sample(60, 0, 0, 0, 0.0, procs=3, cpu_pct=240.0, helpers=("decomp.exe", "setup.exe"))
        self.assertIn("3 process(es) running, CPU 240% (decomp.exe, setup.exe)", busy.line())
        self.assertNotIn("quiet", busy.line())
        quiet = Sample(60, 0, 0, 0, 0.0, procs=2, cpu_pct=0.5)
        self.assertIn("quiet: it may be waiting", quiet.line())
        self.assertNotIn("process(es)", Sample(60, 0, 0, 0, 0.0).line())

    def test_monitor_computes_cpu_between_samples(self):
        from gitfurl.progress import ProgressMonitor

        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as pre, \
                tempfile.TemporaryDirectory() as inst:
            (Path(inst) / "setup.exe").write_bytes(b"x")
            self._proc(root, 11, "setup.exe", pre, 0, 0)
            mon = ProgressMonitor(Path(inst) / "setup.exe", Path(pre), lambda s: None, proc_root=root)
            mon._t0 = 0.0
            with mock.patch("time.monotonic", side_effect=[10.0, 20.0]):
                first = mon.sample()
                self._proc(root, 12, "x", pre, 0, 0)  # a second process appears
                (Path(root) / "11" / "stat").write_text(
                    "11 (setup.exe) " + " ".join(["S"] + ["0"] * 10 + ["500", "500"] + ["0"] * 10))
                second = mon.sample()
        self.assertIsNone(first.cpu_pct)
        self.assertEqual(second.procs, 2)
        hz = os.sysconf("SC_CLK_TCK")
        self.assertAlmostEqual(second.cpu_pct, 100.0 * 1000 / hz / 10.0, places=3)

    def test_doctor_reports_machine_resources(self):
        from gitfurl.doctor import machine_resources

        with tempfile.NamedTemporaryFile("w", suffix=".meminfo") as fh:
            fh.write("MemTotal: 16000000 kB\nMemAvailable: 8388608 kB\n")
            fh.flush()
            text = machine_resources(fh.name)
        self.assertIn("8.0 GB memory available", text)
        self.assertIn("CPU threads", text)
        self.assertIn("memory unknown", machine_resources("/nonexistent/meminfo"))


DUMMY = '''
import os, signal, subprocess, sys, time
role, mode, out = sys.argv[1], sys.argv[2], sys.argv[3]
def note(name):
    with open(out, "a") as fh:
        fh.write(f"{name} {os.getpid()}\\n")
if role == "leader":
    if mode == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "fail":
        sys.exit(3)
    if mode == "quick":
        sys.exit(0)
    note("leader")
    subprocess.Popen([sys.executable, __file__, "child", mode, out])
    time.sleep(120)
elif role == "child":
    if mode == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    if mode == "escape":
        os.setsid()
    note("child")
    subprocess.Popen([sys.executable, __file__, "grandchild", mode, out])
    time.sleep(120)
else:
    if mode == "ignore":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    note("grandchild")
    time.sleep(120)
'''


def pid_alive(pid):
    try:
        with open(f"/proc/{pid}/stat") as fh:
            return fh.read().rpartition(")")[2].split()[0] != "Z"
    except OSError:
        return False


class StopTests(unittest.TestCase):
    """Controlled dummy processes only. These tests say nothing about real installers."""

    def setUp(self):
        import time

        from gitfurl.runner import InstallPlan, RunControl, RunOptions, execute

        self.time = time
        self.execute, self.RunControl, self.RunOptions = execute, RunControl, RunOptions
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        (t / "prefix").mkdir()
        (t / "dummy.py").write_text(DUMMY)
        import sys

        self.py = sys.executable
        # a fake "wine" and a fake "wineserver" beside it, so nothing real is ever started
        (t / "wine").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n')
        (t / "wineserver").write_text(
            f'#!/bin/sh\necho "$@ WINEPREFIX=$WINEPREFIX" >> "{t}/server.txt"\n')
        for n in ("wine", "wineserver"):
            (t / n).chmod(0o755)
        self.out = t / "pids.txt"
        self.plan_for = lambda mode, runtime="wine": InstallPlan(
            argv=["wine", str(t / "dummy.py"), "leader", mode, str(self.out)],
            prefix=t / "prefix", cwd=t, runtime=runtime,
            env_overrides={"WINEPREFIX": str(t / "prefix")})
        self.env = {"XDG_STATE_HOME": str(t / "state"), "HOME": str(t)}
        self.stray = []
        self.addCleanup(self._kill_strays)

    def _kill_strays(self):
        import os
        import signal

        for pid in self.stray:  # only pids this test itself recorded
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)

    def pids(self, want):
        end = self.time.monotonic() + 15
        while self.time.monotonic() < end:
            found = dict(line.split() for line in self.out.read_text().splitlines()) \
                if self.out.exists() else {}
            if all(k in found for k in want):
                got = {k: int(v) for k, v in found.items()}
                self.stray.extend(got.values())
                return got
            self.time.sleep(0.05)
        self.fail("dummy processes did not start")

    def start(self, mode, control, runtime="wine"):
        import threading

        box = {}

        def work():
            box["res"] = self.execute(self.plan_for(mode, runtime), str(Path(self.tmp.name) / "wine"),
                                      self.env, options=self.RunOptions(control=control))

        th = threading.Thread(target=work)
        th.start()
        return th, box

    def gone(self, pid):
        end = self.time.monotonic() + 5
        while self.time.monotonic() < end:
            if not pid_alive(pid):
                return True
            self.time.sleep(0.05)
        return False

    def test_stop_ends_leader_child_and_grandchild_and_reports_cancelled(self):
        control = self.RunControl(grace_s=5)
        th, box = self.start("normal", control)
        p = self.pids(["leader", "child", "grandchild"])
        control.request_stop()
        th.join(20)
        status, log = box["res"]
        self.assertEqual(status, 130)
        self.assertTrue(control.cancelled)
        for pid in p.values():
            self.assertTrue(self.gone(pid), pid)
        text = Path(log).read_text()
        self.assertIn("cancelled by user", text)
        self.assertNotIn("--- exit status 0", text)
        self.assertFalse(any("force-killed" in n for n in control.notes))  # TERM was enough
        self.assertEqual(Path(log).stat().st_mode & 0o777, 0o600)
        self.assertTrue((Path(self.tmp.name) / "prefix").is_dir())  # nothing deleted

    def test_a_child_that_ignores_term_is_killed_after_the_grace_period(self):
        control = self.RunControl(grace_s=0.5)
        th, box = self.start("ignore", control)
        p = self.pids(["leader", "child", "grandchild"])
        control.request_stop()
        th.join(20)
        self.assertEqual(box["res"][0], 130)
        for pid in p.values():
            self.assertTrue(self.gone(pid), pid)
        self.assertTrue(any("force-killed" in n for n in control.notes))

    def test_a_program_that_leaves_the_group_is_not_killed(self):
        control = self.RunControl(grace_s=0.5)
        th, box = self.start("escape", control)
        p = self.pids(["leader", "child", "grandchild"])
        control.request_stop()
        th.join(20)
        self.assertEqual(box["res"][0], 130)
        self.assertTrue(self.gone(p["leader"]))
        self.assertTrue(pid_alive(p["child"]))  # documented limit: out of reach of the group signal
        self.assertTrue(pid_alive(p["grandchild"]))  # it stayed with the escapee's own group

    def test_stopping_twice_or_after_the_end_does_nothing(self):
        import os

        control = self.RunControl(grace_s=1)
        th, box = self.start("quick", control)
        th.join(20)
        self.assertEqual(box["res"][0], 0)
        with mock.patch("os.killpg") as killpg, mock.patch("os.kill") as kill:
            control.request_stop()
            control.request_stop()
        killpg.assert_not_called()
        kill.assert_not_called()
        self.assertFalse(control.cancelled)
        self.assertTrue(os.path.isdir(Path(self.tmp.name) / "prefix"))

    def test_second_stop_while_stopping_is_harmless(self):
        control = self.RunControl(grace_s=5)
        th, box = self.start("normal", control)
        self.pids(["leader", "child", "grandchild"])
        control.request_stop()
        control.request_stop()
        th.join(20)
        self.assertEqual(box["res"][0], 130)

    def test_only_the_recorded_group_and_this_prefixs_wineserver_are_touched(self):
        import os
        import signal

        real_killpg = os.killpg
        calls = []

        def spy(pgid, sig):
            calls.append((pgid, sig))
            return real_killpg(pgid, sig)

        control = self.RunControl(grace_s=5)
        th, box = self.start("normal", control)
        p = self.pids(["leader", "child", "grandchild"])
        with mock.patch("os.killpg", side_effect=spy), mock.patch("os.kill") as kill:
            control.request_stop()
            th.join(20)
        kill.assert_not_called()  # never a single-pid or name-based signal
        self.assertTrue(calls)
        for pgid, sig in calls:
            self.assertEqual(pgid, p["leader"])
            self.assertIn(sig, (0, signal.SIGTERM, signal.SIGKILL))
        server = (Path(self.tmp.name) / "server.txt").read_text().strip()
        self.assertEqual(server, f"-k WINEPREFIX={Path(self.tmp.name) / 'prefix'}")

    def test_no_name_based_tools_are_ever_called(self):
        import subprocess as sp

        started = []
        real_run = sp.run

        def spy(argv, *a, **k):
            started.append(list(argv))
            return real_run(argv, *a, **k)

        control = self.RunControl(grace_s=5)
        th, box = self.start("normal", control)
        self.pids(["leader", "child", "grandchild"])
        with mock.patch("subprocess.run", side_effect=spy):
            control.request_stop()
            th.join(20)
        self.assertEqual(len(started), 1)
        self.assertEqual(Path(started[0][0]).name, "wineserver")
        self.assertEqual(started[0][1:], ["-k"])
        for word in ("pkill", "killall", "pgrep", "kill"):
            self.assertFalse(any(Path(a).name == word for a in started[0]))

    def test_proton_runtime_never_runs_wineserver(self):
        control = self.RunControl(grace_s=5)
        th, box = self.start("normal", control, runtime="proton")
        self.pids(["leader", "child", "grandchild"])
        control.request_stop()
        th.join(20)
        self.assertEqual(box["res"][0], 130)
        self.assertFalse((Path(self.tmp.name) / "server.txt").exists())

    def test_a_finished_run_can_no_longer_be_signalled(self):
        control = self.RunControl()
        control.attach(os_getpid_other())
        control.finish()
        with mock.patch("os.killpg") as killpg:
            self.assertFalse(control._signal(15))
            control.request_stop()
        killpg.assert_not_called()

    def test_stop_before_start_means_the_run_does_not_start(self):
        control = self.RunControl()
        control.request_stop()
        th, box = self.start("normal", control)
        th.join(20)
        self.assertEqual(box["res"][0], 130)
        self.assertFalse(self.out.exists())

    def test_a_failing_installer_is_reported_as_failed_not_cancelled(self):
        control = self.RunControl()
        th, box = self.start("fail", control)
        th.join(20)
        status, log = box["res"]
        self.assertEqual(status, 3)
        self.assertFalse(control.cancelled)
        self.assertIn("--- exit status 3", Path(log).read_text())

    def test_ctrl_c_triggers_the_same_graceful_stop(self):
        import os

        real = os.waitid
        state = {"n": 0}
        control = self.RunControl(grace_s=5)

        def waitid(*a):
            state["n"] += 1
            if state["n"] == 1:
                self.pids(["leader", "child", "grandchild"])
                raise KeyboardInterrupt
            return real(*a)

        with mock.patch("os.waitid", side_effect=waitid):
            status, log = self.execute(self.plan_for("normal"), str(Path(self.tmp.name) / "wine"),
                                       self.env, options=self.RunOptions(control=control))
        self.assertEqual(status, 130)
        self.assertTrue(control.cancelled)
        self.assertIn("cancelled by user", Path(log).read_text())

    def test_cancelled_event_in_the_json_log(self):
        import json

        control = self.RunControl(grace_s=5)
        plan = self.plan_for("normal")
        import threading

        box = {}
        th = threading.Thread(target=lambda: box.update(res=self.execute(
            plan, str(Path(self.tmp.name) / "wine"), self.env,
            options=self.RunOptions(control=control, detailed_log=True))))
        th.start()
        self.pids(["leader", "child", "grandchild"])
        control.request_stop()
        th.join(20)
        events = [json.loads(line) for line in
                  Path(box["res"][1]).with_suffix(".events.jsonl").read_text().splitlines()]
        self.assertEqual([e["event"] for e in events][-1], "cancelled")
        self.assertNotIn("exit", [e["event"] for e in events])

    def test_cli_cancelled_run_returns_130_and_skips_cleanup(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            (Path(data) / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)

            def fake(plan, wine, create, track, env=None, run=None, options=None):
                options.control.cancelled = True
                return 130, Path(state) / "x.log", None

            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("gitfurl.cli.run_tracked", side_effect=fake):
                code, out, _ = run_cli("install", str(f), "--prefix", "p1", "--yes",
                                       "--cleanup", "installer")
            self.assertTrue(f.exists())
        self.assertEqual(code, 130)
        self.assertIn("cancelled", out)
        self.assertIn("Nothing was deleted", out)
        self.assertIn("cleanup: skipped", out)
        self.assertNotIn("installer exited with status", out)


def os_getpid_other():
    import os

    return os.getpid() + 1_000_000  # a pid that is never signalled in the tests that use it


class PreflightTests(unittest.TestCase):
    def test_folder_size_counts_regular_files_and_does_not_follow_symlinks(self):
        from gitfurl.preflight import folder_size

        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as other:
            (Path(d) / "a.bin").write_bytes(b"x" * 1000)
            (Path(d) / "sub").mkdir()
            (Path(d) / "sub" / "b.bin").write_bytes(b"y" * 500)
            (Path(other) / "big.bin").write_bytes(b"z" * 100000)
            (Path(d) / "link").symlink_to(other)
            (Path(d) / "flink").symlink_to(Path(other) / "big.bin")
            self.assertEqual(folder_size(Path(d)), (1500, True))

    def test_folder_size_says_at_least_when_capped(self):
        from gitfurl.preflight import folder_size

        with tempfile.TemporaryDirectory() as d:
            for i in range(10):
                (Path(d) / f"f{i}").write_bytes(b"x" * 10)
            size, complete = folder_size(Path(d), max_entries=4)
            self.assertFalse(complete)
            self.assertLessEqual(size, 40)
            size, complete = folder_size(Path(d), max_seconds=-1)
            self.assertFalse(complete)

    def test_memory_from_fake_meminfo_and_failures(self):
        from gitfurl.preflight import memory_available

        with tempfile.TemporaryDirectory() as d:
            m = Path(d) / "meminfo"
            m.write_text("MemTotal: 999 kB\nMemAvailable:    2048 kB\n")
            self.assertEqual(memory_available(str(m)), 2048 * 1024)
            m.write_text("MemTotal: 999 kB\n")
            self.assertIsNone(memory_available(str(m)))
            m.write_text("MemAvailable: lots kB\n")
            self.assertIsNone(memory_available(str(m)))
            self.assertIsNone(memory_available(str(Path(d) / "missing")))

    def _measure(self, free, folder_files, mem_kb):
        from gitfurl.preflight import measure

        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "inst").mkdir()
            inst = Path(d) / "inst" / "setup.exe"
            inst.write_bytes(b"x" * folder_files)
            m = Path(d) / "meminfo"  # outside the measured folder
            m.write_text(f"MemAvailable: {mem_kb} kB\n")
            with mock.patch("shutil.disk_usage", return_value=mock.Mock(free=free)):
                return measure(inst, Path(d) / "pfx" / "deep", str(m))

    def test_labels_separate_measured_from_estimate_and_required_space_is_unknown(self):
        text = self._measure(10**12, 2000, 8_000_000).describe()
        self.assertIn("measured: free space", text)
        self.assertIn("measured: size of the installer's own files", text)
        self.assertIn("space the install needs: unknown", text)
        self.assertIn("estimate:", text)
        self.assertNotIn("advisory", text)  # nothing to warn about

    def test_no_multiplier_is_applied(self):
        # free space above the installer's own size: no warning, however close it is
        self.assertEqual(self._measure(2001, 2000, 8_000_000).warnings(), [])

    def test_warnings_are_advisory_and_labelled_estimates(self):
        warns = self._measure(100, 2000, 1_000_000).warnings()
        self.assertEqual(len(warns), 2)
        for w in warns:
            self.assertIn("advisory, estimate", w)
            self.assertIn("You can still go ahead", w)

    def test_unknown_values_are_reported_as_unknown(self):
        from gitfurl.preflight import Preflight

        text = Preflight(None, Path("/x"), 0, False, None, 4).describe()
        self.assertIn("free space where the prefix will live (/x): unknown", text)
        self.assertIn("at least", text)
        self.assertIn("memory available: unknown", text)

    def test_dry_run_shows_the_preflight_and_changes_nothing(self):
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            code, out, _ = run_cli("install", str(f), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("preflight (read-only, advisory)", out)
        self.assertIn("space the install needs: unknown", out)
