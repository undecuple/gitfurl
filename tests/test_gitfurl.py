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

try:  # pytest puts tests/ on sys.path; `python -m unittest tests.test_gitfurl` does not
    from farc_writer import F, make_archive, sample_blocks
except ImportError:
    from tests.farc_writer import F, make_archive, sample_blocks
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
    with mock.patch("subprocess.run", run), mock.patch("gitfurl.prefixes.run_in_group", run), \
            mock.patch("gitfurl.runner._run_in_group", run):
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
                code, _, err = run_cli("install", "--no-memory-limit", str(f), "--yes")
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
                code, _, _ = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1")
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
                code, _, err = run_cli("install", "--no-memory-limit", str(f), "--prefix", "nope", "--yes")
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
                code, out, _ = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes")
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
            "gitfurl.prefixes.run_in_group", return_value=done
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

    def test_ctrl_c_during_prefix_creation_is_a_message_and_status_130(self):
        with tempfile.TemporaryDirectory() as data, mock.patch.dict(
            "os.environ", {"XDG_DATA_HOME": data}
        ), mock.patch("shutil.which", return_value="/x/wine"), mock.patch(
            "gitfurl.prefixes.run_in_group", side_effect=KeyboardInterrupt
        ):
            code, _, err = run_cli("prefix", "create", "abc", "--yes")
        self.assertEqual(code, 130)
        self.assertIn("interrupted", err)

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

    def test_stop_applies_only_to_the_active_unstopped_run(self):
        # The decision stop_clicked re-checks after its modal dialog (R4). The dialog itself
        # and the button are not driven by any test.
        from gitfurl.gui import stop_applies
        from gitfurl.runner import RunControl

        mine, other = RunControl(), RunControl()
        self.assertTrue(stop_applies({"control": mine}, mine))
        self.assertFalse(stop_applies({"control": None}, mine))  # the run ended during the dialog
        self.assertFalse(stop_applies({"control": other}, mine))  # a newer run replaced it
        self.assertFalse(stop_applies({"control": None}, None))
        mine.cancelled = True
        self.assertFalse(stop_applies({"control": mine}, mine))  # already stopping

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
                code, out, _ = run_cli("install", "--no-memory-limit", str(f), "--prefix", "new", "--create-prefix", "--yes")
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
                    mock.patch("gitfurl.prefixes.run_in_group", return_value=done) as run:
                code, _, err = run_cli("install", "--no-memory-limit", str(f), "--prefix", "new", "--create-prefix", "--yes")
        self.assertEqual(code, 1)
        self.assertIn("installer was not run", err)
        self.assertEqual(run.call_count, 1)

    def test_without_flag_still_refuses_missing_prefix(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data, \
                tempfile.TemporaryDirectory() as state:
            f, env = self._env(d, data, state)
            with env, mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("subprocess.run") as run:
                code, _, err = run_cli("install", "--no-memory-limit", str(f), "--prefix", "new", "--yes")
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
                    run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes", "--cleanup", "installer")
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
                    outs[bool(flag)] = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes", *flag)[1]
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
                _, out, _ = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes")
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
                code, _, err = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes",
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
                code, _, _ = run_cli("install", "--no-memory-limit", str(f), "--prefix", "n", "--create-prefix", "--yes",
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
                    _, out, _ = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes", *flag)
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
                code1, _, err1 = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes")
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
                run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes")
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
            {"platform": "x", "wine": r, "bwrap": r, "umu": r, "innoextract": r, "tkinter": r}))

    def test_doctor_wording(self):
        # Seen on a real system on 2026-11-23: "tkinter: available at importable", and the
        # optional notes shown only on one of found/missing.
        from gitfurl import doctor

        with mock.patch("importlib.util.find_spec", return_value=object()):
            tk = doctor.probe_tkinter()
        found = doctor.ToolReport("x", "/bin/x", "x 1.0", None)
        gone = doctor.ToolReport("x", None, None, "not found on PATH")
        for tool in (found, gone):
            text = doctor.format_report({"platform": "p", "wine": tool, "bwrap": tool,
                                         "umu": tool, "innoextract": tool, "tkinter": tk})
            self.assertIn("tkinter: available (importable)\n", text)
            self.assertNotIn("at importable", text)
            lines = text.splitlines()  # platform, wine, bwrap, umu-run, innoextract, tkinter
            self.assertTrue(lines[2].endswith("(optional, for --portable)"), lines[2])
            self.assertTrue(lines[3].endswith("(optional, for the proton runtime)"), lines[3])
            self.assertTrue(lines[4].endswith(
                "(optional; only noted: gitfurl never runs it to extract)"), lines[4])
            self.assertNotIn("optional", lines[1])  # wine

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

    def test_sample_line_strips_control_characters_from_program_names(self):
        from gitfurl.progress import Sample

        odd = Sample(60, 0, 0, 0, 0.0, procs=1, helpers=("evil\x1b[2J\rname", "ok.exe"))
        line = odd.line()
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\r", line)
        self.assertIn("evil?[2J?name, ok.exe", line)

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
        control.start(lambda: mock.Mock(pid=os_getpid_other()))
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

    def _spy_killpg(self):
        import os

        real, calls = os.killpg, []

        def spy(pgid, sig):
            calls.append((pgid, sig))
            return real(pgid, sig)

        return calls, mock.patch("os.killpg", side_effect=spy)

    def test_a_stop_between_the_check_and_the_start_cannot_be_lost(self):
        # R1: the stop arrives while the leader is being started. It must wait for the start
        # and then end the run, never let it run on while being reported as cancelled.
        import subprocess
        import threading

        control = self.RunControl(grace_s=5)
        real_popen = subprocess.Popen

        def slow_popen(*a, **k):
            threading.Thread(target=control.request_stop).start()
            self.time.sleep(0.4)  # the stop is now waiting for the lock
            proc = real_popen(*a, **k)
            self.stray.append(proc.pid)
            return proc

        calls, spy = self._spy_killpg()
        with spy, mock.patch("subprocess.Popen", side_effect=slow_popen):
            status, _ = self.execute(self.plan_for("normal"), str(Path(self.tmp.name) / "wine"),
                                       self.env, options=self.RunOptions(control=control))
        self.assertEqual(status, 130)
        self.assertTrue(control.cancelled)
        self.assertIn((self.stray[0], __import__("signal").SIGTERM), calls)
        self.assertTrue(self.gone(self.stray[0]))

    def test_ctrl_c_between_the_start_and_the_recording_is_not_lost(self):
        import os
        import signal
        import subprocess

        control = self.RunControl(grace_s=5)
        real_popen = subprocess.Popen

        def popen_then_sigint(*a, **k):
            proc = real_popen(*a, **k)
            self.stray.append(proc.pid)
            os.kill(os.getpid(), signal.SIGINT)  # our own process only
            return proc

        with mock.patch("subprocess.Popen", side_effect=popen_then_sigint):
            status, _ = self.execute(self.plan_for("normal"), str(Path(self.tmp.name) / "wine"),
                                       self.env, options=self.RunOptions(control=control))
        self.assertEqual(status, 130)
        self.assertTrue(control.cancelled)
        self.assertTrue(self.gone(self.stray[0]))

    def test_second_ctrl_c_skips_the_wait_and_forces_the_stop(self):
        # R3: a long grace period, a program that ignores TERM, two Ctrl-C presses.
        import os
        import signal
        import threading

        control = self.RunControl(grace_s=60)

        def presses():
            p = self.pids(["leader", "child", "grandchild"])
            os.kill(os.getpid(), signal.SIGINT)
            self.time.sleep(1.0)
            os.kill(os.getpid(), signal.SIGINT)
            return p

        th = threading.Thread(target=presses)
        th.start()
        t0 = self.time.monotonic()
        status, log = self.execute(self.plan_for("ignore"), str(Path(self.tmp.name) / "wine"),
                                   self.env, options=self.RunOptions(control=control))
        th.join(5)
        self.assertLess(self.time.monotonic() - t0, 30)
        self.assertEqual(status, 130)
        self.assertTrue(any("second Ctrl-C" in n for n in control.notes), control.notes)
        for pid in self.stray:
            self.assertTrue(self.gone(pid), pid)
        self.assertIn("force-killed", Path(log).read_text())

    def test_stop_after_the_leader_exited_is_not_a_cancel(self):
        # R2, in the gap between the leader exiting and the run being closed.
        control = self.RunControl(grace_s=1)
        real_mark = control.mark_exited

        def mark_then_stop():
            real_mark()
            control.request_stop()

        calls, spy = self._spy_killpg()
        with spy, mock.patch.object(control, "mark_exited", side_effect=mark_then_stop):
            status, log = self.execute(self.plan_for("quick"), str(Path(self.tmp.name) / "wine"),
                                       self.env, options=self.RunOptions(control=control))
        self.assertEqual(status, 0)
        self.assertFalse(control.cancelled)
        self.assertEqual(calls, [])
        self.assertFalse((Path(self.tmp.name) / "server.txt").exists())  # no wineserver -k
        self.assertIn("--- exit status 0", Path(log).read_text())

    def test_stop_racing_the_leader_exit_signals_nothing_when_the_group_is_empty(self):
        import os

        control = self.RunControl(grace_s=1)
        real = os.waitid

        def waitid_then_stop(*a):
            res = real(*a)  # the leader has exited (a zombie, not reaped)
            control.request_stop()
            return res

        calls, spy = self._spy_killpg()
        with spy, mock.patch("os.waitid", side_effect=waitid_then_stop):
            status, _ = self.execute(self.plan_for("quick"), str(Path(self.tmp.name) / "wine"),
                                       self.env, options=self.RunOptions(control=control))
        self.assertEqual(status, 0)
        self.assertFalse(control.cancelled)
        self.assertEqual(calls, [])

    def test_wineserver_failure_and_absence_are_reported_honestly(self):
        t = Path(self.tmp.name)
        (t / "wineserver").write_text("#!/bin/sh\nexit 7\n")
        control = self.RunControl(grace_s=5)
        th, _ = self.start("normal", control)
        self.pids(["leader", "child", "grandchild"])
        control.request_stop()
        th.join(20)
        self.assertTrue(any("exited with status 7" in n for n in control.notes), control.notes)
        self.assertFalse(any(n.startswith("asked this prefix") for n in control.notes))
        missing = self.RunControl(grace_s=5)
        missing.configure_server(None, None)
        missing.start(lambda: mock.Mock(pid=os_getpid_other()))
        with mock.patch("os.killpg"), mock.patch.object(missing, "_group_alive", return_value=True):
            missing.request_stop()
        self.assertTrue(any("wineserver was not found" in n for n in missing.notes), missing.notes)

    def test_a_bare_wine_name_never_resolves_wineserver_against_the_cwd(self):
        import os

        t = Path(self.tmp.name)
        marker = t / "cwd-server-ran.txt"
        (t / "cwd").mkdir()
        (t / "cwd" / "wineserver").write_text(f'#!/bin/sh\necho x >> "{marker}"\n')
        (t / "cwd" / "wineserver").chmod(0o755)
        control = self.RunControl(grace_s=1)
        plan = self.plan_for("normal")
        plan = type(plan)(argv=plan.argv, prefix=plan.prefix, cwd=t / "cwd", runtime="wine",
                          env_overrides=plan.env_overrides)
        old = os.getcwd()
        os.chdir(t / "cwd")
        self.addCleanup(os.chdir, old)
        # "wine" is not absolute, so only a PATH lookup may find a wineserver
        (t / "bin").mkdir()
        (t / "bin" / "wine").write_text(f'#!/bin/sh\nexec "{self.py}" "$@"\n')
        (t / "bin" / "wine").chmod(0o755)
        env = {**self.env, "PATH": f"{t / 'bin'}:/usr/bin:/bin"}
        with mock.patch("shutil.which", return_value=None):
            th = __import__("threading").Thread(target=lambda: self.execute(
                plan, str(t / "bin" / "wine"), env, options=self.RunOptions(control=control)))
            th.start()
            self.pids(["leader", "child", "grandchild"])
            control.request_stop()
            th.join(20)
        self.assertFalse(marker.exists())

    def test_ctrl_c_between_guard_release_and_the_wait_loop_still_stops_the_run(self):
        # any exception after the child is recorded must stop it, not leave it unmanaged
        control = self.RunControl(grace_s=5)
        from gitfurl import runner

        real_release = runner._SigintGuard.release
        state = {"n": 0}

        def release_then_interrupt(g):
            real_release(g)
            state["n"] += 1
            if state["n"] == 1:  # the release inside the run, right after the child was recorded
                raise KeyboardInterrupt

        calls, spy = self._spy_killpg()
        with spy, mock.patch.object(runner._SigintGuard, "release", release_then_interrupt), \
                self.assertRaises(KeyboardInterrupt):
            self.execute(self.plan_for("normal"), str(Path(self.tmp.name) / "wine"),
                         self.env, options=self.RunOptions(control=control))
        self.assertTrue(any(sig == __import__("signal").SIGTERM for _, sig in calls), calls)

    def test_request_stop_releases_finish_even_if_the_guard_setup_fails(self):
        from gitfurl import runner

        control = self.RunControl(grace_s=1)
        control.start(lambda: mock.Mock(pid=os_getpid_other()))
        with mock.patch.object(runner._SigintGuard, "__enter__", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt):
            control.request_stop()
        control.finish()  # must not wait forever
        self.assertFalse(control.cancelled)

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
                options.control.request_stop()  # a real stop: nothing was started yet
                return 130, Path(state) / "x.log", None

            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data, "XDG_STATE_HOME": state}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("gitfurl.cli.run_tracked", side_effect=fake):
                code, out, _ = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--yes",
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


