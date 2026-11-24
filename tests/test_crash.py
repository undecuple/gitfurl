"""Crash and stuck-window notes: Wine's crash handler in the prefix and the run's window
titles. Fakes only (a fake /proc and a fake xwininfo/xprop); nothing is run."""

import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from test_gitfurl import FakeClock, RunEventHelpers
except ImportError:  # python -m unittest tests.test_crash
    from tests.test_gitfurl import FakeClock, RunEventHelpers

from gitfurl import windows
from gitfurl.progress import prefix_processes
from gitfurl.runner import ExecResult, describe_result


def fake_proc(root: Path, pid: int, comm: str, prefix: str):
    d = root / str(pid)
    d.mkdir(parents=True)
    (d / "environ").write_bytes(f"WINEPREFIX={prefix}\0".encode())
    (d / "stat").write_text(f"{pid} ({comm}) " + " ".join(["S"] + ["0"] * 19))


class PrefixProcessesTests(unittest.TestCase):
    def test_crash_handler_and_pids(self):
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as pre:
            fake_proc(Path(root), 11, "setup.tmp", pre)
            fake_proc(Path(root), 12, "wineserver", pre)
            fake_proc(Path(root), 13, "winedbg.exe", "/elsewhere")  # another prefix
            a = prefix_processes(Path(pre), root)
            self.assertEqual((a.count, a.pids, a.names, a.crash_handler),
                             (2, (11, 12), ("setup.tmp",), False))
            fake_proc(Path(root), 14, "winedbg.exe", pre)
            a = prefix_processes(Path(pre), root)
        self.assertTrue(a.crash_handler)
        self.assertEqual(a.pids, (11, 12, 14))
        self.assertNotIn("winedbg.exe", a.names)  # Wine's own, not shown as a helper

    def test_crash_handler_named_without_exe(self):
        # Wine 11.0 names it "winedbg" in /proc (seen in the sandbox on 2026-11-24).
        with tempfile.TemporaryDirectory() as root, tempfile.TemporaryDirectory() as pre:
            fake_proc(Path(root), 11, "rundll32.exe", pre)
            fake_proc(Path(root), 12, "winedbg", pre)
            a = prefix_processes(Path(pre), root)
        self.assertEqual(a.crash_pids, (12,))
        self.assertEqual(a.names, ("rundll32.exe",))


TREE = '''
xwininfo: Window id: 0x1d5 (the root window) (has no name)

  Root window id: 0x1d5 (the root window) (has no name)
  Parent window id: 0x0 (none)
     5 children:
     0x1e00007 "Error": ("setup.tmp" "setup.tmp")  400x120+0+0  +300+300
     0x1e00009 "Setup - Game": ("setup.tmp" "setup.tmp")  500x380+0+0  +200+200
     0x2a00003 "Error": ("kitty" "kitty")  800x600+0+0  +0+0
     0x1e0000b "": ("setup.tmp" "setup.tmp")  1x1+0+0  +0+0
     0x1e0000d "Default IME": ("setup.tmp" "setup.tmp")  1x1+0+0  +0+0
     0x3c00001 "Other "quoted" C:\\dir\\": ("averyveryverylongname.exe" "averyveryverylongname.exe")  10x10+0+0  +0+0
'''


