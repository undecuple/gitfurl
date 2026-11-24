"""Tests for the optional ClamAV scan. Fakes only: ClamAV itself is never run.

A fake `clamscan` shell script on a temporary PATH prints configured output, exits with a
configured status and records its arguments. No real installer, Wine or network is used.
"""

import io
import json
import math
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest import mock

from gitfurl import cli, scan
from gitfurl.procgroup import group_alive
from gitfurl.runner import RunControl, RunOptions, build_plan, run_with_prefix
from gitfurl.scan import (
    CLEAN,
    ERROR,
    FORBIDDEN_OPTIONS,
    HIT,
    ScanBlocked,
    build_scan_plan,
    decide,
    describe_scan_plan,
    interpret,
    parse_version,
    run_scan,
)

SRC = str(Path(__file__).resolve().parent.parent / "src")
VERSION = "ClamAV 1.4.3/27780/Thu Oct  2 04:58:32 2025"


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = cli.main(list(argv))
    return code, out.getvalue(), err.getvalue()


class Fixture(unittest.TestCase):
    """A temp installer folder, a fake clamscan and fake wine on a temp PATH."""

    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.t = Path(self._t.name)
        self.inst_dir = self.t / "inst"
        self.inst_dir.mkdir()
        self.installer = self.inst_dir / "setup.exe"
        self.installer.write_bytes(b"MZ test")
        (self.inst_dir / "data-1.bin").write_bytes(b"x")
        (self.inst_dir / "sub").mkdir()
        (self.inst_dir / "sub" / "deep.bin").write_bytes(b"x")
        (self.inst_dir / "link.bin").symlink_to(self.inst_dir / "data-1.bin")
        self.bin = self.t / "bin"
        self.bin.mkdir()
        self.rec = self.t / "clamscan.args"
        self.out = self.t / "clamscan.out"
        self.code = self.t / "clamscan.code"
        self.pid = self.t / "clamscan.pid"
        self.wine_rec = self.t / "wine.called"
        self.clamscan = self.fake_clamscan()
        self.wine = self._script(self.bin / "wine", f'echo "$@" >> "{self.wine_rec}"\nexit 0\n')
        self.env = {"PATH": str(self.bin), "HOME": str(self.t / "home"),
                    "XDG_DATA_HOME": str(self.t / "data"), "XDG_STATE_HOME": str(self.t / "state")}

    def tearDown(self):
        self._t.cleanup()

    def _script(self, path, body):
        path.write_text("#!/bin/sh\n" + body)
        path.chmod(path.stat().st_mode | stat.S_IXUSR)
        return str(path)

    def fake_clamscan(self, sleep=None, version=VERSION):
        body = (f'if [ "$1" = "--version" ]; then printf "%s\\n" "{version}"; exit 0; fi\n'
                f'echo $$ > "{self.pid}"\n'
                f'printf "%s\\n" "$@" > "{self.rec}"\n')
        if sleep:
            body += f"{shutil.which('sleep')} {sleep}\n"
        cat = shutil.which("cat")
        body += f'{cat} "{self.out}"\nread code < "{self.code}"\nexit "$code"\n'
        return self._script(self.bin / "clamscan", body)

    def respond(self, lines, code):
        self.out.write_bytes(b"".join(
            (ln.encode("utf-8", "surrogateescape") if isinstance(ln, str) else ln) + b"\n"
            for ln in lines))
        self.code.write_text(str(code))

    def plan(self, timeout=30):
        return build_scan_plan(self.installer, self.clamscan, timeout)

    def ok_lines(self, plan):
        return [f"{f}: OK" for f in plan.files]

    def argv_seen(self):
        return self.rec.read_text().splitlines()


