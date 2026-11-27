"""The run timeline (gitfurl.timeline): formatting, folding, caps and cleaning, the file
(mode 0600, O_EXCL, written as rows arrive), rows from a mocked Wine run and from a native
unpack of synthetic FreeArc archives, and `gitfurl cli log`. No Wine, no real installer."""

import os
import stat
import subprocess
import tempfile
import threading
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

try:
    from test_gitfurl import FakeClock, RunEventHelpers
    from test_native_run import NativeBase
    from test_route import no_process, run_cli
except ImportError:  # python -m unittest tests.test_timeline
    from tests.test_gitfurl import FakeClock, RunEventHelpers
    from tests.test_native_run import NativeBase
    from tests.test_route import no_process, run_cli

from gitfurl import timeline as tlm
from gitfurl.timeline import SEP, Timeline


class Clock:
    """09:00:00, then one second later at each call."""

    def __init__(self):
        self.t = datetime(2026, 11, 27, 9, 0, 0)

    def __call__(self):
        now = self.t
        self.t += timedelta(seconds=1)
        return now


class FormatTests(unittest.TestCase):
    def test_one_line_per_entry_with_and_without_a_reason(self):
        tl = Timeline(clock=Clock())
        tl.add("plan", "Route: native", "every block uses lzma")
        tl.add("approval", "You approved the plan")
        self.assertEqual(tl.lines(), [f"09:00:00  Route: native{SEP}every block uses lzma",
                                      "09:00:01  You approved the plan"])

    def test_progress_rows_fold_into_one_that_updates(self):
        tl = Timeline(clock=Clock())
        tl.add("start", "Started")
        for done in (1, 2, 3):
            tl.progress(tlm.progress_text("Reading the installer's data", done * 10**9,
                                          38 * 10**8))
        self.assertEqual(len(tl.lines()), 2)
        self.assertEqual(tl.lines()[1],
                         "09:00:03  Reading the installer's data: 78 %, 3.0 GB of 3.8 GB")
        tl.add("note", "Setup exited with status 0")
        tl.progress("Reading again")  # after another row: a new progress row
        self.assertEqual(len(tl.lines()), 4)

    def test_progress_text_matches_the_example(self):
        self.assertEqual(tlm.progress_text("Reading the installer's data", 16 * 10**8,
                                           38 * 10**8),
                         "Reading the installer's data: 42 %, 1.6 GB of 3.8 GB")
        self.assertEqual(tlm.progress_text("Unpacking", 5, 0), "Unpacking: 5 bytes")

    def test_two_changing_rows_each_update_and_the_newest_is_last(self):
        tl = Timeline(clock=Clock())
        tl.add("archive", "Data block 1 of 3: lzma", fold=("blocks", 0))
        tl.progress("Unpacking: 10 %")
        tl.add("archive", "Data blocks 1-2 of 3: lzma", fold=("blocks", 0))
        tl.progress("Unpacking: 20 %")
        self.assertEqual([ln[10:] for ln in tl.lines()],
                         ["Data blocks 1-2 of 3: lzma", "Unpacking: 20 %"])

    def test_a_progress_sample_that_says_nothing_new_adds_no_row(self):
        # Seen in the sandbox run: the same 0 % row came back after each window and note.
        tl = Timeline(clock=Clock())
        tl.progress("Setup reading the installer's data: 0 %", "6.4 MB written")
        tl.add("stall", "Quiet for 2 min 01 s: possibly stalled")
        tl.progress("Setup reading the installer's data: 0 %", "6.4 MB written")
        tl.add("note", "You pressed Stop")
        tl.progress("Setup reading the installer's data: 1 %", "7.0 MB written")
        self.assertEqual([ln[10:].split(SEP)[0] for ln in tl.lines()], [
            "Setup reading the installer's data: 0 %", "Quiet for 2 min 01 s: possibly stalled",
            "You pressed Stop", "Setup reading the installer's data: 1 %"])

    def test_end_words_take_the_headline_s_first_sentence(self):
        self.assertEqual(tlm.end_words("Cancelled. Stopped by you. Nothing was deleted.", "x"),
                         ("Cancelled.", "Stopped by you."))
        self.assertEqual(tlm.end_words("Unpacked (Setup was not run).", "Extraction finished: 5"
                                       " files\nmore"),
                         ("Unpacked (Setup was not run).", "Extraction finished: 5 files"))
        self.assertEqual(tlm.end_words("", ""), ("The run ended", ""))

    def test_a_setup_data_warning_keeps_its_file_names(self):
        from gitfurl.detect import Detection, Evidence

        det = Detection(Path("setup.exe"), evidence=[Evidence(
            "Inno Setup", "likely", "x", "setup data format 5.5.0; more",
            lines=["warning: data.inf, game-01.bin not found beside the installer; a silent "
                   "run is likely to stop"])])
        tl = Timeline()
        tlm.detection_entries(tl, det)
        text = tl.text()
        self.assertIn("Detected Inno Setup (likely)" + SEP + "setup data format 5.5.0", text)
        self.assertIn("Warning: data.inf, game-01.bin not found beside the installer" + SEP
                      + "a silent run is likely to stop", text)

    def test_unknown_kind_is_refused(self):
        with self.assertRaises(ValueError):
            Timeline().add("shell", "x")

    def test_durations(self):
        self.assertEqual(tlm.duration_text(42), "42 s")
        self.assertEqual(tlm.duration_text(130), "2 min 10 s")
        self.assertEqual(tlm.duration_text(3 * 3600 + 120), "3 h 02 min")


