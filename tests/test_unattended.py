"""The unattended Inno Setup route. Synthetic files and a mocked launcher only."""

import contextlib
import io
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from gitfurl import cli
from gitfurl.detect import detect
from gitfurl.runner import build_plan, describe_result
from gitfurl.unattended import (
    INNO_SWITCHES,
    LOG_READ_LIMIT,
    UnattendedRefused,
    plan_unattended,
    read_report,
)

INNO = b"MZ" + b"\0" * 200 + b"Inno Setup Setup Data (6.4.0) (u)" + b"\0" * 40
NOW = datetime(2026, 11, 17, 18, 0, 0)


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


@contextlib.contextmanager
def patched_runs(side_effect=None):
    run = mock.Mock(return_value=subprocess.CompletedProcess([], 0), side_effect=side_effect)
    # The mocked launcher never starts a real group, so the run is marked as started here.
    with mock.patch("subprocess.run", run), mock.patch("gitfurl.prefixes.run_in_group", run), \
            mock.patch("gitfurl.runner._run_in_group", run), \
            mock.patch("gitfurl.runner.RunControl.started", new_callable=mock.PropertyMock,
                       return_value=True):
        yield run


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.setup = self.dir / "setup.exe"
        self.setup.write_bytes(INNO)

    def tearDown(self):
        self.tmp.cleanup()

    def test_inno_gets_the_documented_switches_and_a_folder_in_the_prefix(self):
        up = plan_unattended("my-game", detect(self.setup), NOW)
        self.assertEqual(up.args(), [*INNO_SWITCHES, r"/DIR=C:\Games\my-game",
                                     r"/LOG=C:\gitfurl-setup-20261117-180000.log"])
        prefix = self.dir / "pfx"
        self.assertEqual(up.folder(prefix), prefix / "drive_c" / "Games" / "my-game")
        self.assertEqual(up.log_path(prefix), prefix / "drive_c" / "gitfurl-setup-20261117-180000.log")
        text = "\n".join(up.describe(prefix))
        self.assertIn("/VERYSILENT", text)
        self.assertIn("ignore these switches", text)

    def test_refused_without_an_inno_id(self):
        other = self.dir / "other.exe"
        other.write_bytes(b"MZ" + b"\0" * 500)
        with self.assertRaises(UnattendedRefused):
            plan_unattended("p1", detect(other), NOW)

    def test_bad_prefix_name_is_refused(self):
        with self.assertRaises(ValueError):
            plan_unattended("../x", detect(self.setup), NOW)

    def test_switches_follow_the_installer_in_argv(self):
        up = plan_unattended("p1", detect(self.setup), NOW)
        with mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(self.dir)}):
            plan = build_plan(self.setup, "p1", unattended=up)
            plain = build_plan(self.setup, "p1")
        self.assertEqual(plan.argv[:2], ["wine", str(self.setup)])
        self.assertEqual(plan.argv[2:], up.args())
        self.assertEqual(plan.installer, self.setup)
        self.assertEqual(plain.argv, ["wine", str(self.setup)])
        self.assertIsNone(plain.unattended)


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.prefix = Path(self.tmp.name) / "p1"
        (self.prefix / "drive_c").mkdir(parents=True)
        setup = Path(self.tmp.name) / "setup.exe"
        setup.write_bytes(INNO)
        self.up = plan_unattended("p1", detect(setup), NOW)

    def tearDown(self):
        self.tmp.cleanup()

    def write_log(self, text):
        self.up.log_path(self.prefix).write_text(text, encoding="utf-8")

    def test_success_log(self):
        self.up.folder(self.prefix).mkdir(parents=True)
        self.write_log("Log opened.\n-- File entry --\nDest filename: C:\\Games\\p1\\a.bin\n"
                       "-- File entry --\nDest filename: C:\\Games\\p1\\b.bin\n"
                       "Installation process succeeded.\nLog closed.\n")
        rep = read_report(self.up, self.prefix, 0)
        self.assertTrue(rep.log_found and rep.succeeded and rep.folder_exists)
        self.assertEqual(rep.files, 2)
        text = "\n".join(rep.lines())
        self.assertIn("exit status 0: Setup finished", text)
        self.assertIn("has its 'Installation process succeeded.' line", text)
        self.assertIn("not steps the setup script runs after it", text)
        self.assertNotIn("says the installation succeeded", text)
        self.assertIn("2 file(s)", text)

    def test_cancelled_run_with_the_success_line_is_not_a_success(self):
        self.write_log("Dest filename: C:\\Games\\p1\\a.bin\n"
                       "Installation process succeeded.\n")
        rep = read_report(self.up, self.prefix, None, cancelled=True)
        self.assertTrue(rep.succeeded and rep.cancelled)
        text = "\n".join(rep.lines())
        self.assertTrue(text.startswith("The run was cancelled, so Setup did not finish."))
        self.assertIn("did not finish", text)
        self.assertIn("before the cancel", text)
        self.assertNotIn("says the installation succeeded", text)

    def test_cancelled_run_without_a_log(self):
        text = "\n".join(read_report(self.up, self.prefix, None, cancelled=True).lines())
        self.assertIn("Setup did not finish", text)
        self.assertIn("was not found", text)

    def test_missing_log(self):
        rep = read_report(self.up, self.prefix, 1)
        self.assertFalse(rep.log_found)
        text = "\n".join(rep.lines())
        self.assertIn("failed to initialize", text)
        self.assertIn("was not found", text)

    def test_errors_are_quoted_cleaned_and_capped(self):
        lines = [f"Exception message: bad \x1b[31mthing {i}" for i in range(9)]
        self.write_log("\n".join(lines) + "\nFatal exception during installation.\n")
        rep = read_report(self.up, self.prefix, 4)
        self.assertFalse(rep.succeeded)
        self.assertEqual(len(rep.errors), 5)
        self.assertNotIn("\x1b", "".join(rep.errors))
        text = "\n".join(rep.lines())
        self.assertIn("fatal error happened during the installation", text)
        self.assertIn("has no 'Installation process succeeded.' line", text)
        self.assertIn("folder", text)

    def test_only_the_end_of_a_huge_log_is_read(self):
        self.write_log("x" * (LOG_READ_LIMIT + 10) + "\nInstallation process succeeded.\n")
        with mock.patch("builtins.open", wraps=open) as op:
            rep = read_report(self.up, self.prefix, 0)
        self.assertTrue(rep.succeeded)
        self.assertEqual(op.call_count, 1)

    def test_invalid_utf8_does_not_raise(self):
        self.up.log_path(self.prefix).write_bytes(b"\xff\xfe Error \x80\nInstallation process succeeded.\n")
        rep = read_report(self.up, self.prefix, 0)
        self.assertTrue(rep.succeeded)
        self.assertEqual(len(rep.errors), 1)

    def test_unknown_exit_status_has_no_meaning_line(self):
        rep = read_report(self.up, self.prefix, 42)
        self.assertFalse(any("exit status 42" in ln for ln in rep.lines()))


class CliTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        base = Path(self.tmp.name)
        self.data, self.state = base / "data", base / "state"
        self.setup = base / "setup.exe"
        self.setup.write_bytes(INNO)
        self.env = mock.patch.dict("os.environ", {"XDG_DATA_HOME": str(self.data),
                                                   "XDG_STATE_HOME": str(self.state)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_dry_run_shows_switches_and_runs_nothing(self):
        with patched_runs() as run:
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                                   "--unattended", "--no-memory-limit")
        self.assertEqual(code, 0)
        run.assert_not_called()
        self.assertIn("/VERYSILENT", out)
        self.assertIn("unattended install (Inno Setup): on", out)
        self.assertIn("route: wine (chosen by gitfurl)", out)
        self.assertNotIn("not available in this version", out)

    def test_off_by_default(self):
        code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run",
                               "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertNotIn("/VERYSILENT", out)

    def test_not_inno_is_refused_before_anything_runs(self):
        other = Path(self.tmp.name) / "other.exe"
        other.write_bytes(b"MZ" + b"\0" * 100)
        (self.data / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)
        with mock.patch("shutil.which", return_value="/x/wine"), patched_runs() as run:
            code, _, err = run_cli("install", str(other), "--prefix", "p1", "--yes",
                                   "--unattended", "--no-memory-limit")
        self.assertEqual(code, 1)
        run.assert_not_called()
        self.assertIn("only works with Inno Setup", err)

    def test_real_run_passes_switches_and_reports_the_inno_log(self):
        prefix = self.data / "gitfurl" / "prefixes" / "p1"
        (prefix / "drive_c").mkdir(parents=True)
        seen = {}

        def fake_run(argv, **kw):
            seen["argv"] = list(argv)
            log_arg = next(a for a in argv if a.startswith("/LOG="))
            name = log_arg.split("\\")[-1]
            (prefix / "drive_c" / "Games" / "p1").mkdir(parents=True)
            (prefix / "drive_c" / name).write_text(
                "Dest filename: C:\\Games\\p1\\x.bin\nInstallation process succeeded.\n")
            return subprocess.CompletedProcess(argv, 0)

        with mock.patch("shutil.which", return_value="/x/wine"), patched_runs(fake_run):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--unattended", "--no-memory-limit")
        self.assertEqual(code, 0)
        self.assertEqual(seen["argv"][:2], ["/x/wine", str(self.setup)])
        self.assertIn("/VERYSILENT", seen["argv"])
        self.assertIn(r"/DIR=C:\Games\p1", seen["argv"])
        self.assertIn("has its 'Installation process succeeded.' line", out)
        self.assertNotIn("says the installation succeeded", out)
        self.assertIn("1 file(s)", out)
        logs = list((self.state / "gitfurl" / "logs").glob("p1-*.log"))
        self.assertEqual(len(logs), 1)
        self.assertIn("covers Setup's own file list", logs[0].read_text())

    def test_failed_run_reports_inno_exit_meaning(self):
        prefix = self.data / "gitfurl" / "prefixes" / "p1"
        (prefix / "drive_c").mkdir(parents=True)
        with mock.patch("shutil.which", return_value="/x/wine"), \
                patched_runs(lambda argv, **kw: subprocess.CompletedProcess(argv, 4)):
            code, out, _ = run_cli("install", str(self.setup), "--prefix", "p1", "--yes",
                                   "--unattended", "--no-memory-limit")
        self.assertNotEqual(code, 0)
        self.assertIn("fatal error happened during the installation", out)
        self.assertIn("was not found", out)


class CancelledRunTests(unittest.TestCase):
    """A run stopped after Inno's success line is reported as cancelled everywhere."""

    def test_runner_reports_the_cancel_in_text_log_and_events(self):
        import json

        from gitfurl.runner import RunControl, RunOptions, execute

        with tempfile.TemporaryDirectory() as d:
            base = Path(d)
            setup = base / "setup.exe"
            setup.write_bytes(INNO)
            env = {"XDG_DATA_HOME": str(base / "data"), "XDG_STATE_HOME": str(base / "state"),
                   "HOME": d, "PATH": "/nonexistent"}
            up = plan_unattended("p1", detect(setup), NOW)
            with mock.patch.dict("os.environ", env):
                plan = build_plan(setup, "p1", unattended=up)
            (plan.prefix / "drive_c").mkdir(parents=True)
            control = RunControl()

            def fake_run(argv, **kw):
                up.log_path(plan.prefix).write_text(
                    "Dest filename: C:\\Games\\p1\\a.bin\nInstallation process succeeded.\n")
                control.cancelled = True  # as a stop after the success line would
                return subprocess.CompletedProcess(argv, -2)

            res = execute(plan, "wine", env, run=fake_run,
                          options=RunOptions(control=control, detailed_log=True))
            self.assertEqual(res.status, 130)
            self.assertTrue(res.unattended.cancelled)
            text = describe_result(res)
            self.assertIn("Setup did not finish", text)
            self.assertNotIn("says the installation succeeded", text)
            log_text = Path(res.log_path).read_text()
            self.assertIn("The run was cancelled, so Setup did not finish.", log_text)
            events_path = Path(str(res.log_path)[:-4] + ".events.jsonl")
            events = [json.loads(ln) for ln in events_path.read_text().splitlines()]
            result = next(e for e in events if e["event"] == "unattended_result")
            self.assertTrue(result["cancelled"])
            self.assertTrue(result["succeeded"])


class ResultTextTests(unittest.TestCase):
    def test_describe_result_includes_unattended_lines(self):
        from gitfurl.runner import ExecResult
        from gitfurl.unattended import UnattendedReport

        res = ExecResult(0, Path("/x.log"), exit_status=0,
                         unattended=UnattendedReport(0, log_path=Path("/p/l.log")))
        self.assertIn("was not found", describe_result(res))


class GuiPlanTests(unittest.TestCase):
    def test_describe_plan_with_and_without_unattended(self):
        try:
            from gitfurl.gui import describe_plan
        except ImportError as exc:  # no tkinter
            self.skipTest(str(exc))
        with tempfile.TemporaryDirectory() as d, \
                mock.patch.dict("os.environ", {"XDG_DATA_HOME": d}):
            setup = Path(d) / "setup.exe"
            setup.write_bytes(INNO)
            off, err0 = describe_plan(str(setup), "p1")
            on, err1 = describe_plan(str(setup), "p1", unattended=True, now=NOW)
            other = Path(d) / "o.exe"
            other.write_bytes(b"MZ")
            _, err2 = describe_plan(str(other), "p1", unattended=True, now=NOW)
        self.assertIsNone(err0)
        self.assertIsNone(err1)
        self.assertNotIn("/VERYSILENT", off)
        self.assertIn("gitfurl-setup-20261117-180000.log", on)
        self.assertIn("only works with Inno Setup", err2)


if __name__ == "__main__":
    unittest.main()