class ArgvTests(Fixture):
    def test_argv_is_read_only_and_names_only_the_expected_files(self):
        p = self.plan()
        self.assertEqual(p.argv[:3], [self.clamscan, "--no-summary", "--stdout"])
        self.assertEqual(p.files, (str(self.installer.resolve()),
                                   str(self.inst_dir.resolve() / "data-1.bin")))
        self.assertEqual(p.skipped, 2)  # the subfolder and the symlink
        for arg in p.argv:
            for bad in FORBIDDEN_OPTIONS:
                self.assertFalse(arg.startswith(bad), arg)
        self.assertNotIn("-r", p.argv)
        self.assertNotIn("--recursive", p.argv)
        self.assertTrue(all(os.path.isabs(a) for a in p.files))

    def test_fake_records_exactly_the_planned_argv(self):
        p = self.plan()
        self.respond(self.ok_lines(p), 0)
        run_scan(p)
        self.assertEqual(self.argv_seen(), p.argv[1:])

    def test_missing_clamscan_is_refused_with_a_reason(self):
        with self.assertRaises(ValueError) as cm:
            build_scan_plan(self.installer, None)
        self.assertIn("clamscan was not found on PATH", str(cm.exception))
        with self.assertRaises(ValueError):
            build_scan_plan(self.installer, "clamscan")  # relative
        with self.assertRaises(ValueError):
            build_scan_plan(self.installer, self.clamscan, 0)

    def test_line_break_in_a_name_is_refused(self):
        (self.inst_dir / "a\nb.bin").write_bytes(b"x")
        with self.assertRaises(ValueError):
            self.plan()

    def test_other_line_separators_in_a_name_are_refused(self):
        for ch in ("\x0b", "\x0c", "\x1c", "\x85", "\u2028"):
            bad = self.inst_dir / f"a{ch}b.bin"
            bad.write_bytes(b"x")
            with self.assertRaises(ValueError, msg=repr(ch)) as cm:
                self.plan()
            self.assertIn("line-separator", str(cm.exception))
            self.assertNotIn(ch, str(cm.exception))
            bad.unlink()
        self.plan()  # fine again

    def test_non_finite_timeout_is_refused(self):
        for value in (math.nan, math.inf, -math.inf, -1):
            with self.assertRaises(ValueError, msg=value):
                build_scan_plan(self.installer, self.clamscan, value)

    def test_more_than_1000_entries_is_refused(self):
        for i in range(scan.MAX_FILES):
            (self.inst_dir / f"f{i:04d}.bin").touch()
        with self.assertRaises(ValueError) as cm:
            self.plan()
        self.assertIn("more than 1000 entries", str(cm.exception))

    def test_fifo_beside_the_installer_is_skipped_and_nothing_blocks(self):
        fifo = self.inst_dir / "pipe"
        os.mkfifo(fifo)
        p = self.plan()
        self.assertNotIn(str(fifo), p.files)
        self.assertNotIn(str(fifo), p.large)
        self.assertEqual(p.skipped, 3)  # the subfolder, the symlink and the pipe
        self.respond(self.ok_lines(p), 0)
        box = []
        th = threading.Thread(target=lambda: box.append(run_scan(p)), daemon=True)
        th.start()
        th.join(20)
        self.assertFalse(th.is_alive(), "the scan blocked on a named pipe")
        self.assertEqual(box[0].status, CLEAN)
        self.assertNotIn(str(fifo), self.argv_seen())

    def test_find_clamscan_refuses_a_relative_result(self):
        with mock.patch("shutil.which", return_value="clamscan"):
            self.assertIsNone(scan.find_clamscan())
        with mock.patch("shutil.which", return_value="/usr/bin/clamscan"):
            self.assertEqual(scan.find_clamscan(), "/usr/bin/clamscan")