class WindowTitleTests(unittest.TestCase):
    def setUp(self):
        self.bin = tempfile.TemporaryDirectory()
        self.addCleanup(self.bin.cleanup)
        for tool in ("xwininfo", "xprop"):
            p = Path(self.bin.name) / tool
            p.write_text("#!/bin/sh\nexit 1\n")
            p.chmod(0o700)
        self.env = {"DISPLAY": ":9", "PATH": self.bin.name}
        self.calls = []

    def fake(self, pids_by_window, tree=TREE, fail=None):
        def run(argv, **kw):
            self.calls.append(argv)
            self.assertLessEqual(kw["timeout"], windows.TIMEOUT_S)
            if fail == argv[0].rsplit("/", 1)[-1]:
                raise subprocess.TimeoutExpired(argv, 2)
            if argv[0].endswith("xwininfo"):
                return subprocess.CompletedProcess(argv, 0, tree, "")
            pid = pids_by_window.get(argv[2])
            out = f"_NET_WM_PID(CARDINAL) = {pid}\n" if pid else "_NET_WM_PID:  not found.\n"
            return subprocess.CompletedProcess(argv, 0, out, "")
        return run

    def test_titles_of_the_runs_own_windows(self):
        run = self.fake({"0x1e00007": 11, "0x1e00009": 11, "0x2a00003": 11, "0x3c00001": 12})
        got = windows.window_titles([11, 12], ["setup.tmp", "averyveryverylo"], self.env, run)
        self.assertEqual(got, ["Error", "Setup - Game", 'Other "quoted" C:\\dir\\'])
        checked = [c[2] for c in self.calls if c[0].endswith("xprop")]
        self.assertNotIn("0x2a00003", checked)  # another program's window: class differs
        self.assertNotIn("0x1e0000b", checked)  # no title
        self.assertNotIn("0x1e0000d", checked)  # Wine's hidden IME window

    def test_a_window_of_another_process_is_left_out(self):
        run = self.fake({"0x1e00007": 99, "0x1e00009": 11})
        self.assertEqual(windows.window_titles([11], ["setup.tmp"], self.env, run),
                         ["Setup - Game"])

    def test_unknown_without_display_tools_or_on_failure(self):
        run = self.fake({})
        self.assertIsNone(windows.window_titles([11], ["setup.tmp"], {"PATH": self.bin.name},
                                                run))
        self.assertIsNone(windows.window_titles([11], ["setup.tmp"],
                                                {"DISPLAY": ":9", "PATH": "/nonexistent"}, run))
        self.assertIsNone(windows.window_titles([11], ["setup.tmp"], self.env,
                                                self.fake({}, fail="xwininfo")))
        self.assertEqual(windows.window_titles([], ["setup.tmp"], self.env, run), [])
        self.assertEqual([c[0].rsplit("/", 1)[-1] for c in self.calls], ["xwininfo"])  # the failing one

    def test_checks_are_bounded(self):
        tree = "".join(f'     0x{n:x} "W{n}": ("setup.tmp" "setup.tmp")  1x1+0+0  +0+0\n'
                       for n in range(100))
        run = self.fake({}, tree=tree)
        self.assertEqual(windows.window_titles([11], ["setup.tmp"], self.env, run), [])
        self.assertEqual(sum(c[0].endswith("xprop") for c in self.calls), windows.MAX_CHECKED)

    def test_crash_dialog_of_winedbg_is_listed(self):
        tree = '     0x4400001 "Program Error": ("winedbg.exe" "winedbg.exe")  400x200+0+0  +0+0\n'
        run = self.fake({"0x4400001": 14}, tree=tree)
        self.assertEqual(windows.window_titles([11, 14], ["setup.tmp", "winedbg.exe"],
                                               self.env, run), ["Program Error"])

    def test_one_overall_deadline(self):
        tree = "".join(f'     0x{n:x} "W{n}": ("setup.tmp" "setup.tmp")  1x1+0+0  +0+0\n'
                       for n in range(10))
        clock = iter([0.0, 1.0, 2.5, 3.5, 9.0, 9.0])
        run = self.fake({}, tree=tree)
        with mock.patch("gitfurl.windows.time.monotonic", lambda: next(clock)):
            windows.window_titles([11], ["setup.tmp"], self.env, run)
        timeouts = [c for c in self.calls if c[0].endswith("xprop")]
        self.assertEqual(len(timeouts), 2)  # the third check is past the 3 s deadline

    def test_titles_text(self):
        self.assertEqual(windows.titles_text(None), "")
        self.assertEqual(windows.titles_text([]), "")
        self.assertIn('"Error"; one of them may be waiting for an answer',
                      windows.titles_text(["Error"]))


