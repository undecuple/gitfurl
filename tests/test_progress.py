"""Stopping the progress monitor and the memory limit's adjuster: no callback may start after
stop()/finish() has returned, and sampling never runs twice at once. Mocks only."""

import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from gitfurl import progress
from gitfurl.memlimit import Availability, MemoryScope, compute_budget

GIB = 1 << 30


def short_join(thread_owner):
    """Make the owner's thread join() time out after 0.05 s instead of seconds, so a stop that
    relies on the join alone (rather than the lock) returns while the callback still runs."""
    th = thread_owner._thread
    real = th.join
    th.join = lambda timeout=None: real(0.05)


class MonitorStopTests(unittest.TestCase):
    def setUp(self):
        self._t = tempfile.TemporaryDirectory()
        self.addCleanup(self._t.cleanup)
        self.dir = Path(self._t.name)
        (self.dir / "setup.exe").write_bytes(b"MZ")
        self.prefix = self.dir / "pfx"
        self.prefix.mkdir()
        patcher = mock.patch.object(progress, "payload_files", return_value=[])
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_stop_waits_for_a_running_callback_and_none_starts_after(self):
        entered, release = threading.Event(), threading.Event()
        calls = []

        def on_sample(sample):
            calls.append(sample)
            entered.set()
            release.wait(10)

        mon = progress.ProgressMonitor(self.dir / "setup.exe", self.prefix, on_sample,
                                       interval=0.001)
        mon.start()
        self.assertTrue(entered.wait(10))
        short_join(mon)
        done = threading.Event()
        box = {}

        def stopper():
            box["final"] = mon.stop()
            done.set()

        threading.Thread(target=stopper, daemon=True).start()
        self.assertFalse(done.wait(0.3))  # held by the running callback
        release.set()
        self.assertTrue(done.wait(10))
        count = len(calls)
        time.sleep(0.1)
        self.assertEqual(len(calls), count)
        self.assertIsNotNone(box["final"])

    def test_sample_never_runs_twice_at_once(self):
        active, peak = [0], [0]
        lock = threading.Lock()
        mon = progress.ProgressMonitor(self.dir / "setup.exe", self.prefix, lambda s: None,
                                       interval=0.0005)
        real = mon.sample

        inside = threading.Event()

        def counted():
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            inside.set()
            try:
                time.sleep(0.2)
                return real()
            finally:
                with lock:
                    active[0] -= 1

        mon.sample = counted
        mon.start()
        self.assertTrue(inside.wait(10))  # the thread is sampling now
        short_join(mon)
        mon.stop()
        self.assertEqual(peak[0], 1)


    def test_ctrl_c_while_stop_waits_is_held_until_the_callback_has_ended(self):
        # Review finding: a second Ctrl-C while stop() waited for the lock left stop() before
        # the join, with the thread still inside on_sample.
        entered, release = threading.Event(), threading.Event()
        ended = threading.Event()

        def on_sample(sample):
            entered.set()
            release.wait(10)
            ended.set()

        mon = progress.ProgressMonitor(self.dir / "setup.exe", self.prefix, on_sample,
                                       interval=0.001)
        mon.start()
        self.assertTrue(entered.wait(10))
        real = mon._lock
        hits = []

        class InterruptOnce:
            def acquire(self, timeout=-1):
                if not hits:
                    hits.append(1)
                    threading.Timer(0.2, release.set).start()
                    raise KeyboardInterrupt
                return real.acquire(timeout=timeout)

            def release(self):
                real.release()

            def __enter__(self):
                real.acquire()

            def __exit__(self, *a):
                real.release()

        mon._lock = InterruptOnce()
        with self.assertRaises(KeyboardInterrupt):
            mon.stop()
        self.assertEqual(hits, [1])
        self.assertTrue(ended.is_set())  # the callback had finished before stop() raised
        self.assertFalse(mon._thread.is_alive())

    def test_stop_gives_up_on_a_stuck_callback_after_its_wait(self):
        entered, release = threading.Event(), threading.Event()
        self.addCleanup(release.set)

        def on_sample(sample):
            entered.set()
            release.wait(10)

        mon = progress.ProgressMonitor(self.dir / "setup.exe", self.prefix, on_sample,
                                       interval=0.001)
        mon.start()
        self.assertTrue(entered.wait(10))
        t0 = time.monotonic()
        self.assertIsNotNone(mon.stop(wait_s=0.2))
        self.assertLess(time.monotonic() - t0, 5)


class ScopeFinishTests(unittest.TestCase):
    def test_finish_waits_for_a_running_tick_and_none_starts_after(self):
        avail = Availability(True, "", "/x/systemd-run", "/x/systemctl", 262, "/user.slice/x")
        scope = MemoryScope(avail, compute_budget(16 * GIB, 10 * GIB), "gitfurl-p1-t-1.scope",
                            env={}, interval=0.001, settle_s=0)
        entered, release = threading.Event(), threading.Event()
        ticks = []

        def tick():
            ticks.append(1)
            entered.set()
            release.wait(10)

        scope.tick = tick
        scope.start()
        self.assertTrue(entered.wait(10))
        short_join(scope)
        done = threading.Event()

        def finisher():
            with mock.patch.object(scope, "_find_cgroup", return_value=None), \
                    mock.patch.object(scope, "_show", return_value={}), \
                    mock.patch.object(scope, "_systemctl", return_value=None):
                scope.finish()
            done.set()

        threading.Thread(target=finisher, daemon=True).start()
        self.assertFalse(done.wait(0.3))
        release.set()
        self.assertTrue(done.wait(10))
        count = len(ticks)
        time.sleep(0.1)
        self.assertEqual(len(ticks), count)


if __name__ == "__main__":
    unittest.main()