class ResultTests(Fixture):
    def test_clean(self):
        p = self.plan()
        self.respond(self.ok_lines(p), 0)
        res = run_scan(p)
        self.assertEqual(res.status, CLEAN, res.reason)
        ok, msg = decide(res)
        self.assertTrue(ok)
        self.assertIn("no known signature matched (ClamAV, database dated 2025-10-02)", msg)
        self.assertNotIn("safe", msg.lower())
        self.assertNotIn("clean of", msg.lower())

    def test_files_over_the_default_size_limit_are_named_not_called_clean(self):
        big = self.inst_dir / "data-2.bin"
        with open(big, "wb") as fh:  # sparse: the size counts, no disk is used
            fh.truncate(150 * 1000 * 1000)
        p = self.plan()
        self.assertEqual(p.large, (str(big),))
        text = "\n".join(describe_scan_plan(p))
        self.assertIn("may not have been scanned completely: data-2.bin", text)
        self.respond(self.ok_lines(p), 0)
        res = run_scan(p)
        ok, msg = decide(res)
        self.assertTrue(ok)
        self.assertIn("no known signature matched in the parts ClamAV scanned", msg)
        self.assertIn("larger than ClamAV's default per-file limit (100 MB)", msg)
        self.assertIn("data-2.bin", msg)
        self.assertEqual(self.plan().large, (str(big),))
        small = self.plan()
        self.assertNotIn(str(self.inst_dir / "data-1.bin"), small.large)

    def test_hit_blocks_and_override_proceeds(self):
        p = self.plan()
        self.respond([f"{p.files[0]}: OK", f"{p.files[1]}: Win.Test.EICAR_HDB-1 FOUND"], 1)
        res = run_scan(p)
        self.assertEqual(res.status, HIT)
        ok, msg = decide(res)
        self.assertFalse(ok)
        self.assertIn("data-1.bin: Win.Test.EICAR_HDB-1", msg)
        self.assertIn("falsely", msg)
        self.assertIn("--allow-scan-hit", msg)
        self.assertIn("nothing was run or created", msg)
        ok, msg = decide(res, allow_hit=True)
        self.assertTrue(ok)

    def test_hit_with_an_error_line_is_an_error_not_overridable(self):
        p = self.plan()
        self.respond([f"{p.files[0]}: Win.Test.Sig\x1b[1m FOUND",
                      f"{p.files[1]}: Can't read file ERROR"], 1)
        res = run_scan(p)
        self.assertEqual(res.status, ERROR, res.reason)
        self.assertIn("a known signature matched", res.reason)
        self.assertIn("could not be scanned or read", res.reason)
        for allow in (False, True):
            ok, msg = decide(res, allow_hit=allow)
            self.assertFalse(ok)
            self.assertIn("setup.exe: Win.Test.Sig?[1m", msg)  # the match is named, cleaned
            self.assertNotIn("\x1b", msg)
            self.assertNotIn("run it anyway", msg)

    def test_hit_with_a_file_without_result_is_an_error(self):
        p = self.plan()
        self.respond([f"{p.files[0]}: Win.Test FOUND"], 1)  # nothing for data-1.bin
        res = run_scan(p)
        self.assertEqual(res.status, ERROR, res.reason)
        self.assertIn("no result for", res.reason)
        self.assertIn("Win.Test", res.reason)
        self.assertFalse(decide(res, allow_hit=True)[0])

    def test_hit_with_an_unreadable_line_is_an_error(self):
        files = ("/a/setup.exe",)
        res = interpret(1, "/a/setup.exe: X FOUND\nnoise\n", files)
        self.assertEqual(res.status, ERROR)
        self.assertFalse(decide(res, allow_hit=True)[0])

    def test_error_exit_blocks(self):
        p = self.plan()
        self.respond([f"{p.files[0]}: Can't open file ERROR"], 2)
        res = run_scan(p)
        self.assertEqual(res.status, ERROR)
        self.assertFalse(decide(res)[0])
        self.assertFalse(decide(res, allow_hit=True)[0])  # an override never covers an error

    def test_unparseable_output_blocks(self):
        p = self.plan()
        self.respond([*self.ok_lines(p), "something unexpected"], 0)
        self.assertEqual(run_scan(p).status, ERROR)
        self.respond(["garbage only"], 0)
        self.assertEqual(run_scan(p).status, ERROR)
        self.respond([], 0)  # no result for any file
        self.assertEqual(run_scan(p).status, ERROR)
        self.respond(["garbage"], 1)  # "found" without a readable file
        self.assertEqual(run_scan(p).status, ERROR)

    def test_unexpected_exit_codes_block(self):
        p = self.plan()
        for code in (3, 40, 255):
            self.respond(self.ok_lines(p), code)
            self.assertEqual(run_scan(p).status, ERROR, code)

    def test_inconsistent_found_with_exit_0_blocks(self):
        files = ("/a/setup.exe",)
        self.assertEqual(interpret(0, "/a/setup.exe: X FOUND\n", files).status, ERROR)
        self.assertEqual(interpret(0, "/a/setup.exe: OK\n", files, truncated=True).status, ERROR)
        self.assertEqual(interpret(0, "/a/setup.exe: Empty file\n", files).status, CLEAN)

    def test_control_characters_are_cleaned(self):
        evil = self.inst_dir / "evil\x1b[31m\x07.bin"
        evil.write_bytes(b"x")
        p = self.plan()
        lines = [f"{f}: OK" for f in p.files if "evil" not in f]
        lines.append(f"{evil.resolve()}: Win.Bad\x1b]0;title\x07Sig FOUND")
        self.respond(lines, 1)
        res = run_scan(p)
        self.assertEqual(res.status, HIT)
        _, msg = decide(res)
        for ch in ("\x1b", "\x07"):
            self.assertNotIn(ch, msg)
        self.assertIn("evil?[31m?.bin", msg)
        self.assertNotIn("\x1b", "\n".join(scan.describe_scan_plan(p)))

    def test_timeout_kills_only_its_own_group_and_leaves_nothing(self):
        self.fake_clamscan(sleep=60)
        p = self.plan(timeout=1)
        self.respond(self.ok_lines(p), 0)
        real_killpg = os.killpg
        with mock.patch("os.killpg", side_effect=real_killpg) as kp, \
                mock.patch("os.kill") as kill:
            t0 = time.monotonic()
            res = run_scan(p)
        self.assertLess(time.monotonic() - t0, 15)
        self.assertEqual(res.status, ERROR)
        self.assertIn("did not finish within 1 s", res.reason)
        pgid = int(self.pid.read_text())
        self.assertTrue(kp.called)
        self.assertEqual({c.args[0] for c in kp.call_args_list}, {pgid})
        kill.assert_not_called()
        self.assertFalse(group_alive(pgid))
        self.assertFalse(decide(res)[0])

    def test_stop_request_ends_the_scan(self):
        self.fake_clamscan(sleep=60)
        p = self.plan(timeout=60)
        self.respond(self.ok_lines(p), 0)
        t0 = time.monotonic()
        res = run_scan(p, should_stop=lambda: time.monotonic() - t0 > 0.5)
        self.assertEqual(res.status, ERROR)
        self.assertIn("stopped by you", res.reason)
        self.assertFalse(group_alive(int(self.pid.read_text())))

    def test_child_ignoring_sigterm_is_killed_after_the_grace_period(self):
        sh = shutil.which("sh")
        sleep = shutil.which("sleep")
        child_pid = self.t / "child.pid"
        self._script(self.bin / "clamscan",
                     f'if [ "$1" = "--version" ]; then echo "{VERSION}"; exit 0; fi\n'
                     f'echo $$ > "{self.pid}"\n'
                     f"{sh} -c 'trap \"\" TERM; echo $$ > \"{child_pid}\"; "
                     f"while :; do {sleep} 1; done' &\n"
                     f"trap '' TERM\n"
                     f"wait\n")
        p = self.plan(timeout=1)
        t0 = time.monotonic()
        res = run_scan(p)
        took = time.monotonic() - t0
        self.assertEqual(res.status, ERROR)
        self.assertGreaterEqual(took, 1 + scan.STOP_GRACE_S - 0.5)
        self.assertLess(took, 20)
        self.assertTrue(any("force-killed" in n for n in res.notes), res.notes)
        self.assertTrue(child_pid.exists())  # the TERM-ignoring child had started
        self.assertFalse(group_alive(int(self.pid.read_text())))  # leader and child gone

    def test_ctrl_c_during_the_scan_stops_its_group(self):
        if threading.current_thread() is not threading.main_thread():
            self.skipTest("SIGINT is only delivered on the main thread")
        self.fake_clamscan(sleep=60)
        p = self.plan(timeout=60)
        self.respond(self.ok_lines(p), 0)

        def press():
            for _ in range(200):  # wait until the fake has started
                if self.pid.exists() and self.pid.read_text().strip():
                    break
                time.sleep(0.05)
            time.sleep(0.3)
            os.kill(os.getpid(), signal.SIGINT)  # our own process only

        th = threading.Thread(target=press)
        th.start()
        t0 = time.monotonic()
        with self.assertRaises(KeyboardInterrupt) as cm:
            run_scan(p)
        th.join(10)
        self.assertLess(time.monotonic() - t0, 20)
        self.assertTrue(cm.exception.scan_interrupted)
        self.assertIn("the installer was not started", cm.exception.notes[0])
        self.assertFalse(group_alive(int(self.pid.read_text())))
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_failure_after_the_start_stops_the_group_and_re_raises(self):
        self.fake_clamscan(sleep=60)
        p = self.plan(timeout=60)
        self.respond(self.ok_lines(p), 0)
        real = scan._reader
        calls = []

        def second_fails(*a):
            calls.append(1)
            if len(calls) == 2:
                for _ in range(200):  # let the fake record its pid first
                    if self.pid.exists() and self.pid.read_text().strip():
                        break
                    time.sleep(0.05)
                raise RuntimeError("can't start new thread")
            return real(*a)

        with mock.patch("gitfurl.scan._reader", side_effect=second_fails), \
                self.assertRaises(RuntimeError) as cm:
            run_scan(p)
        self.assertTrue(cm.exception.scan_interrupted)
        self.assertFalse(group_alive(int(self.pid.read_text())))
        self.assertIs(signal.getsignal(signal.SIGINT), signal.default_int_handler)

    def test_clamscan_vanished_after_planning_is_an_error(self):
        p = self.plan()
        os.unlink(self.clamscan)
        res = run_scan(p)
        self.assertEqual(res.status, ERROR)
        self.assertIn("could not start clamscan", res.reason)


