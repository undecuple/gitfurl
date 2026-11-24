"""The native route's shared pieces for the window: the stop checks, one approved run on a
worker thread (perform_native), progress numbers and Stop. Synthetic archives and fakes only;
no Wine, no installer."""

import subprocess
import threading
import unittest
from unittest import mock

try:
    from test_route import Base, chain_blocks, lzma_blocks
except ImportError:  # python -m unittest tests.test_native_run
    from tests.test_route import Base, chain_blocks, lzma_blocks

from gitfurl.route import (
    NATIVE_WINEBOOT_ENV,
    NativeControl,
    NativeInstallPlan,
    NativeStop,
    native_stops,
    perform_native,
    run_native,
)


class NativeBase(Base):
    def plan(self, create=False, wine=None):
        self.arc("NAME-01.bin", lzma_blocks())
        self.arc("NAME-02.bin", chain_blocks())
        choice = self.choose()
        self.assertEqual(choice.route, "native", choice.line())
        return NativeInstallPlan(self.setup, "p1", self.prefix, choice, create, wine)

    def meminfo(self, avail_kb=64 << 20, total_kb=64 << 20):
        path = self.base / f"meminfo-{avail_kb}-{total_kb}"
        path.write_text(f"MemTotal: {total_kb} kB\nMemAvailable: {avail_kb} kB\n")
        return str(path)


class NativeStopsTests(NativeBase):
    def stops(self, plan, **kw):
        args = {"missing": not self.prefix.is_dir(), "create_ok": False, "wine": None,
                "scope_problem": None, "memory_check": True, "meminfo": self.meminfo()}
        args.update(kw)
        return native_stops(plan.extract, **args)

    def test_nothing_stops_a_ready_prefix(self):
        self.make_prefix()
        self.assertEqual(self.stops(self.plan()), [])

    def test_each_stop_in_order(self):
        plan = self.plan()
        got = self.stops(plan, missing=True, create_ok=False, scope_problem="no scope")
        self.assertEqual([s.kind for s in got], ["no_prefix", "scope"])
        self.assertEqual(got[0].detail, str(self.prefix))
        got = self.stops(plan, missing=True, create_ok=True, wine=None)
        self.assertEqual([s.kind for s in got], ["no_wine"])
        got = self.stops(plan, missing=True, create_ok=True, wine="/x/wine")
        self.assertEqual(got, [])
        self.assertTrue(NativeStop("scope").memory_related)
        self.assertTrue(NativeStop("memory").memory_related)
        self.assertFalse(NativeStop("space").memory_related)

    def test_space_and_incomplete_prefix(self):
        plan = self.plan()
        self.make_prefix()
        (self.prefix / "gitfurl-incomplete").write_text("")
        kinds = [s.kind for s in self.stops(plan)]
        self.assertEqual(kinds, ["space"])
        with mock.patch("gitfurl.extract.ExtractPlan.space_problem_before_prefix",
                        return_value="not enough space"):
            got = self.stops(plan, missing=True, create_ok=True, wine="/x/wine")
        self.assertEqual(got, [NativeStop("space", "not enough space")])

    def test_memory_check_and_its_switch(self):
        self.make_prefix()
        plan = self.plan()
        small = self.meminfo(avail_kb=1, total_kb=1 << 20)
        self.assertEqual([s.kind for s in self.stops(plan, meminfo=small)], ["memory"])
        self.assertEqual(self.stops(plan, meminfo=small, memory_check=False), [])