class PrefixTimeoutTests(unittest.TestCase):
    def test_a_wineboot_timeout_is_a_clear_error_not_a_traceback(self):
        import subprocess

        from gitfurl.prefixes import create_prefix

        def slow(argv, **kw):
            raise subprocess.TimeoutExpired(argv, kw["timeout"])

        with tempfile.TemporaryDirectory() as d:
            env = {"XDG_DATA_HOME": d, "HOME": d}
            with self.assertRaisesRegex(RuntimeError, "timed out.*What gitfurl did"):
                create_prefix("p1", "/x/wine", env, run=slow)
            self.assertTrue((Path(d) / "gitfurl" / "prefixes" / "p1").is_dir())  # kept


class PrefixGroupTests(unittest.TestCase):
    """wineboot runs in its own group; dummy processes and a fake wineserver only."""

    CHILD = (
        "import os, signal, subprocess, sys, time\n"
        "out, role = sys.argv[1], sys.argv[2]\n"
        "ignore = 'ignore' in sys.argv[3:]\n"
        "if role == 'child':\n"
        "    if ignore:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    open(out, 'a').write(f'child {os.getpid()}\\n'); time.sleep(120)\n"
        "else:\n"
        "    if 'ignoreleader' in sys.argv[3:]:\n"
        "        signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "    open(out, 'a').write(f'leader {os.getpid()}\\n')\n"
        "    subprocess.Popen([sys.executable, __file__, out, 'child'] + sys.argv[3:])\n"
        "    time.sleep(120)\n"
    )

    def setUp(self):
        import sys

        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        (t / "d.py").write_text(self.CHILD)
        (t / "wine").write_text("#!/bin/sh\nexit 0\n")
        (t / "wineserver").write_text(
            f'#!/bin/sh\necho "$@ WINEPREFIX=$WINEPREFIX" >> "{t}/server.txt"\n')
        for n in ("wine", "wineserver"):
            (t / n).chmod(0o755)
        self.argv = [sys.executable, str(t / "d.py"), str(t / "pids.txt"), "leader"]
        self.env = {"WINEPREFIX": str(t / "p"), "PATH": "/nonexistent"}
        self.stray = []
        self.addCleanup(self._kill)

    def _kill(self):
        import signal

        for pid in self.stray:
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)

    def _pids(self):
        import time

        out = Path(self.tmp.name) / "pids.txt"
        end = time.monotonic() + 15
        while time.monotonic() < end:
            if out.exists() and len(out.read_text().split()) >= 4:
                got = [int(x) for x in out.read_text().split()[1::2]]
                self.stray.extend(got)
                return got
            time.sleep(0.05)
        self.fail("dummy processes did not start")

    def test_normal_exit_returns_the_status_and_runs_no_cleanup(self):
        from gitfurl.prefixes import run_in_group

        with mock.patch("os.killpg") as killpg:
            res = run_in_group(["/bin/sh", "-c", "exit 3"], {}, 10, str(Path(self.tmp.name) / "wine"))
        self.assertEqual(res.returncode, 3)
        killpg.assert_not_called()
        self.assertFalse((Path(self.tmp.name) / "server.txt").exists())

    def test_timeout_ends_the_whole_group_and_asks_this_prefixs_server_to_exit(self):
        import subprocess
        import threading

        from gitfurl.prefixes import run_in_group

        box = {}

        def work():
            try:
                run_in_group(self.argv, self.env, 2, str(Path(self.tmp.name) / "wine"), grace_s=2)
            except subprocess.TimeoutExpired as exc:
                box["exc"] = exc

        th = threading.Thread(target=work)
        th.start()
        pids = self._pids()
        th.join(30)
        self.assertIn("exc", box)
        for pid in pids:
            end = __import__("time").monotonic() + 5
            while pid_alive(pid) and __import__("time").monotonic() < end:
                __import__("time").sleep(0.05)
            self.assertFalse(pid_alive(pid), pid)
        server = (Path(self.tmp.name) / "server.txt").read_text().strip()
        self.assertEqual(server, f"-k WINEPREFIX={Path(self.tmp.name) / 'p'}")

    def test_should_stop_ends_the_group_from_another_thread(self):
        # The window's Stop during prefix creation: the run waits on a worker thread, where
        # no Ctrl-C can reach it.
        import threading
        import time

        from gitfurl.prefixes import PrefixStopped, run_in_group

        box, stop = {}, threading.Event()

        def work():
            try:
                run_in_group(self.argv, self.env, 60, str(Path(self.tmp.name) / "wine"),
                             grace_s=2, should_stop=stop.is_set)
            except PrefixStopped as exc:
                box["exc"] = exc

        th = threading.Thread(target=work)
        th.start()
        pids = self._pids()
        stop.set()
        th.join(30)
        self.assertIn("exc", box)
        self.assertIn("asked this prefix's Wine server to exit", "; ".join(box["exc"].notes))
        for pid in pids:
            end = time.monotonic() + 5
            while pid_alive(pid) and time.monotonic() < end:
                time.sleep(0.05)
            self.assertFalse(pid_alive(pid), pid)

    def test_ctrl_c_ends_the_group_and_reraises(self):
        import signal
        import threading

        from gitfurl.prefixes import run_in_group

        def press():
            self._pids()
            os.kill(os.getpid(), signal.SIGINT)  # our own process only

        th = threading.Thread(target=press)
        th.start()
        with self.assertRaises(KeyboardInterrupt):
            run_in_group(self.argv, self.env, 60, str(Path(self.tmp.name) / "wine"), grace_s=2)
        th.join(10)
        for pid in self.stray:
            end = __import__("time").monotonic() + 5
            while pid_alive(pid) and __import__("time").monotonic() < end:
                __import__("time").sleep(0.05)
            self.assertFalse(pid_alive(pid), pid)

    def test_only_the_leaders_group_is_signalled_and_nothing_by_name(self):
        import subprocess
        import threading

        from gitfurl.prefixes import run_in_group

        calls = []
        real = os.killpg

        def spy(pgid, sig):
            calls.append(pgid)
            return real(pgid, sig)

        box = {}

        def work():
            with contextlib.suppress(subprocess.TimeoutExpired):
                run_in_group(self.argv, self.env, 1, str(Path(self.tmp.name) / "wine"), grace_s=2)
            box["done"] = True

        with mock.patch("os.killpg", side_effect=spy), mock.patch("os.kill") as kill:
            th = threading.Thread(target=work)
            th.start()
            leader = self._pids()[0]
            th.join(30)
        kill.assert_not_called()
        self.assertTrue(calls)
        self.assertEqual(set(calls), {leader})

    def _gone(self, pids, seconds=8):
        import time

        end = time.monotonic() + seconds
        for pid in pids:
            while pid_alive(pid) and time.monotonic() < end:
                time.sleep(0.05)
            self.assertFalse(pid_alive(pid), pid)

    def test_a_child_that_ignores_term_is_killed_even_when_the_leader_exits_on_term(self):
        # the leader dies on TERM (default action); the child ignores it. Reaping the leader
        # must not stop the KILL from reaching the child.
        import subprocess
        import threading

        from gitfurl.prefixes import run_in_group

        box = {}

        def work():
            try:
                run_in_group(self.argv + ["ignore"], self.env, 2,
                             str(Path(self.tmp.name) / "wine"), grace_s=1)
            except subprocess.TimeoutExpired as exc:
                box["notes"] = exc.notes

        th = threading.Thread(target=work)
        th.start()
        pids = self._pids()
        th.join(30)
        self._gone(pids)
        self.assertTrue(any("force-killed" in n for n in box["notes"]), box)
        self.assertTrue(any("were stopped" in n for n in box["notes"]), box)

    def test_a_second_ctrl_c_during_cleanup_only_shortens_the_wait(self):
        import signal
        import threading
        import time

        from gitfurl.prefixes import run_in_group

        def presses():
            self._pids()
            os.kill(os.getpid(), signal.SIGINT)  # our own process only
            time.sleep(0.7)
            os.kill(os.getpid(), signal.SIGINT)

        th = threading.Thread(target=presses)
        th.start()
        t0 = time.monotonic()
        with self.assertRaises(KeyboardInterrupt) as cm:
            run_in_group(self.argv + ["ignore"], self.env, 60,
                         str(Path(self.tmp.name) / "wine"), grace_s=30)
        th.join(10)
        self.assertLess(time.monotonic() - t0, 20)  # the 30 s grace was cut short
        self._gone(self.stray)
        self.assertTrue(any("second Ctrl-C" in n for n in cm.exception.notes), cm.exception.notes)
        self.assertTrue((Path(self.tmp.name) / "server.txt").exists())  # the later steps still ran

    def test_a_ctrl_c_held_before_the_start_still_gets_the_normal_grace(self):
        import signal
        import subprocess

        from gitfurl.prefixes import run_in_group

        real_popen = subprocess.Popen

        def popen_then_sigint(*a, **k):
            import time

            proc = real_popen(*a, **k)
            self.stray.append(proc.pid)
            out = Path(self.tmp.name) / "pids.txt"
            end = time.monotonic() + 15
            while time.monotonic() < end and not (out.exists() and "leader" in out.read_text()):
                time.sleep(0.02)  # the leader now ignores TERM
            os.kill(os.getpid(), signal.SIGINT)  # held by the guard: this is the first Ctrl-C
            return proc

        with mock.patch("subprocess.Popen", side_effect=popen_then_sigint), \
                self.assertRaises(KeyboardInterrupt) as cm:
            run_in_group(self.argv + ["ignoreleader"], self.env, 60,
                         str(Path(self.tmp.name) / "wine"), grace_s=1)
        self._gone(self.stray)
        # the leader ignores TERM, so KILL follows; but only after the normal grace period,
        # and the note must not blame a second Ctrl-C for it
        self.assertTrue(any("did not stop within 1 s" in n for n in cm.exception.notes),
                        cm.exception.notes)
        self.assertFalse(any("second Ctrl-C" in n for n in cm.exception.notes), cm.exception.notes)

    def test_ctrl_c_while_the_prefix_folder_is_made_says_nothing_was_run(self):
        from gitfurl.prefixes import create_prefix

        with tempfile.TemporaryDirectory() as d, \
                mock.patch("pathlib.Path.mkdir", side_effect=KeyboardInterrupt), \
                self.assertRaises(KeyboardInterrupt) as cm:
            create_prefix("p1", "/x/wine", {"XDG_DATA_HOME": d, "HOME": d})
        self.assertIn("nothing was run", cm.exception.notes[0])

    def test_notes_say_when_wineserver_failed_or_was_missing(self):
        import subprocess

        from gitfurl.prefixes import run_in_group

        t = Path(self.tmp.name)
        (t / "wineserver").write_text("#!/bin/sh\nexit 7\n")
        with self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_in_group(self.argv, self.env, 0.5, str(t / "wine"), grace_s=1)
        self.assertTrue(any("exited with status 7" in n for n in cm.exception.notes))
        with mock.patch("shutil.which", return_value=None), \
                self.assertRaises(subprocess.TimeoutExpired) as cm:
            run_in_group(self.argv, self.env, 0.5, "wine", grace_s=1)  # a bare wine name
        self.assertTrue(any("wineserver was not found" in n for n in cm.exception.notes))

    def test_wineserver_is_never_taken_from_a_relative_path_entry(self):
        from gitfurl.procgroup import wineserver_for

        t = Path(self.tmp.name)
        (t / "wineserver").chmod(0o755)
        old = os.getcwd()
        os.chdir(t)
        self.addCleanup(os.chdir, old)
        with mock.patch.dict("os.environ", {"PATH": ".:"}):
            self.assertIsNone(wineserver_for("wine"))
        self.assertEqual(wineserver_for(str(t / "wine")), str(t / "wineserver"))

    def test_install_create_prefix_interrupt_says_what_was_done_and_exits_130(self):
        with tempfile.TemporaryDirectory() as d, tempfile.TemporaryDirectory() as data:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"x")
            exc = KeyboardInterrupt()
            exc.notes = ["the processes it started were stopped"]
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}), \
                    mock.patch("shutil.which", return_value="/x/wine"), \
                    mock.patch("gitfurl.cli.run_tracked", side_effect=exc):
                code, _, err = run_cli("install", "--no-memory-limit", str(f), "--prefix", "p1", "--create-prefix", "--yes")
        self.assertEqual(code, 130)
        self.assertIn("the processes it started were stopped", err)
        self.assertIn("The installer was not started", err)

    def test_a_missing_wineserver_does_not_hide_the_timeout(self):
        import subprocess

        from gitfurl.prefixes import run_in_group

        with self.assertRaises(subprocess.TimeoutExpired):
            run_in_group([__import__("sys").executable, "-c", "import time; time.sleep(60)"],
                         {"PATH": "/nonexistent"}, 0.5, "wine", grace_s=2)  # bare name and an empty PATH: no wineserver found