class VersionTests(unittest.TestCase):
    def test_version_line(self):
        info = parse_version(VERSION + "\n")
        self.assertEqual(info.engine, "1.4.3")
        self.assertEqual(info.db_version, "27780")
        self.assertEqual(info.db_date, datetime(2025, 10, 2, 4, 58, 32))
        self.assertIsNone(info.problem)
        lines = scan.describe_database(info, now=datetime(2025, 10, 5))
        self.assertFalse(any("WARNING" in ln for ln in lines))
        lines = scan.describe_database(info, now=datetime(2025, 10, 20))
        self.assertTrue(any("WARNING" in ln and "17 days old" in ln for ln in lines))
        self.assertTrue(any("freshclam" in ln for ln in lines))

    def test_no_database_and_garbage(self):
        self.assertIn("no signature database", parse_version("ClamAV 1.4.3\n").problem)
        info = parse_version("weird\x1b output\n")
        self.assertEqual(info.line, "weird? output")
        self.assertIsNotNone(info.problem)
        info = parse_version("ClamAV 1.4.3/27780/not a date\n")
        self.assertIsNone(info.db_date)
        self.assertIn("date", info.problem)

    def test_invalid_utf8_version_output_does_not_raise(self):
        with tempfile.TemporaryDirectory() as d:
            fake = Path(d) / "clamscan"
            fake.write_bytes(b"#!/bin/sh\nprintf 'ClamAV 1.4.3/27780/\\377\\376 bad\\n'\n")
            fake.chmod(0o700)
            info = scan.database_info(str(fake))
            self.assertTrue(info.line.startswith("ClamAV 1.4.3/27780/"))
            self.assertIsNotNone(info.problem)
            from gitfurl import doctor

            with mock.patch("shutil.which", return_value=str(fake)):
                rep = doctor.probe_tool("clamscan", ["--version"])
            self.assertIsNone(rep.problem)
            self.assertIn("\ufffd", rep.version)

    def test_doctor_reports_clamscan(self):
        from gitfurl import doctor

        r = doctor.ToolReport("x", "/x", "v", None)
        base = {"platform": "p", "wine": r, "bwrap": r, "umu": r, "innoextract": r, "tkinter": r}
        text = doctor.format_report({**base, "clamscan": doctor.ToolReport(
            "clamscan", "/usr/bin/clamscan", VERSION, None)})
        self.assertIn("clamscan: ClamAV 1.4.3 at /usr/bin/clamscan (optional, for --scan", text)
        self.assertIn("dated 2025-10-02", text)
        self.assertIn("never updates", text)
        text = doctor.format_report({**base, "clamscan": doctor.ToolReport(
            "clamscan", None, None, "not found on PATH")})
        self.assertIn("clamscan: unavailable (not found on PATH)", text)
        text = doctor.format_report({**base, "clamscan": doctor.ToolReport(
            "clamscan", "/usr/bin/clamscan", "ClamAV 1.5.4", None)})
        self.assertIn("clamscan: ClamAV 1.5.4 at /usr/bin/clamscan", text)
        self.assertIn("no signature database reported", text)
        text = doctor.format_report({**base, "clamscan": doctor.ToolReport(
            "clamscan", "/usr/bin/clamscan", "odd", None)})
        self.assertIn("clamscan: version not recognised at /usr/bin/clamscan", text)