class CrashNoteTests(RunEventHelpers, unittest.TestCase):
    def crash_process(self):
        d = self.proc / "101"
        d.mkdir()
        (d / "environ").write_bytes(f"WINEPREFIX={self.plan.prefix}\0".encode())
        (d / "stat").write_text("101 (winedbg.exe) " + " ".join(["S"] + ["0"] * 19))

    def test_crash_handler_gives_one_note_an_event_and_a_result_line(self):
        self.fake_prefix_process(0)
        notes, seen = [], []
        sleeper = self.sleeper(0.4)

        def run(argv, **kw):
            self.crash_process()  # the crash handler appears while the run is going
            return sleeper(argv, **kw)

        def titles(pids, programs, env):
            seen.append((tuple(pids), tuple(programs)))
            return ["Program Error"]

        with mock.patch("os.kill") as kill, mock.patch("os.killpg") as killpg:
            res = self.run_it(run, detailed_log=True, on_note=notes.append,
                              window_titles=titles)
        crash_notes = [n for n in notes if n.startswith("crash:")]
        self.assertEqual(len(crash_notes), 1, notes)
        self.assertIn("winedbg", crash_notes[0])
        self.assertIn('"Program Error"', crash_notes[0])
        self.assertEqual(seen[0][0], (100, 101))
        self.assertEqual(set(seen[0][1]), {"setup.tmp", "winedbg.exe", "winedbg"})
        evs, _ = self.events(res)
        crash = [e for e in evs if e["event"] == "crash_handler"]
        self.assertEqual(len(crash), 1)
        self.assertEqual(crash[0]["windows"], ["Program Error"])
        self.assertIsNotNone(res.crash_seen_s)
        self.assertEqual(res.status, 0)  # nothing was stopped
        kill.assert_not_called()
        killpg.assert_not_called()
        self.assertIn("crash handler (winedbg) was seen", Path(res.log_path).read_text())
        self.assertIn("Crash: Wine's crash handler", describe_result(res))

    def test_stall_note_lists_the_runs_windows(self):
        self.fake_prefix_process(0)
        notes = []
        res = self.run_it(self.sleeper(1.0), stall_after_s=250, clock=FakeClock(100),
                          on_note=notes.append,
                          window_titles=lambda pids, programs, env: ["Error"])
        stall = [n for n in notes if n.startswith("possibly stalled")]
        self.assertEqual(len(stall), 1)
        self.assertIn('Windows open in the run: "Error"', stall[0])
        self.assertIsNone(res.crash_seen_s)
        self.assertNotIn("Crash:", describe_result(res))

    def test_no_crash_no_line_and_a_failing_window_reader_is_ignored(self):
        self.fake_prefix_process(0)

        def broken(*a):
            raise RuntimeError("x")

        notes = []
        res = self.run_it(self.sleeper(1.0), stall_after_s=250, clock=FakeClock(100),
                          on_note=notes.append, window_titles=broken)
        self.assertEqual(len([n for n in notes if n.startswith("possibly stalled")]), 1)
        self.assertNotIn("Windows open", notes[0])
        self.assertEqual(res.status, 0)

    def test_crash_handler_from_before_the_run_is_not_counted(self):
        self.fake_prefix_process(0)
        self.crash_process()  # already there when the run starts
        notes = []
        res = self.run_it(self.sleeper(0.4), detailed_log=True, on_note=notes.append,
                          window_titles=lambda *a: [])
        self.assertIsNone(res.crash_seen_s)
        self.assertEqual([n for n in notes if n.startswith("crash:")], [])
        self.assertIn("already running in this prefix before the run",
                      Path(res.log_path).read_text())

    def test_crash_seen_only_in_the_final_sample(self):
        self.fake_prefix_process(0)
        real_sleeper = self.sleeper(0.2)

        def run(argv, **kw):
            self.crash_process()  # appears after the start, before the final sample
            return real_sleeper(argv, **kw)

        notes = []
        res = self.run_it(run, detailed_log=True, on_note=notes.append, progress_interval=3600,
                          window_titles=lambda *a: [])
        self.assertIsNotNone(res.crash_seen_s)
        self.assertIn("Crash: Wine's crash handler (winedbg) was seen", describe_result(res))
        self.assertIn("crash handler (winedbg) was seen", Path(res.log_path).read_text())

    def test_portable_run_does_not_ask_for_windows(self):
        from dataclasses import replace

        self.fake_prefix_process(0)
        asked = []
        self.plan = replace(self.plan, portable=True)
        with mock.patch("gitfurl.portable.find_bwrap", return_value="/x/bwrap"), \
                mock.patch("gitfurl.portable.build_portable_argv", return_value=["/x/bwrap"]):
            res = self.run_it(self.sleeper(1.0), stall_after_s=250, clock=FakeClock(100),
                              detailed_log=True, on_note=lambda n: None,
                              window_titles=lambda *a: asked.append(a) or ["X"])
        self.assertEqual(asked, [])
        evs, _ = self.events(res)
        self.assertIsNone([e for e in evs if e["event"] == "possibly_stalled"][0]["windows"])

    def test_describe_result_without_a_crash(self):
        r = ExecResult(0, Path("/x.log"), exit_status=0, observation_measured=True)
        self.assertNotIn("Crash", describe_result(r))


if __name__ == "__main__":
    unittest.main()