class DetectTests(unittest.TestCase):
    """Synthetic bytes built here. No real installer, archive or key is ever used."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def file(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def det(self, name, data):
        from gitfurl.detect import detect

        return detect(self.file(name, data))

    INNO = b"Inno Setup Setup Data (5.5.0)"

    def test_inno_id_in_a_windows_program_is_likely_and_without_archives_is_wine(self):
        from gitfurl.detect import ROUTE_WINE
        from gitfurl.route import choose_route

        det = self.det("setup.exe", b"MZ" + b"\0" * 1000 + self.INNO + b"\0" * 40)
        self.assertEqual([(e.format, e.confidence) for e in det.evidence], [("Inno Setup", "likely")])
        self.assertIn("byte offset 1002", det.evidence[0].where)
        self.assertIn("5.5.0", det.evidence[0].detail)
        with tempfile.TemporaryDirectory() as data:
            choice = choose_route(det.path, "p1", det, env={"XDG_DATA_HOME": data})
        self.assertEqual(choice.route, ROUTE_WINE)
        self.assertIn("no FreeArc archives", choice.line())

    def test_inno_variants_and_legacy_id(self):
        for ident in (b"Inno Setup Setup Data (6.4.0.1)", b"Inno Setup Setup Data (5.5.7) (U)",
                      b"My Inno Setup Extensions Setup Data (3.0.6.1)", b"i1.2.10--32\x1a"):
            det = self.det("v.exe", b"MZ" + b"\0" * 10 + ident)
            self.assertEqual([e.format for e in det.evidence], ["Inno Setup"], ident)

    def test_the_id_without_mz_is_not_detected(self):
        det = self.det("notes.txt", b"hello " + self.INNO)
        self.assertEqual(det.evidence, [])

    def test_id_split_across_a_read_boundary_is_found(self):
        from gitfurl import detect as d

        pad = b"\0" * (d.CHUNK - 10)
        det = self.det("big.exe", b"MZ" + pad + self.INNO)
        self.assertEqual([e.format for e in det.evidence], ["Inno Setup"])

    def test_the_scan_is_bounded_and_says_so(self):
        from gitfurl import detect as d

        with mock.patch.object(d, "MAX_SCAN_BYTES", 4096):
            det = self.det("late.exe", b"MZ" + b"\0" * 8000 + self.INNO)
        self.assertEqual(det.evidence, [])
        self.assertTrue(any("was not searched" in n for n in det.notes))

    def test_truncated_garbage_empty_and_odd_files_are_safe(self):
        for data in (b"", b"M", b"MZ", b"MZ" + self.INNO[:10], b"\xff" * 100, b"ArC"):
            det = self.det("x.bin", data)
            self.assertEqual(det.evidence, [], data)
            self.assertTrue(det.readable)

    def test_a_length_like_field_is_never_trusted(self):
        # 4 GiB claimed in the bytes; only real bytes are read
        det = self.det("liar.exe", b"MZ" + b"\xff\xff\xff\xff" * 4 + self.INNO)
        self.assertEqual([e.format for e in det.evidence], ["Inno Setup"])

    def test_freearc_signature_without_a_readable_index_is_only_possible(self):
        det = self.det("a.arc", b"ArC\x01" + b"\0" * 20)
        self.assertEqual([(e.format, e.confidence) for e in det.evidence], [("FreeArc", "possible")])
        self.assertIn("index could not be read", det.evidence[0].detail)
        self.assertIn("footer", det.evidence[0].detail)
        self.assertEqual(det.evidence[0].lines, [])
        self.assertNotIn("unverified", det.evidence[0].detail)

    def test_large_files_beside_the_installer_are_checked_by_their_first_four_bytes(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ" + b"\0" * 100 + self.INNO)
        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 64):
            self.file("p-01.bin", b"ArC\x01" + b"x" * 100)
            self.file("p-02.bin", b"ArC\x01" + b"x" * 100)
            self.file("other.dat", b"zzzz" + b"x" * 100)
            self.file("tiny.bin", b"ArC\x01")  # below the size floor
            det = d.detect(inst)
        free = [e for e in det.evidence if e.format == "FreeArc"]
        self.assertEqual([e.where for e in free], ["large file beside the installer: p-01.bin",
                                                   "large file beside the installer: p-02.bin"])
        self.assertEqual({e.confidence for e in free}, {"possible"})  # not real archives
        self.assertFalse(any("other.dat" in e.where + e.detail for e in free))
        self.assertIn("Inno Setup", [e.format for e in det.evidence])

    def test_symlinks_and_directories_are_not_followed_or_opened(self):
        from gitfurl import detect as d

        (self.dir / "sub").mkdir()
        target = self.dir / "sub" / "real.bin"
        target.write_bytes(b"ArC\x01" + b"x" * 100)
        link = self.dir / "link.exe"
        link.symlink_to(target)
        det = d.detect(link)
        self.assertFalse(det.readable)
        (self.dir / "adir").mkdir()
        self.assertFalse(d.detect(self.dir / "adir").readable)
        self.assertFalse(d.detect(self.dir / "missing.exe").readable)
        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 8):
            inst = self.file("setup.exe", b"MZ")
            os.symlink(target, self.dir / "linked-payload.bin")
            det = d.detect(inst)
        self.assertEqual([e for e in det.evidence if e.format == "FreeArc"], [])

    def test_a_fifo_does_not_block(self):
        from gitfurl import detect as d

        fifo = self.dir / "pipe.exe"
        os.mkfifo(fifo)
        self.assertFalse(d.detect(fifo).readable)

    def test_unreadable_file_routes_to_unsupported(self):
        from gitfurl.detect import ROUTE_UNSUPPORTED, decide, detect

        self.assertEqual(decide(detect(self.dir / "nope.exe")).route, ROUTE_UNSUPPORTED)

    def test_neighbour_count_is_capped_and_names_are_sanitised(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ")
        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 8), mock.patch.object(d, "MAX_NEIGHBOURS", 3):
            for i in range(6):
                self.file(f"f{i}.bin", b"zzzzzzzzzz")
            det = d.detect(inst)
        self.assertTrue(any("not all files" in n for n in det.notes))
        self.assertEqual(d.clean("a\x1b[31mb\nc"), "a?[31mb?c")

    def test_a_huge_folder_is_not_walked_and_only_capped_files_are_opened(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ")
        for i in range(30):
            self.file(f"f{i:02}.bin", b"ArC\x01" + b"x" * 20)
        opened = []
        real_open = d._open_regular

        def counting(path):
            opened.append(path)
            return real_open(path)

        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 8), mock.patch.object(d, "MAX_ENTRIES", 10), \
                mock.patch.object(d, "MAX_NEIGHBOURS", 4), mock.patch.object(d, "_open_regular", counting):
            det = d.detect(inst)
        self.assertLessEqual(len(opened), 1 + 4)  # the installer itself plus at most 4 neighbours
        self.assertTrue(any("not all files" in n for n in det.notes))

    def test_listing_is_never_sorted_as_a_whole(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ")
        self.file("a.bin", b"zzzz" * 10)
        with mock.patch("builtins.sorted", side_effect=AssertionError("full sort")):
            d.detect(inst)

    def test_detection_runs_nothing_and_writes_nothing(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ" + self.INNO)
        before = sorted(p.name for p in self.dir.iterdir())
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no process")), \
                mock.patch("subprocess.run", side_effect=AssertionError("no process")), \
                mock.patch("os.system", side_effect=AssertionError("no shell")):
            text = d.describe(d.detect(inst))
        self.assertEqual(before, sorted(p.name for p in self.dir.iterdir()))
        self.assertIn("nothing was run", text)
        self.assertIn("not a promise", text)
        # no compressor is named: the only mention of methods is the fixed wording
        self.assertEqual(text.count("method"), text.count("(names, sizes, methods)"))

    def test_the_file_is_never_made_executable_or_modified(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ" + self.INNO)
        before = (inst.stat().st_mode, inst.stat().st_mtime_ns, inst.read_bytes())
        d.detect(inst)
        after = (inst.stat().st_mode, inst.stat().st_mtime_ns, inst.read_bytes())
        self.assertEqual(before, after)

    def test_dry_run_prints_the_detection_and_still_creates_nothing(self):
        with tempfile.TemporaryDirectory() as data, tempfile.TemporaryDirectory() as d:
            f = Path(d) / "setup.exe"
            f.write_bytes(b"MZ" + self.INNO)
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}):
                code, out, _ = run_cli("install", str(f), "--prefix", "p1", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("Inno Setup: likely", out)
            self.assertIn("route: wine", out)
            self.assertFalse((Path(data) / "gitfurl").exists())


class FreeArcIndexInPlanTests(unittest.TestCase):
    """FreeArc archives built in memory by tests/farc_writer.py; none is kept in the repo."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)

    def file(self, name, data):
        p = self.dir / name
        p.write_bytes(data)
        return p

    def test_a_readable_index_is_likely_and_its_summary_is_in_the_plan(self):
        from gitfurl import detect as d

        blocks, folders = sample_blocks("lzma:1mb")
        blocks.append(("storing", [F("c.txt", b"abc" * 100)]))
        det = d.detect(self.file("game.arc", make_archive(blocks, folders, index="lzma:1mb")))
        self.assertEqual([(e.format, e.confidence) for e in det.evidence], [("FreeArc", "likely")])
        ev = det.evidence[0]
        self.assertIn("CRC-32 checks match", ev.detail)
        self.assertNotIn("unverified", ev.detail)
        self.assertEqual(ev.lines[:2], ["3 files and 1 folder, 3.9 KiB in total",
                                        "2 data blocks: 1 storing, 1 lzma"])
        text = d.describe(det)
        self.assertIn("FreeArc: likely.", text)
        self.assertIn("    3 files and 1 folder, 3.9 KiB in total", text)
        self.assertIn("    locked: no", text)
        self.assertIn("nothing was run or extracted", text)
        self.assertIn("only the archive's own index", text)
        self.assertNotIn("native extraction is not available", text)
        self.assertIn("route: not chosen here", text)
        from gitfurl.route import choose_route

        choice = choose_route(det.path, "p1", det, env={"XDG_DATA_HOME": str(self.dir)})
        self.assertEqual(choice.route, d.ROUTE_NATIVE)
        self.assertIn("route: native (chosen by gitfurl)", d.describe(det, choice))

    def test_unhandled_method_is_named_generically_and_its_text_never_shown(self):
        from gitfurl import detect as d

        blocks = [("storing", [F("a.bin", b"a" * 10)]),
                  ("zzzmethod:42:qqsecret", [F("b.bin", b"b" * 20)])]
        text = d.describe(d.detect(self.file("x.arc", make_archive(blocks))))
        self.assertIn("2 data blocks: 1 storing, 0 lzma, 1 a method gitfurl does not handle",
                      text)
        self.assertNotIn("zzzmethod", text)
        self.assertNotIn("qqsecret", text)

    def test_encrypted_or_damaged_index_stays_possible_and_says_why(self):
        from gitfurl import detect as d

        blocks, folders = sample_blocks()
        cases = {
            "enc.arc": (make_archive(blocks, folders, index="aes-256/ctr:n1000:qqkey"),
                        "encrypted"),
            "crc.arc": (make_archive(blocks, folders, bad_dir_crc=True), "CRC"),
            "cut.arc": (make_archive(blocks, folders)[:-30], "footer"),
        }
        for name, (data, why) in cases.items():
            det = d.detect(self.file(name, data))
            self.assertEqual([(e.format, e.confidence) for e in det.evidence],
                             [("FreeArc", "possible")], name)
            self.assertIn("index could not be read", det.evidence[0].detail, name)
            self.assertIn(why, det.evidence[0].detail, name)
            text = d.describe(det)
            self.assertNotIn("qqkey", text)
            self.assertNotIn("aes", text)
            from gitfurl.route import choose_route

            choice = choose_route(det.path, "p1", det, env={"XDG_DATA_HOME": str(self.dir)})
            self.assertEqual(choice.route, d.ROUTE_WINE, name)
            self.assertIn("cannot be unpacked by gitfurl", choice.line(), name)

    def test_names_from_the_archive_are_cleaned(self):
        from gitfurl import detect as d

        files = [F("../evil\x1b[31m\nname", b"1"), F("ok.txt", b"2")]
        text = d.describe(d.detect(self.file("n.arc", make_archive([("storing", files)]))))
        self.assertIn("1 name would not be safe to extract", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("../evil?[31m?name", text)

    def test_an_unexpected_error_or_memory_error_never_leaves_detect(self):
        from gitfurl import detect as d

        path = self.file("a.arc", make_archive(*sample_blocks()))
        for exc, why in ((RuntimeError("boom"), "could not be read"),
                         (MemoryError(), "not enough memory")):
            with mock.patch.object(d.farc, "read_fd", side_effect=exc):
                det = d.detect(path)
            self.assertEqual([e.confidence for e in det.evidence], ["possible"])
            self.assertIn(why, det.evidence[0].detail)
            self.assertNotIn("boom", det.evidence[0].detail)

    def test_neighbours_each_get_their_own_index_line(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ" + b"\0" * 100 + DetectTests.INNO)
        blocks, folders = sample_blocks()
        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 64):
            self.file("data-1.bin", make_archive(blocks, folders))
            self.file("data-2.bin", b"ArC\x01" + b"x" * 100)
            self.file("\x1bodd.bin", make_archive(blocks, folders, locked=True))
            det = d.detect(inst)
        free = {e.where: e for e in det.evidence if e.format == "FreeArc"}
        self.assertEqual(sorted(free), ["large file beside the installer: ?odd.bin",
                                        "large file beside the installer: data-1.bin",
                                        "large file beside the installer: data-2.bin"])
        self.assertEqual(free["large file beside the installer: data-1.bin"].confidence, "likely")
        self.assertEqual(free["large file beside the installer: data-2.bin"].confidence,
                         "possible")
        self.assertIn("locked: yes", free["large file beside the installer: ?odd.bin"].lines)
        from gitfurl.route import choose_route

        choice = choose_route(inst, "p1", det, env={"XDG_DATA_HOME": str(self.dir)})
        self.assertEqual(choice.route, d.ROUTE_WINE)
        self.assertIn("Setup unpacks its own archives", choice.line())

    def test_each_freearc_file_is_opened_once_and_read_from_that_open_file(self):
        # Review finding: the index was read from a second open by path, so the file could
        # be swapped between the signature check and the index read.
        from gitfurl import detect as d

        data = make_archive(*sample_blocks())
        inst = self.file("setup.arc", data)
        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 8):
            other = self.file("data-1.bin", data)
            real_open = os.open
            opened = []

            def counting(path, *a, **kw):
                opened.append(os.fspath(path))
                return real_open(path, *a, **kw)

            with mock.patch("os.open", side_effect=counting), \
                    mock.patch.object(d.farc, "read_archive",
                                      side_effect=AssertionError("opened by path again")):
                det = d.detect(inst)
        self.assertEqual(opened.count(os.fspath(inst)), 1)
        self.assertEqual(opened.count(os.fspath(other)), 1)
        self.assertEqual([e.confidence for e in det.evidence], ["likely", "likely"])

    def test_index_reads_are_bounded_by_the_neighbour_cap(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", b"MZ")
        data = make_archive(*sample_blocks())
        for i in range(6):
            self.file(f"p{i}.bin", data)
        real = d.farc.read_fd
        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 8), \
                mock.patch.object(d, "MAX_NEIGHBOURS", 3), \
                mock.patch.object(d.farc, "read_fd", side_effect=real) as reads:
            det = d.detect(inst)
        self.assertEqual(reads.call_count, 3)
        self.assertEqual(len([e for e in det.evidence if e.format == "FreeArc"]), 3)
        self.assertTrue(any("not all files" in n for n in det.notes))

    def test_files_beside_the_installer_use_the_lower_index_cap(self):
        from gitfurl import detect as d

        inst = self.file("setup.exe", d.FREEARC_SIGNATURE + b"\0" * 4)
        self.file("p.bin", make_archive(*sample_blocks()))
        real = d.farc.read_fd
        with mock.patch.object(d, "MIN_NEIGHBOUR_BYTES", 8), \
                mock.patch.object(d.farc, "read_fd", side_effect=real) as reads:
            d.detect(inst)
        limits = sorted((c.args + (None,))[2] or 0 for c in reads.call_args_list)
        self.assertEqual(limits, [0, d.NEIGHBOUR_INDEX_LIMIT])
        self.assertLess(d.NEIGHBOUR_INDEX_LIMIT, d.farc.MAX_INDEX_TOTAL)

    def test_reading_the_index_writes_nothing_and_runs_nothing(self):
        from gitfurl import detect as d

        path = self.file("a.arc", make_archive(*sample_blocks("lzma:1mb"), index="lzma:1mb"))
        before = (sorted(p.name for p in self.dir.iterdir()), path.stat().st_mode,
                  path.stat().st_mtime_ns, path.read_bytes())
        with mock.patch("subprocess.Popen", side_effect=AssertionError("no process")), \
                mock.patch("subprocess.run", side_effect=AssertionError("no process")), \
                mock.patch("os.system", side_effect=AssertionError("no shell")):
            det = d.detect(path)
        self.assertEqual(det.evidence[0].confidence, "likely")
        self.assertEqual(before, (sorted(p.name for p in self.dir.iterdir()),
                                  path.stat().st_mode, path.stat().st_mtime_ns,
                                  path.read_bytes()))

    def test_dry_run_shows_the_index_summary(self):
        with tempfile.TemporaryDirectory() as data:
            f = self.file("game.arc", make_archive(*sample_blocks()))
            with mock.patch.dict("os.environ", {"XDG_DATA_HOME": data}):
                code, out, _ = run_cli("install", str(f), "--prefix", "p1", "--dry-run")
            self.assertEqual(code, 0)
            self.assertIn("FreeArc: likely", out)
            self.assertIn("2 files and 1 folder, 3.6 KiB in total", out)
            self.assertIn("1 data block: 1 storing, 0 lzma", out)
            self.assertIn("route: native (chosen by gitfurl)", out)
            self.assertNotIn("not available in this version", out)
            self.assertFalse((Path(data) / "gitfurl").exists())

    def test_the_window_uses_the_same_plan_text(self):
        # The approval dialog adds describe_detection(detect(...)) to its text; the window
        # itself is not driven here.
        from gitfurl import detect as d
        from gitfurl import gui

        self.assertIs(gui.describe_detection, d.describe)
        self.assertIs(gui.detect, d.detect)


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
        self.assertIn("measured: size of the files in the installer's folder", text)
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