class RunGateTests(Fixture):
    """The scan inside run_with_prefix: before the prefix is created and before the run."""

    def setUp(self):
        super().setUp()
        self.calls = []

    def fake_run(self, argv, **kw):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0)

    def go(self, options=None):
        p = self.plan()
        plan = build_plan(self.installer.resolve(), "p1", env=self.env, scan=p)
        with mock.patch.dict("os.environ", self.env):
            return plan, run_with_prefix(plan, self.wine, True, env=self.env, run=self.fake_run,
                                         options=options)

    def test_hit_blocks_with_nothing_run_or_created(self):
        p = self.plan()
        self.respond([f"{p.files[0]}: Win.Test FOUND", f"{p.files[1]}: OK"], 1)
        with self.assertRaises(ScanBlocked) as cm:
            self.go()
        self.assertIn("Win.Test", str(cm.exception))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.t / "data").exists())
        self.assertFalse((self.t / "state").exists())
        self.assertEqual(sorted(x.name for x in self.inst_dir.iterdir()),
                         ["data-1.bin", "link.bin", "setup.exe", "sub"])

    def test_hit_with_an_error_blocks_even_with_the_override(self):
        # The app offers its second dialog only for status HIT (gui.py), so not for this.
        p = self.plan()
        self.respond([f"{p.files[0]}: Win.Test FOUND", f"{p.files[1]}: Can't read ERROR"], 1)
        with self.assertRaises(ScanBlocked) as cm:
            self.go(RunOptions(allow_scan_hit=True))
        self.assertEqual(cm.exception.result.status, ERROR)
        self.assertIn("Win.Test", str(cm.exception))
        self.assertEqual(self.calls, [])
        self.assertFalse((self.t / "data").exists())

    def test_hit_with_override_runs_and_logs_the_scan(self):
        p = self.plan()
        self.respond([f"{p.files[0]}: Win.Test FOUND", f"{p.files[1]}: OK"], 1)
        notes = []
        _, res = self.go(RunOptions(allow_scan_hit=True, on_note=notes.append))
        self.assertEqual(len(self.calls), 2)  # wineboot, then the installer
        self.assertEqual(res.status, 0)
        log = Path(res.log_path).read_text()
        self.assertIn("scan: ClamAV scan: a known signature matched", log)
        self.assertIn("You chose to run it anyway", notes[0])

    def test_error_blocks_with_nothing_created(self):
        self.respond(["oops"], 2)
        with self.assertRaises(ScanBlocked):
            self.go()
        self.assertEqual(self.calls, [])
        self.assertFalse((self.t / "data").exists())

    def test_clean_runs(self):
        p = self.plan()
        self.respond(self.ok_lines(p), 0)
        _, res = self.go()
        self.assertEqual(res.status, 0)
        self.assertIn("no known signature matched", Path(res.log_path).read_text())

    def test_given_result_is_not_scanned_again(self):
        p = self.plan()
        self.respond([f"{p.files[0]}: Win.Test FOUND", f"{p.files[1]}: OK"], 1)
        hit = run_scan(p)
        self.assertEqual(hit.status, HIT)
        self.rec.unlink()
        with mock.patch("gitfurl.scan.run_scan") as rs:
            self.go(RunOptions(scan_result=hit, allow_scan_hit=True))
        rs.assert_not_called()
        self.assertFalse(self.rec.exists())

    def test_cancel_before_start_stops_the_scan_and_creates_nothing(self):
        self.fake_clamscan(sleep=60)
        p = self.plan()
        self.respond(self.ok_lines(p), 0)
        control = RunControl()
        control.request_stop()  # before the start: cancelled
        with self.assertRaises(ScanBlocked) as cm:
            self.go(RunOptions(control=control))
        self.assertIn("stopped by you", str(cm.exception))
        self.assertEqual(self.calls, [])

    def test_scan_off_changes_nothing(self):
        plan = build_plan(self.installer.resolve(), "p1", env=self.env)
        self.assertIsNone(plan.scan)
        self.assertEqual(plan.argv, ["wine", str(self.installer.resolve())])
        with mock.patch("gitfurl.scan.run_scan") as rs, mock.patch.dict("os.environ", self.env):
            res = run_with_prefix(plan, self.wine, True, env=self.env, run=self.fake_run)
        rs.assert_not_called()
        self.assertFalse(self.rec.exists())
        self.assertNotIn("scan:", Path(res.log_path).read_text())