class CapAndCleanTests(unittest.TestCase):
    def test_text_is_one_line_printable_and_capped(self):
        tl = Timeline(clock=Clock())
        tl.add("window", "Window opened: \"Setup\x1b[31m\nnext\"", "x" * 1000)
        line = tl.lines()[0]
        self.assertNotIn("\x1b", line)
        self.assertNotIn("\n", line)
        self.assertIn('"Setup?[31m next"', line)
        self.assertLessEqual(len(line.split(SEP)[1]), tlm.MAX_TEXT + 3)
        self.assertTrue(line.endswith("..."))

    def test_shown_uses_the_shared_cleaning(self):
        from gitfurl.detect import clean

        title = "Setup\x07" + "y" * 500
        self.assertEqual(tlm.shown(title), clean(title, tlm.SHOWN_MAX))
        self.assertLessEqual(len(tlm.shown(title)), tlm.SHOWN_MAX + 3)

    def test_rows_are_bounded_and_the_plan_stays(self):
        tl = Timeline(clock=Clock(), max_entries=100)
        tl.add("plan", "Route: wine")
        for i in range(500):
            tl.add("note", f"note {i}")
        entries = tl.entries()
        self.assertEqual(len(entries), 100)
        self.assertEqual(entries[0].what, "Route: wine")
        self.assertEqual(entries[-1].what, "note 499")
        lines = tl.lines()
        self.assertIn("401 earlier rows left out", lines[tlm.KEEP_HEAD])

    def test_threads_can_add_at_once(self):
        tl = Timeline()

        def many():
            for i in range(300):
                tl.add("note", f"n{i}")
                tl.progress(f"p{i}")

        threads = [threading.Thread(target=many) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertLessEqual(len(tl.entries()), tlm.MAX_ENTRIES)


class FileTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def test_mode_0600_rows_written_as_they_arrive(self):
        tl = Timeline(clock=Clock())
        tl.add("plan", "Route: native")
        path = self.dir / "p1-20261127-090000.timeline.txt"
        tl.attach(path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        tl.add("start", "Started the unpack")
        self.assertEqual(path.read_text().splitlines(),
                         ["09:00:00  Route: native", "09:00:01  Started the unpack"])
        tl.progress("Unpacking: 10 %")
        tl.progress("Unpacking: 20 %")
        self.assertNotIn("Unpacking", path.read_text())  # still changing: not final yet
        tl.end("Unpacked (Setup was not run).")
        text = path.read_text().splitlines()
        self.assertEqual(text[2][10:], "Unpacking: 20 %")
        self.assertEqual(text[3][10:], "Unpacked (Setup was not run).")
        tl.add("note", "after the end")  # ignored
        self.assertEqual(len(path.read_text().splitlines()), 4)

    def test_an_existing_file_or_a_link_is_never_used(self):
        path = self.dir / "p1-20261127-090000.timeline.txt"
        path.write_text("someone else's\n")
        tl = Timeline()
        tl.add("plan", "x")
        tl.attach(path)
        self.assertIsNone(tl.path)
        self.assertIn("could not be written", tl.problem)
        self.assertEqual(path.read_text(), "someone else's\n")
        target = self.dir / "target"
        target.write_text("")
        link = self.dir / "p1-20261127-090001.timeline.txt"
        link.symlink_to(target)
        tl2 = Timeline()
        tl2.attach(link)
        self.assertIsNone(tl2.path)
        self.assertEqual(target.read_text(), "")

    def test_save_only_after_a_step_that_did_something(self):
        tl = Timeline()
        tl.add("plan", "Route: wine")
        tl.add("approval", "You approved the plan")
        tl.add("note", "The ClamAV scan stopped the run")
        tl.save(self.dir / "logs", "p1")
        self.assertFalse((self.dir / "logs").exists())  # nothing was done: nothing written
        tl.add("prefix", "Creating the prefix with wineboot")
        tl.save(self.dir / "logs", "p1")
        self.assertIsNotNone(tl.path)
        self.assertEqual(tlm.kept(self.dir / "logs", "p1"), [tl.path])
        self.assertEqual(stat.S_IMODE((self.dir / "logs").stat().st_mode), 0o700)

    def test_the_file_is_capped_but_keeps_the_end(self):
        tl = Timeline(max_entries=50)
        tl.attach(self.dir / "p-20261127-090000.timeline.txt")
        for i in range(200):
            tl.add("note", f"n{i}")
        tl.end("Done.")
        lines = tl.path.read_text().splitlines()
        self.assertEqual(len(lines), 52)
        self.assertIn("more rows left out", lines[50])
        self.assertTrue(lines[-1].endswith("Done."))

    def test_on_line_gets_each_final_row(self):
        got = []
        tl = Timeline(clock=Clock(), on_line=got.append)
        tl.progress("a")
        tl.progress("b")
        tl.add("note", "c")
        self.assertEqual([g[10:] for g in got], ["b", "c"])

    def test_kept_latest_and_read_back(self):
        logs = self.dir
        for name in ("p1-20261127-090000.timeline.txt", "p1-20261127-100000.timeline.txt",
                     "p1-20261127-100000-2.timeline.txt", "p1-x-20261127-110000.timeline.txt",
                     "p1-20261127-120000.log", "other.timeline.txt"):
            (logs / name).write_text("09:00:00  row\x1b\n")
        self.assertEqual([p.name for p in tlm.kept(logs, "p1")],
                         ["p1-20261127-090000.timeline.txt", "p1-20261127-100000.timeline.txt",
                          "p1-20261127-100000-2.timeline.txt"])
        self.assertEqual(tlm.latest(logs, "p1").name, "p1-20261127-100000-2.timeline.txt")
        self.assertEqual(tlm.latest(logs, "p1-x").name, "p1-x-20261127-110000.timeline.txt")
        self.assertIsNone(tlm.latest(logs, "p2"))
        self.assertEqual(tlm.read_text(tlm.latest(logs, "p1")), "09:00:00  row?")
        (logs / "p1-20261127-100000.log").write_text("log")
        self.assertEqual(tlm.log_for(logs / "p1-20261127-100000.timeline.txt").name,
                         "p1-20261127-100000.log")
        self.assertIsNone(tlm.log_for(logs / "p1-20261127-090000.timeline.txt"))
        big = logs / "p3-20261127-090000.timeline.txt"
        big.write_text("x" * 100)
        self.assertIn("was not read", tlm.read_text(big, limit=10))


class PlanEntryTests(NativeBase):
    def test_route_reasons_prefix_and_skipped(self):
        from gitfurl.detect import detect

        plan = self.plan()
        tl = Timeline()
        tlm.plan_entries(tl, detect(self.setup), plan.choice, self.prefix, False, True,
                         plan.extract.target)
        tlm.approval_entry(tl, "yes")
        text = tl.text()
        self.assertIn("Detected Inno Setup (likely)", text)
        self.assertIn("Route: native (gitfurl unpacks the archives itself; gitfurl chose it)",
                      text)
        self.assertIn("gitfurl can unpack them itself", text)
        self.assertIn("the archives share the installer's name", text)
        self.assertIn("Prefix p1: new", text)
        self.assertNotIn("Reason for the route" + SEP + "Setup is not run", text)
        self.assertIn("Setup is not run", text)
        self.assertIn("C:\\Games\\p1", text)
        self.assertIn("Approved with --yes", text)


class WineRunTests(RunEventHelpers, unittest.TestCase):
    def test_rows_from_a_mocked_wine_run(self):
        self.fake_prefix_process(1 << 20)
        calls = []

        def lister(pids, programs, env, pid_of=None):
            calls.append(1)
            main = [("0x1", "Setup - Game")]
            return main if len(calls) == 1 else main + [("0x2", "Error\x1b")]

        tl = Timeline()
        tl.add("approval", "You approved the plan")
        res = self.run_it(self.sleeper(1.0), timeline=tl, stall_after_s=250,
                          clock=FakeClock(100), on_note=lambda _n: None, window_list=lister)
        tl.end("Installer exited with status 0")
        text = tl.text()
        self.assertIn("Started Setup (setup.exe) under Wine, with its own windows", text)
        self.assertIn("Setup reading the installer's data: 50 %", text)
        self.assertIn('Window opened: "Setup - Game"', text)
        self.assertIn('Window opened: "Error?"', text)  # cleaned
        self.assertIn("Quiet for", text)
        self.assertIn("possibly stalled", text)
        self.assertIn("advice only, nothing was stopped", text)
        self.assertIn("Setup exited with status 0", text)
        # saved next to the log, same stamp, 0600
        path = Path(str(res.log_path)[:-4] + tlm.SUFFIX)
        self.assertEqual(tl.path, path)
        self.assertEqual(tl.log_path, res.log_path)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("Window opened", path.read_text())
        self.assertNotIn("hunter2", path.read_text())

    def test_crash_and_stop_rows(self):
        from gitfurl.runner import RunControl

        self.fake_prefix_process(0)
        sleeper = self.sleeper(0.4)

        def run(argv, **kw):
            d = self.proc / "101"
            d.mkdir(exist_ok=True)
            (d / "environ").write_bytes(f"WINEPREFIX={self.plan.prefix}\0".encode())
            (d / "stat").write_text("101 (winedbg.exe) " + " ".join(["S"] + ["0"] * 19))
            return sleeper(argv, **kw)

        tl = Timeline()
        control = RunControl()
        with mock.patch("os.kill"), mock.patch("os.killpg"):
            self.run_it(run, timeline=tl, on_note=lambda _n: None, control=control,
                        window_titles=lambda *a: ["Program Error"])
        text = tl.text()
        self.assertIn("Wine's crash handler (winedbg) started in the prefix", text)
        self.assertIn('open: "Program Error"', text)
        self.assertIn("A Windows program crashed during the run", text)

    def test_no_timeline_changes_nothing(self):
        self.fake_prefix_process(0)
        res = self.run_it(self.sleeper(0.2), on_note=lambda _n: None)
        names = sorted(p.name for p in Path(res.log_path).parent.iterdir())
        self.assertEqual(names, [Path(res.log_path).name])

    def test_prefix_creation_rows(self):
        import shutil

        from gitfurl.runner import RunOptions, run_with_prefix

        shutil.rmtree(self.plan.prefix)
        tl = Timeline()

        def fake(argv, **kw):
            if "wineboot" in argv:
                return subprocess.CompletedProcess(argv, 0)
            return subprocess.CompletedProcess(argv, 0)

        run_with_prefix(self.plan, "/x/wine", True, self.env, fake,
                        options=RunOptions(timeline=tl))
        text = tl.text()
        self.assertIn("Creating the prefix with wineboot", text)
        self.assertIn("Prefix created" + SEP + "wineboot exited with status 0", text)
        self.assertLess(text.index("Prefix created"), text.index("Started Setup"))


class NativeRunTests(NativeBase):
    def test_rows_from_a_native_unpack(self):
        from gitfurl.route import run_native

        plan = self.plan()
        self.make_prefix()
        tl = Timeline()
        with no_process():
            res = run_native(plan, timeline=tl, env=self.env, progress_interval=0)
        tl.end("Unpacked (Setup was not run).")
        self.assertEqual(res.status, 0)
        text = tl.text()
        self.assertIn("Destination: C:\\Games\\p1", text)
        self.assertIn("Started the unpack with gitfurl's own reader" + SEP + "2 archives, 5 files",
                      text)
        self.assertIn("Opened NAME-01.bin: 2 files in 1 data block", text)
        self.assertIn("Opened NAME-02.bin: 3 files in 2 data blocks", text)
        self.assertIn("Data block 1 of 1 in NAME-01.bin: lzma", text)
        self.assertIn("Data block 1 of 2 in NAME-02.bin: rep+exe+delta+lzma", text)
        self.assertIn("Data block 2 of 2 in NAME-02.bin: storing", text)
        self.assertIn("5 files written", text)
        self.assertIn("each one CRC-32 checked" + SEP + "every CRC-32 matched", text)
        self.assertIn("1 redistributable installer unpacked but not run", text)
        self.assertIn("Unpacking: 100 %", text)
        self.assertNotIn("1mb", text)  # never the archive's own method text
        path = Path(str(res.log_path)[:-4] + tlm.SUFFIX)
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertIn("Unpacked (Setup was not run).", path.read_text())

    def test_a_crc_mismatch_is_listed_as_removed(self):
        from gitfurl.route import run_native

        plan = self.plan()
        self.make_prefix()
        tl = Timeline()
        with no_process(), mock.patch("gitfurl.extract.zlib",
                                          mock.Mock(crc32=lambda *_a: 1)):
            run_native(plan, timeline=tl, env=self.env)
        text = tl.text()
        self.assertIn("files removed again" + SEP + "their CRC-32 did not match", text)

    def test_perform_native_adds_the_prefix_rows(self):
        from gitfurl.route import NativeControl, perform_native

        plan = self.plan(create=True, wine="/x/wine")
        tl = Timeline()

        def create(p, memory_limit, env=None, should_stop=None):
            self.make_prefix()
            return 0

        with no_process():
            out = perform_native(plan, NativeControl(), memory_limit=False, create=create,
                                 env=self.env, timeline=tl)
        self.assertEqual(out.status, 0)
        text = tl.text()
        self.assertIn("Creating the prefix with wineboot", text)
        self.assertIn("Prefix created", text)
        self.assertLess(text.index("Prefix created"), text.index("Started the unpack"))


class CliLogTests(NativeBase):
    def install(self, *extra):
        self.plan()
        self.make_prefix()
        with no_process():
            return run_cli("install", str(self.setup), "--prefix", "p1", "--yes", *extra)

    def test_install_saves_a_timeline_and_log_prints_it(self):
        code, out, _ = self.install()
        self.assertEqual(code, 0, out)
        self.assertNotIn("[timeline]", out)  # default output unchanged
        self.assertIn("timeline: ", out)
        code, out, _ = run_cli("log", "p1")
        self.assertEqual(code, 0)
        self.assertIn("Route: native", out)
        self.assertIn("Approved with --yes", out)
        self.assertIn("Opened NAME-01.bin", out)
        self.assertTrue(out.rstrip().endswith("Unpacked (Setup was not run)."), out[-300:])
        code, out, _ = run_cli("log", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(out.count(".timeline.txt"), 1)

    def test_timeline_flag_prints_rows_as_they_come(self):
        code, out, _ = self.install("--timeline")
        self.assertEqual(code, 0, out)
        rows = [ln for ln in out.splitlines() if ln.startswith("[timeline] ")]
        self.assertTrue(any("Route: native" in r for r in rows), rows)
        self.assertTrue(rows[-1].endswith("Unpacked (Setup was not run)."), rows[-3:])

    def test_log_without_a_timeline_or_with_a_bad_name(self):
        code, out, _ = run_cli("log", "p1")
        self.assertEqual(code, 1)
        self.assertIn("no timeline for p1", out)
        code, _, err = run_cli("log", "../x")
        self.assertEqual(code, 1)
        self.assertIn("error", err)
        code, _, err = run_cli("log")
        self.assertEqual(code, 1)
        code, out, _ = run_cli("log", "--all")
        self.assertEqual(code, 0)
        self.assertIn("no timelines kept", out)

    def test_a_declined_or_dry_run_writes_no_timeline(self):
        self.plan()
        self.make_prefix()
        with no_process():
            run_cli("install", str(self.setup), "--prefix", "p1", "--dry-run")
        logs = self.state / "gitfurl" / "logs"
        self.assertEqual(tlm.kept(logs), [])
        self.assertFalse(os.path.exists(logs))


if __name__ == "__main__":
    unittest.main()