class FakeClock:
    """Moves forward by `step` seconds every time it is read."""

    def __init__(self, step):
        self.now, self.step = 0.0, step

    def __call__(self):
        self.now += self.step
        return self.now


class RunEventHelpers:
    """Fixture and helpers for runs with a fake /proc (no test methods: tests/test_crash.py
    uses them too)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        t = Path(self.tmp.name)
        self.installer = t / "inst" / "setup.exe"
        self.installer.parent.mkdir()
        self.installer.write_bytes(b"x")
        self.payload = self.installer.parent / "p.bin"
        self.payload.write_bytes(b"x" * (2 << 20))
        self.env = {"XDG_DATA_HOME": str(t / "data"), "XDG_STATE_HOME": str(t / "state"),
                    "SECRET_TOKEN": "hunter2"}
        from gitfurl.runner import build_plan

        self.plan = build_plan(self.installer.resolve(), "p", self.env)
        self.plan.prefix.mkdir(parents=True)
        self.proc = t / "proc"
        self.proc.mkdir()
        self.meminfo = t / "meminfo"
        self.meminfo.write_text("MemTotal: 999 kB\nMemAvailable:  123456 kB\n")

    def fake_prefix_process(self, pos):
        d = self.proc / "100"
        (d / "fd").mkdir(parents=True, exist_ok=True)
        (d / "fdinfo").mkdir(exist_ok=True)
        (d / "environ").write_bytes(f"WINEPREFIX={self.plan.prefix}\0SECRET_TOKEN=hunter2\0".encode())
        stat = ["S"] + ["0"] * 19
        stat[11] = "5"
        (d / "stat").write_text("100 (setup.tmp) " + " ".join(stat))
        link = d / "fd" / "7"
        if not link.is_symlink():
            link.symlink_to(self.payload.resolve())
        (d / "fdinfo" / "7").write_text(f"pos:\t{pos}\nflags:\t0\n")

    def run_it(self, run, **opts):
        from gitfurl.runner import RunOptions, execute

        return execute(self.plan, "/x/wine", self.env, run,
                       RunOptions(**{"progress_interval": 0.05, "proc_root": str(self.proc),
                                     "meminfo": str(self.meminfo), **opts}))

    def sleeper(self, seconds=0.6, write=0):
        def run(argv, **kw):
            import time

            if write:
                (self.plan.prefix / "big.dat").write_bytes(b"y" * write)
            time.sleep(seconds)
            return subprocess.CompletedProcess(argv, 0)

        return run

    @staticmethod
    def events(res):
        import json

        text = Path(res.log_path).with_suffix(".events.jsonl").read_text()
        return [json.loads(line) for line in text.splitlines()], text


class RunEventTests(RunEventHelpers, unittest.TestCase):
    """Stall advisory, process and memory events, and the three-field result. Fakes only."""

    def test_stall_advisory_fires_once_and_never_signals(self):
        notes = []
        with mock.patch("os.kill") as kill, mock.patch("os.killpg") as killpg, \
                mock.patch("subprocess.run") as sub:
            res = self.run_it(self.sleeper(1.0), detailed_log=True, stall_after_s=250,
                              clock=FakeClock(100), on_note=notes.append)
        evs, _ = self.events(res)
        stalls = [e for e in evs if e["event"] == "possibly_stalled"]
        self.assertEqual(len(stalls), 1)
        self.assertTrue(stalls[0]["advisory"])
        self.assertEqual(len(notes), 1)
        self.assertIn("advice only", notes[0])
        self.assertEqual(res.status, 0)  # the run was not touched
        kill.assert_not_called()
        killpg.assert_not_called()
        sub.assert_not_called()

    def test_stall_off_by_default_and_rejects_bad_value(self):
        from gitfurl.runner import RunOptions

        self.assertIsNone(RunOptions().stall_after_s)
        notes = []
        res = self.run_it(self.sleeper(0.4), detailed_log=True, on_progress=lambda s: None,
                          clock=FakeClock(1000), on_note=notes.append)
        evs, _ = self.events(res)
        self.assertEqual(notes, [])
        self.assertNotIn("possibly_stalled", [e["event"] for e in evs])
        with self.assertRaises(ValueError):
            self.run_it(self.sleeper(0.1), stall_after_s=0)

    def test_stall_detector_counts_each_kind_of_movement(self):
        from gitfurl.progress import Sample, StallDetector

        def smp(read=0, cpu=None):
            return Sample(1, read, 100, 0, 0, 1, cpu)

        log = Path(self.tmp.name) / "a.log"
        log.write_text("a")
        d = StallDetector(25, log, FakeClock(10))
        self.assertIsNone(d.check(smp(read=5)))  # read grew
        self.assertIsNone(d.check(smp(read=5, cpu=5.0)))  # cpu used (3% or more)
        log.write_text("ab")
        self.assertIsNone(d.check(smp(read=5, cpu=0.0)))  # log grew
        self.assertIsNone(d.check(smp(read=5, cpu=0.0)))  # idle 10
        self.assertIsNone(d.check(smp(read=5, cpu=0.0)))  # idle 20
        self.assertIsNotNone(d.check(smp(read=5, cpu=0.0)))  # idle 30: fires
        self.assertIsNone(d.check(smp(read=5, cpu=0.0)))  # only once
        # Wine's own background processes use about 1-2% while nothing happens (Wine 11.0,
        # 2026-11-24): that is quiet, not movement.
        d = StallDetector(25, None, FakeClock(10))
        self.assertIsNone(d.check(smp(cpu=1.5)))
        self.assertIsNone(d.check(smp(cpu=2.9)))
        self.assertIsNotNone(d.check(smp(cpu=1.0)))

    def test_processes_and_memory_events_without_env_values(self):
        self.fake_prefix_process(pos=2 << 20)
        res = self.run_it(self.sleeper(0.5), detailed_log=True, on_progress=lambda s: None)
        evs, text = self.events(res)
        procs = [e for e in evs if e["event"] == "processes"]
        mems = [e for e in evs if e["event"] == "memory"]
        self.assertTrue(procs)
        self.assertEqual(procs[0]["count"], 1)
        self.assertEqual(procs[0]["programs"], ["setup.tmp"])
        self.assertEqual(mems[0]["mem_available_bytes"], 123456 * 1024)
        self.assertNotIn("hunter2", text)

    def test_no_events_when_detailed_log_is_off(self):
        self.fake_prefix_process(pos=2 << 20)
        res = self.run_it(self.sleeper(0.4), on_progress=lambda s: None, stall_after_s=0.001,
                          clock=FakeClock(100))
        names = [p.name for p in Path(res.log_path).parent.iterdir()]
        self.assertEqual([n for n in names if n.endswith(".jsonl")], [])

    def test_no_process_or_memory_events_without_the_monitor(self):
        res = self.run_it(self.sleeper(0.2), detailed_log=True)
        evs, _ = self.events(res)
        self.assertEqual([e["event"] for e in evs], ["start", "exit"])

    def test_extraction_observed_when_prefix_grew_and_payload_was_read(self):
        self.fake_prefix_process(pos=2 << 20)
        res = self.run_it(self.sleeper(0.5, write=2 << 20), on_progress=lambda s: None)
        self.assertTrue(res.extraction_observed)
        self.assertTrue(res.observation_measured)
        self.assertEqual(res.exit_status, 0)
        self.assertTrue(res.started)

    def test_extraction_not_observed_with_reads_only(self):
        self.fake_prefix_process(pos=2 << 20)
        res = self.run_it(self.sleeper(0.4), on_progress=lambda s: None)
        self.assertFalse(res.extraction_observed)

    def test_extraction_not_observed_with_writes_only(self):
        res = self.run_it(self.sleeper(0.4, write=2 << 20), on_progress=lambda s: None)
        self.assertFalse(res.extraction_observed)

    def test_extraction_observed_false_when_nothing_read_or_written(self):
        res = self.run_it(self.sleeper(0.4), detailed_log=True, on_progress=lambda s: None)
        self.assertFalse(res.extraction_observed)
        self.assertTrue(res.observation_measured)
        evs, _ = self.events(res)
        last = evs[-1]
        self.assertEqual(last["event"], "exit")
        self.assertIs(last["extraction_observed"], False)
        self.assertIs(last["started"], True)
        self.assertEqual(last["exit_status"], 0)

    def test_result_is_not_measured_without_the_monitor_and_still_unpacks(self):
        from gitfurl.runner import describe_result

        res = self.run_it(self.sleeper(0.1))
        status, log = res
        self.assertEqual((status, log), (res.status, res.log_path))
        self.assertEqual(res[0], 0)
        self.assertFalse(res.observation_measured)
        self.assertIn("not measured", describe_result(res))
        self.assertIsNone(describe_result((0, "x")))

    def test_final_event_shows_the_three_fields_and_wording(self):
        from gitfurl.runner import describe_result

        self.fake_prefix_process(pos=2 << 20)
        res = self.run_it(self.sleeper(0.5, write=2 << 20), detailed_log=True,
                          on_progress=lambda s: None)
        evs, _ = self.events(res)
        self.assertIs(evs[-1]["extraction_observed"], True)
        text = describe_result(res)
        self.assertIn("Observed activity (heuristic)", text)
        self.assertIn("does not show that anything was extracted", text)

    def test_stop_before_start_reports_not_started(self):
        from gitfurl.runner import (
            CANCELLED_STATUS,
            RunControl,
            RunOptions,
            describe_result,
            execute,
        )

        control = RunControl()
        control.request_stop()
        with mock.patch("subprocess.Popen") as popen:
            res = execute(self.plan, "/x/wine", self.env,
                          options=RunOptions(control=control, detailed_log=True))
        popen.assert_not_called()
        self.assertFalse(res.started)
        self.assertIsNone(res.exit_status)
        self.assertEqual(res.status, CANCELLED_STATUS)
        evs, _ = self.events(res)
        self.assertEqual(evs[-1]["event"], "cancelled")
        self.assertIs(evs[-1]["started"], False)
        self.assertIn("Started: no", describe_result(res))

    def test_cli_end_text_and_stall_flag(self):
        from gitfurl.runner import TrackedResult

        res = TrackedResult(0, Path(self.tmp.name) / "x.log", True, False, True, 0)
        seen = {}

        def fake(plan, wine, create, track, options=None):
            seen["opts"] = options
            return res

        with mock.patch.dict("os.environ", self.env), \
                mock.patch("shutil.which", return_value="/x/wine"), \
                mock.patch("gitfurl.cli.run_tracked", side_effect=fake):
            code, out, _ = run_cli("install", "--no-memory-limit", str(self.installer), "--prefix", "p", "--yes",
                                   "--stall-after", "900")
            bad, _, err = run_cli("install", "--no-memory-limit", str(self.installer), "--prefix", "p", "--yes",
                                  "--stall-after", "0")
        self.assertEqual(code, 0)
        self.assertEqual(seen["opts"].stall_after_s, 900)
        self.assertIn("Observed activity (heuristic): no", out)
        self.assertIn("Exit status: 0", out)
        self.assertEqual(bad, 1)
        self.assertIn("--stall-after must be above 0", err)