class CliTests(Fixture):
    def make_prefix(self):
        (self.t / "data" / "gitfurl" / "prefixes" / "p1").mkdir(parents=True)

    def test_dry_run_prints_argv_and_runs_nothing(self):
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            code, out, _ = run_cli("install", str(self.installer), "--dry-run", "--scan",
                                   "--prefix", "p1")
        self.assertEqual(code, 0)
        run.assert_not_called()
        popen.assert_not_called()
        inst = str(self.installer.resolve())
        self.assertIn(f"scan command: {self.clamscan} --no-summary --stdout {inst} ", out)
        self.assertIn("not checked in a dry run", out)
        self.assertFalse(self.rec.exists())
        self.assertFalse((self.t / "data").exists())

    def test_dry_run_without_scan_has_no_scan_text(self):
        with mock.patch.dict("os.environ", self.env):
            code, out, _ = run_cli("install", str(self.installer), "--dry-run")
        self.assertEqual(code, 0)
        self.assertNotIn("ClamAV", out)
        self.assertNotIn("clamscan", out)

    def test_allow_scan_hit_needs_scan(self):
        with mock.patch.dict("os.environ", self.env):
            code, _, err = run_cli("install", str(self.installer), "--dry-run",
                                   "--allow-scan-hit")
        self.assertEqual(code, 1)
        self.assertIn("--scan", err)

    def test_missing_clamscan_blocks_with_reason(self):
        os.unlink(self.clamscan)
        self.make_prefix()
        with mock.patch.dict("os.environ", self.env):
            code, _, err = run_cli("install", str(self.installer), "--prefix", "p1", "--no-memory-limit", "--scan",
                                   "--yes")
        self.assertEqual(code, 1)
        self.assertIn("clamscan was not found on PATH", err)
        self.assertIn("Nothing was run", err)
        self.assertFalse(self.wine_rec.exists())

    def test_approval_text_shows_argv_and_database_before_approval(self):
        self.make_prefix()
        with mock.patch.dict("os.environ", self.env), \
                mock.patch("sys.stdin.isatty", return_value=False):
            code, out, _ = run_cli("install", str(self.installer), "--prefix", "p1", "--no-memory-limit", "--scan")
        self.assertEqual(code, 2)  # not approved
        self.assertIn("scan command:", out)
        self.assertIn("dated 2025-10-02", out)
        self.assertIn("WARNING: the signature database is", out)
        self.assertIn("The scan runs first, after you approve", out)
        self.assertFalse(self.rec.exists())  # only the version probe ran, not the scan
        self.assertFalse(self.wine_rec.exists())

    def test_hit_blocks_and_override_runs(self):
        self.make_prefix()
        p = self.plan()
        self.respond([f"{p.files[0]}: Win.Test FOUND", f"{p.files[1]}: OK"], 1)
        with mock.patch.dict("os.environ", self.env):
            code, _, err = run_cli("install", str(self.installer), "--prefix", "p1", "--no-memory-limit", "--scan",
                                   "--yes")
        self.assertEqual(code, 1)
        self.assertIn("Win.Test", err)
        self.assertIn("--allow-scan-hit", err)
        self.assertFalse(self.wine_rec.exists())
        self.assertFalse((self.t / "state").exists())
        with mock.patch.dict("os.environ", self.env):
            code, out, _ = run_cli("install", str(self.installer), "--prefix", "p1", "--no-memory-limit", "--scan",
                                   "--allow-scan-hit", "--yes")
        self.assertEqual(code, 0)
        self.assertTrue(self.wine_rec.exists())
        self.assertIn("You chose to run it anyway", out)

    def test_bad_scan_timeout(self):
        with mock.patch.dict("os.environ", self.env):
            code, _, err = run_cli("install", str(self.installer), "--dry-run", "--scan",
                                   "--scan-timeout", "0")
        self.assertEqual(code, 1)
        self.assertIn("--scan-timeout", err)
        for value in ("nan", "inf", "-inf"):
            with mock.patch.dict("os.environ", self.env):
                code, out, err = run_cli("install", str(self.installer), "--dry-run", "--scan",
                                         f"--scan-timeout={value}")
            self.assertEqual(code, 1, value)
            self.assertIn("--scan-timeout must be a finite number", err)
            self.assertNotIn("scan command", out)