class PerformNativeTests(NativeBase):
    def test_unpacks_with_progress_numbers(self):
        self.make_prefix()
        plan = self.plan()
        seen = []
        out = perform_native(plan, NativeControl(), memory_limit=False, progress_interval=0,
                             env=self.env, on_progress=lambda *a: seen.append(a))
        self.assertEqual(out.status, 0, out.message)
        self.assertIsNone(out.message)
        self.assertEqual(out.result.status, 0)
        self.assertTrue(seen)
        done, total, elapsed = seen[-1]
        self.assertEqual(done, total)
        self.assertGreaterEqual(elapsed, 0)
        self.assertTrue((plan.extract.dest / "bin" / "Game.exe").is_file())
        self.assertEqual(out.result.log_path.stat().st_mode & 0o777, 0o600)

    def test_stop_before_the_unpack_writes_nothing(self):
        # A Stop after the scan or wineboot must not leave an empty destination behind,
        # which would make every later run on this prefix refuse.
        self.make_prefix()
        plan = self.plan()
        control = NativeControl()
        control.request_stop()
        out = perform_native(plan, control, memory_limit=False, env=self.env)
        self.assertEqual(out.status, 130)
        self.assertIsNone(out.result)
        self.assertIn("Stopped before the unpack", out.message)
        self.assertFalse(plan.extract.dest.exists())

    def test_stop_just_before_the_destination_is_made(self):
        # The Stop lands after perform_native's own check, inside run_native.
        self.make_prefix()
        plan = self.plan()
        control = NativeControl()

        def late(p, cancel=None, **kw):
            control.request_stop()
            return run_native(p, cancel=cancel, **kw)

        out = perform_native(plan, control, memory_limit=False, env=self.env, run=late)
        self.assertEqual(out.status, 130)
        self.assertIsNone(out.result)
        self.assertFalse(plan.extract.dest.exists())

    def test_creates_the_prefix_first_with_a_stop_hook(self):
        plan = self.plan(create=True, wine="/x/wine")
        calls = []

        def create(p, memory_limit, env, should_stop=None):
            calls.append((p.prefix_name, memory_limit, should_stop()))
            (self.prefix / "drive_c").mkdir(parents=True)
            return 0

        out = perform_native(plan, NativeControl(), memory_limit=True, env=self.env,
                             create=create)
        self.assertEqual(calls, [("p1", True, False)])
        self.assertEqual(out.status, 0, out.message)
        self.assertTrue(out.prefix_created)

    def test_prefix_creation_problems_become_messages(self):
        from gitfurl.memlimit import MemoryLimitError
        from gitfurl.prefixes import PrefixStopped

        cases = [(MemoryLimitError("no scope"), 2, "no scope. Nothing was unpacked."),
                 (PrefixStopped("creating the prefix was stopped (x)."), 130,
                  "was stopped (x). Nothing was unpacked."),
                 (FileExistsError("exists"), 1, "exists. Nothing was unpacked."),
                 (subprocess.TimeoutExpired(["wine"], 1), 1, "did not finish in time"),
                 (RuntimeError("timed out."), 1, "timed out. Nothing was unpacked.")]
        for exc, status, text in cases:
            plan = self.plan(create=True, wine="/x/wine")

            def create(*a, _exc=exc, **kw):
                raise _exc

            out = perform_native(plan, NativeControl(), memory_limit=False, env=self.env,
                                 create=create)
            self.assertEqual(out.status, status, exc)
            self.assertIn(text, out.message)
            self.assertIsNone(out.result)
            self.assertFalse(self.prefix.exists())
        out = perform_native(plan, NativeControl(), memory_limit=False, env=self.env,
                             create=lambda *a, **kw: 3)
        self.assertEqual((out.status, out.message[:40]),
                         (1, "creating the prefix failed (wine exited "))

    def test_refusal_and_log_problems_become_messages(self):
        from gitfurl.extract import ExtractRefused

        self.make_prefix()
        plan = self.plan()

        def refused(*a, **kw):
            raise ExtractRefused("a name clashes")

        out = perform_native(plan, NativeControl(), memory_limit=False, env=self.env,
                             run=refused)
        self.assertEqual(out.status, 1)
        self.assertTrue(out.message.startswith("a name clashes. "))

        def no_log(*a, **kw):
            raise PermissionError(13, "Permission denied")

        out = perform_native(plan, NativeControl(), memory_limit=False, env=self.env,
                             run=no_log)
        self.assertIn("the log could not be written (Permission denied)", out.message)
        self.assertIn("Nothing was unpacked.", out.message)

        def after_files(p, **kw):
            p.extract.dest.mkdir(parents=True)
            raise PermissionError(13, "Permission denied")

        out = perform_native(plan, NativeControl(), memory_limit=False, env=self.env,
                             run=after_files)
        self.assertIn("after the unpack started", out.message)
        self.assertNotIn("Nothing was unpacked", out.message)

    def test_scan_blocks_stops_or_passes(self):
        from gitfurl.scan import CLEAN, HIT, ScanBlocked, ScanResult

        self.make_prefix()
        plan = self.plan()
        hit = ScanResult(HIT, hits=[("x", "Eicar")])
        with mock.patch("gitfurl.scan.run_scan", return_value=hit):
            with self.assertRaises(ScanBlocked):
                perform_native(plan, NativeControl(), memory_limit=False, env=self.env,
                               scan_plan=object())
            notes = []
            out = perform_native(plan, NativeControl(), memory_limit=False, env=self.env,
                                 scan_plan=object(), allow_scan_hit=True, on_note=notes.append)
        self.assertEqual(out.status, 0)
        self.assertEqual(len(notes), 1)
        control = NativeControl()

        def stopped(scan_plan, should_stop=None):
            control.request_stop()
            self.assertTrue(should_stop())
            return ScanResult(CLEAN)

        with mock.patch("gitfurl.scan.run_scan", side_effect=stopped):
            out = perform_native(plan, control, memory_limit=False, env=self.env,
                                 scan_plan=object())
        self.assertEqual(out.status, 130)
        self.assertIn("Stopped during the ClamAV scan", out.message)


class RunNativeProgressTests(NativeBase):
    def test_on_progress_follows_the_same_throttle(self):
        self.make_prefix()
        plan = self.plan()
        lines, nums = [], []
        res = run_native(plan, on_line=lines.append, on_progress=lambda *a: nums.append(a),
                         progress_interval=0, env=self.env)
        self.assertEqual(res.status, 0)
        self.assertEqual(len(lines), len(nums))
        self.assertTrue(nums)


class CreatePrefixStopTests(Base):
    def test_stopped_wineboot_keeps_the_folder_marked_incomplete(self):
        from gitfurl.prefixes import INCOMPLETE_MARKER, PrefixStopped, create_prefix

        def stopped(argv, env, timeout, wine, should_stop=None):
            self.assertTrue(should_stop())
            exc = PrefixStopped("stopped while the prefix was being created")
            exc.notes = ["the processes it started were stopped"]
            raise exc

        with mock.patch("gitfurl.prefixes.run_in_group", side_effect=stopped):
            with self.assertRaises(PrefixStopped) as ctx:
                create_prefix("p1", "/x/wine", self.env, extra_env=dict(NATIVE_WINEBOOT_ENV),
                              should_stop=lambda: True)
        self.assertIn("was stopped", str(ctx.exception))
        self.assertIn("the processes it started were stopped", str(ctx.exception))
        self.assertIn("marked incomplete", str(ctx.exception))
        self.assertTrue((self.prefix / INCOMPLETE_MARKER).exists())


class NativeControlTests(unittest.TestCase):
    def test_request_stop_sets_the_event(self):
        c = NativeControl()
        self.assertFalse(c.cancelled)
        self.assertIsInstance(c.event, threading.Event)
        self.assertEqual(c.request_stop(), [])
        self.assertTrue(c.cancelled and c.event.is_set())


if __name__ == "__main__":
    unittest.main()
