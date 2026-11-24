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
     0x1e0000b "": ("setup.tmp" "setup.tmp")  300x100+0+0  +0+0
     0x1e0000d "Default IME": ("setup.tmp" "setup.tmp")  300x100+0+0  +0+0
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
        self.unmapped = set()

    def fake(self, pids_by_window, tree=TREE, fail=None):
        def run(argv, **kw):
            self.calls.append(argv)
            self.assertLessEqual(kw["timeout"], windows.TIMEOUT_S)
            if fail == argv[0].rsplit("/", 1)[-1]:
                raise subprocess.TimeoutExpired(argv, 2)
            if argv[0].endswith("xwininfo") and "-id" in argv:
                state = "IsUnMapped" if argv[2] in self.unmapped else "IsViewable"
                return subprocess.CompletedProcess(argv, 0, f"  Map State: {state}\n", "")
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
        tree = "".join(f'     0x{n:x} "W{n}": ("setup.tmp" "setup.tmp")  300x100+0+0  +0+0\n'
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
        tree = "".join(f'     0x{n:x} "W{n}": ("setup.tmp" "setup.tmp")  300x100+0+0  +0+0\n'
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
        self.assertIn('Window open in the run: "Error"; it may be waiting for an answer',
                      windows.titles_text(["Error"]))
        self.assertIn('"Error", "Setup"; one of them may be waiting for an answer',
                      windows.titles_text(["Error", "Setup"]))


class CrashNoteTests(RunEventHelpers, unittest.TestCase):
    def run_it(self, run, **opts):
        # With stall advice on, the run reads windows through window_list (one look per
        # sample); a titles-only fake given as window_titles is adapted to it here.
        titles = opts.get("window_titles")
        if titles is not None and "window_list" not in opts and "stall_after_s" in opts:
            def lister(pids, programs, env, pid_of=None):
                got = titles(pids, programs, env)
                return None if got is None else [(t, t) for t in got]
            opts["window_list"] = lister
        return super().run_it(run, **opts)

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
        self.assertIn('Window open in the run: "Error"', stall[0])
        self.assertIsNone(res.crash_seen_s)
        self.assertNotIn("Crash:", describe_result(res))

    def test_stall_note_names_the_newest_window(self):
        self.fake_prefix_process(0)
        notes, calls = [], []

        def lister(pids, programs, env, pid_of=None):
            calls.append(1)
            main = [("0x1", "Setup - Game")]
            return main if len(calls) == 1 else main + [("0x2", "Error")]

        asked = []
        res = self.run_it(self.sleeper(1.0), stall_after_s=250, clock=FakeClock(100),
                          on_note=notes.append, window_list=lister, detailed_log=True,
                          window_titles=lambda *a: asked.append(a) or [])
        stall = [n for n in notes if n.startswith("possibly stalled")]
        self.assertTrue(stall)
        self.assertIn('Windows open in the run: "Setup - Game", "Error"', stall[0])
        self.assertIn('The newest of them, "Error"', stall[0])
        self.assertGreaterEqual(len(calls), 2)  # one look per progress sample
        self.assertEqual(asked, [])  # and no second look for the note
        evs, _text = self.events(res)
        ev = [e for e in evs if e["event"] == "possibly_stalled"][0]
        self.assertEqual(ev["newest_window"], "Error")
        self.assertIsInstance(ev["newest_window_s"], int)
        self.assertIsNone(ev["top_window"])
        self.assertNotIn("The one on top", stall[0])

    def test_stall_note_names_the_top_window_of_several_open_from_the_start(self):
        self.fake_prefix_process(0)
        notes = []
        res = self.run_it(self.sleeper(1.0), stall_after_s=250, clock=FakeClock(100),
                          on_note=notes.append, detailed_log=True,
                          window_list=lambda *a, **k: [("0x6", "Select Setup Language"),
                                                       ("0x1", "Setup - Game")])
        stall = [n for n in notes if n.startswith("possibly stalled")]
        self.assertTrue(stall)
        self.assertIn('The one on top, "Select Setup Language", is the most likely', stall[0])
        self.assertNotIn("The newest of them", stall[0])
        evs, _text = self.events(res)
        ev = [e for e in evs if e["event"] == "possibly_stalled"][0]
        self.assertEqual((ev["newest_window"], ev["top_window"]),
                         (None, "Select Setup Language"))

    def test_one_window_open_from_the_start_is_listed_not_named_twice(self):
        # The sandbox run of 2026-11-24: the language dialog was open at the first look.
        self.fake_prefix_process(0)
        notes = []
        res = self.run_it(self.sleeper(1.0), stall_after_s=250, clock=FakeClock(100),
                          on_note=notes.append, detailed_log=True,
                          window_list=lambda *a, **k: [("0x6", "Select Setup Language")])
        stall = [n for n in notes if n.startswith("possibly stalled")]
        self.assertTrue(stall)
        self.assertIn('Window open in the run: "Select Setup Language"; it may be waiting',
                      stall[0])
        self.assertNotIn("The one on top", stall[0])  # one window: the list says it all
        self.assertNotIn("The newest of them", stall[0])
        evs, _text = self.events(res)
        ev = [e for e in evs if e["event"] == "possibly_stalled"][0]
        self.assertEqual((ev["newest_window"], ev["top_window"]),
                         (None, "Select Setup Language"))

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



class WindowTrackerTests(unittest.TestCase):
    def test_newest_window_after_the_first_look(self):
        looks = [[("0x1", "Setup")], [("0x1", "Setup"), ("0x2", "Error")],
                 [("0x1", "Setup"), ("0x2", "Error")]]
        t = windows.WindowTracker({}, lambda *a, **k: looks.pop(0))
        t.observe([1], ["setup.tmp"], 3.0)
        self.assertIsNone(t.newest())  # one look: the main window is not "new"
        t.observe([1], ["setup.tmp"], 6.0)
        t.observe([1], ["setup.tmp"], 9.0)
        self.assertEqual(t.newest(), ("Error", 6.0))
        text = windows.newest_text(t.newest(), 70.0)
        self.assertIn('"Error", opened 6 s into the run (64 s ago)', text)
        self.assertEqual(windows.newest_text(None, 1.0), "")

    def test_windows_present_from_the_start_are_not_named(self):
        t = windows.WindowTracker({}, lambda *a, **k: [("0x1", "Setup"), ("0x2", "Other")])
        for s in (3.0, 6.0):
            t.observe([1], ["setup.tmp"], s)
        self.assertIsNone(t.newest())

    def test_unknown_looks_and_a_failing_lister_change_nothing(self):
        def broken(*a, **k):
            raise RuntimeError("x")

        t = windows.WindowTracker({}, broken)
        t.observe([1], ["a"], 1.0)
        self.assertIsNone(t.newest())
        t.lister = lambda *a, **k: None
        t.observe([1], ["a"], 2.0)
        self.assertEqual(t.observations, 0)


class WindowListCacheTests(WindowTitleTests):
    def test_xprop_only_for_new_windows(self):
        run = self.fake({"0x1e00007": 11, "0x1e00009": 11, "0x3c00001": 11})
        cache = {}
        first = windows.window_list([11], ["setup.tmp", "averyveryverylo"], self.env, run,
                                    pid_of=cache)
        asked = [c for c in self.calls if c[0].endswith("xprop")]
        self.assertTrue(first)
        self.calls.clear()
        again = windows.window_list([11], ["setup.tmp", "averyveryverylo"], self.env, run,
                                    pid_of=cache)
        self.assertEqual(first, again)
        self.assertEqual([c for c in self.calls if c[0].endswith("xprop")], [])
        self.assertEqual(len(cache), len(asked))


class WindowListOrderTests(WindowTitleTests):
    def tree(self, n):
        return "".join(f'     0x{i:x} "W{i}": ("setup.tmp" "setup.tmp")  300x100+0+0  +0+0\n'
                       for i in range(1, n + 1))

    def test_the_caps_keep_the_topmost_windows(self):
        run = self.fake({f"0x{i:x}": 11 for i in range(1, 21)}, tree=self.tree(20))
        got = windows.window_list([11], ["setup.tmp"], self.env, run)
        # xwininfo lists the topmost window first (seen with Xvfb in the sandbox).
        self.assertEqual([t for _w, t in got], [f"W{i}" for i in range(1, 7)])

    def test_hidden_helper_windows_are_left_out(self):
        # As listed in the sandbox run of 2026-11-24 (an Inno Setup installer at its
        # language dialog): only the dialog is a window a user can answer.
        tree = (
            '     0xe00006 "Select Setup Language": ("setup.tmp" "setup.tmp")  297x125+171+190  +171+190\n'
            '     0xe00003 "Setup": ("setup.tmp" "setup.tmp")  1x1+320+240  +320+240\n'
            '     0xa00001 "InnoSetupLdrWindow": ("setup.exe" "setup.exe")  113x2+3+29  +3+29\n')
        run = self.fake({"0xe00006": 11, "0xe00003": 11, "0xa00001": 12}, tree=tree)
        got = windows.window_titles([11, 12], ["setup.tmp", "setup.exe"], self.env, run)
        self.assertEqual(got, ["Select Setup Language"])
        checked = [c[2] for c in self.calls if c[0].endswith("xprop")]
        self.assertEqual(checked, ["0xe00006"])  # no probe for the hidden ones

    def test_an_unmapped_window_is_left_out(self):
        # A hidden form of normal size, created after the dialog, is listed on top.
        tree = ('     0x9 "Hidden form": ("setup.tmp" "setup.tmp")  400x300+0+0  +0+0\n'
                '     0x6 "Select Setup Language": ("setup.tmp" "setup.tmp")  297x125+0+0  +0+0\n')
        self.unmapped = {"0x9"}
        run = self.fake({"0x9": 11, "0x6": 11}, tree=tree)
        self.assertEqual(windows.window_titles([11], ["setup.tmp"], self.env, run),
                         ["Select Setup Language"])
        self.unmapped = set()  # shown later: listed then
        self.assertEqual(windows.window_titles([11], ["setup.tmp"], self.env, run),
                         ["Hidden form", "Select Setup Language"])

    def test_a_line_without_a_size_is_kept(self):
        tree = '     0x7 "Error": ("setup.tmp" "setup.tmp")\n'
        run = self.fake({"0x7": 11}, tree=tree)
        self.assertEqual(windows.window_titles([11], ["setup.tmp"], self.env, run), ["Error"])

    def test_closed_windows_leave_the_cache(self):
        cache = {"0x99": 11, "0x1": 11}
        run = self.fake({"0x2": 11}, tree=self.tree(2))
        windows.window_list([11], ["setup.tmp"], self.env, run, pid_of=cache)
        self.assertEqual(cache, {"0x1": 11, "0x2": 11})  # 0x99 is gone from the tree


class TrackerBaselineTests(unittest.TestCase):
    def test_baseline_stays_when_the_main_window_closes(self):
        looks = [[("a", "Setup")], [("b", "Unpacking")], [("b", "Unpacking")]]
        t = windows.WindowTracker({}, lambda *a, **k: looks.pop(0))
        for s in (3.0, 6.0, 9.0):
            t.observe([1], ["x"], s)
        self.assertEqual(t.newest(), ("Unpacking", 6.0))

    def test_top_window_when_nothing_opened_after_the_first_look(self):
        looks = [[("d", "Select Setup Language")], [("d", "Select Setup Language")]]
        t = windows.WindowTracker({}, lambda *a, **k: looks.pop(0))
        for s in (10.0, 20.0):
            t.observe([1], ["x"], s)
        self.assertIsNone(t.newest())
        self.assertEqual(t.top(), "Select Setup Language")
        self.assertIn('The one on top, "Select Setup Language"', windows.top_text(t.top()))
        self.assertEqual(windows.top_text(None), "")

    def test_a_failed_look_forgets_the_open_windows(self):
        looks = [[("a", "Setup")], [("a", "Setup"), ("b", "Error")], None]
        t = windows.WindowTracker({}, lambda *a, **k: looks.pop(0))
        for s in (3.0, 6.0):
            t.observe([1], ["x"], s)
        self.assertIsNotNone(t.newest())
        self.assertIsNone(t.observe([1], ["x"], 9.0))
        self.assertIsNone(t.newest())


if __name__ == "__main__":
    unittest.main()