class GuiTests(Fixture):
    def test_option_state(self):
        from gitfurl.gui import scan_option_state

        ok, why = scan_option_state(None)
        self.assertFalse(ok)
        self.assertIn("clamscan was not found on PATH", why)
        ok, why = scan_option_state("/usr/bin/clamscan")
        self.assertTrue(ok)
        self.assertIn("no known signature matched", why)

    def test_describe_plan_shows_argv_only_when_on(self):
        from gitfurl.gui import describe_plan

        with mock.patch.dict("os.environ", self.env), \
                mock.patch("subprocess.run") as run, mock.patch("subprocess.Popen") as popen:
            text, err = describe_plan(str(self.installer), "p1")
            self.assertIsNone(err)
            self.assertNotIn("clamscan", text)
            text, err = describe_plan(str(self.installer), "p1", scan=True)
            self.assertIsNone(err)
            self.assertIn(f"scan command: {self.clamscan} --no-summary --stdout", text)
            os.unlink(self.clamscan)
            text, err = describe_plan(str(self.installer), "p1", scan=True)
        self.assertEqual(text, "")
        self.assertIn("clamscan was not found", err)
        run.assert_not_called()
        popen.assert_not_called()

    def window(self, clamscan):
        if shutil.which("xvfb-run") is None:
            self.skipTest("xvfb-run not available")
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        code = textwrap.dedent(f'''
            import json, sys
            import gitfurl.scan as s
            s.find_clamscan = lambda: {clamscan!r}
            from gitfurl.gui import main

            def drive(root, ui):
                for _ in range(30):
                    root.update()
                sw = ui["scan_switch"]["w"]
                r = {{"value": ui["scan_var"].get(), "enabled": sw.enabled,
                      "reason": ui["scan_reason"]["text"]}}
                sw._toggle()
                r["after_toggle"] = ui["scan_var"].get()
                print("RESULT " + json.dumps(r))
                root.destroy()

            sys.exit(main(start="details", driver=drive))
        ''')
        env = {**os.environ, "PYTHONPATH": SRC, "HOME": str(self.t / "home"),
               "XDG_DATA_HOME": str(self.t / "data"), "XDG_STATE_HOME": str(self.t / "state")}
        proc = subprocess.run(["xvfb-run", "-a", sys.executable, "-c", code], env=env,
                              capture_output=True, text=True, check=False, timeout=120)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT ")]
        self.assertEqual(proc.returncode, 0, proc.stderr[-1500:])
        return json.loads(lines[-1][7:])

    def test_window_option_disabled_with_reason_without_clamscan(self):
        r = self.window(None)
        self.assertFalse(r["value"])
        self.assertFalse(r["enabled"])
        self.assertIn("clamscan was not found on PATH", r["reason"])
        self.assertFalse(r["after_toggle"])  # a disabled switch does not turn on

    def test_window_option_default_off_when_available(self):
        r = self.window("/opt/fake/clamscan")
        self.assertFalse(r["value"])
        self.assertTrue(r["enabled"])
        self.assertTrue(r["after_toggle"])


if __name__ == "__main__":
    unittest.main()
