"""The window's native route: the plain functions behind the switch, the plan text, the
checklist, the progress line and the end text. Synthetic archives only; no display, no Wine."""

import unittest
from pathlib import Path
from unittest import mock

try:
    from test_native_run import NativeBase
except ImportError:  # python -m unittest tests.test_gui_native
    from tests.test_native_run import NativeBase

try:
    from gitfurl import gui
except ImportError as exc:  # no tkinter
    raise unittest.SkipTest(str(exc)) from exc

from gitfurl.route import NativeOutcome, NativeResult, NativeStop


class WindowWordsTests(unittest.TestCase):
    def test_command_line_options_become_the_windows_switches(self):
        text = gui.window_words("use --route wine, or add --no-memory-limit; "
                                "'gitfurl cli prefix create p1'")
        self.assertNotIn("--", text)
        self.assertIn(gui.SETUP_INSTEAD_TITLE, text)
        self.assertIn(gui.MEMORY_TITLE, text)
        text = gui.window_words("Create it first with 'gitfurl cli prefix create p1'")
        self.assertEqual(text, "Create it first (Create now) for 'p1'")
        text = gui.window_words("  If the program needs one, run it with: gitfurl cli install "
                                "<file> --prefix p1")
        self.assertNotIn("gitfurl cli", text)
        self.assertIn("choose that file as the installer here, with the prefix p1", text)
        text = gui.window_words("to unpack them anyway, use --route native")
        self.assertIn("the window does not offer this", text)

    def test_setup_only_options(self):
        self.assertEqual(gui.window_setup_options(False, False, "wine", False), [])
        got = gui.window_setup_options(True, True, "proton", True)
        self.assertEqual(len(got), 4)
        self.assertIn("the Proton runtime", got)

    def test_stop_texts_name_the_switches(self):
        self.assertIn("Create the prefix if it does not exist",
                      gui.native_stop_text(NativeStop("no_prefix", "/p")))
        self.assertIn("Wine is not on PATH", gui.native_stop_text(NativeStop("no_wine")))
        for kind in ("scope", "memory"):
            self.assertIn(gui.MEMORY_TITLE, gui.native_stop_text(NativeStop(kind, "x")))
        self.assertEqual(gui.native_stop_text(NativeStop("space", "full")), "full.")

    def test_footer_state(self):
        self.assertEqual(gui.run_state_text("native", False), "Unpacking")
        self.assertEqual(gui.run_state_text("native", True), "Stopping...")
        self.assertEqual(gui.run_state_text("install", False), "Installer running")


class ChecklistTests(unittest.TestCase):
    def rows(self, **kw):
        args = dict(installer_error=None, prefix_error=None, prefix_exists=True,
                    will_create=True, runtime="wine", runtime_found=False)
        args.update(kw)
        return gui.plan_checklist(**args)

    def test_wine_not_needed_for_an_unpack_into_an_existing_prefix(self):
        rows = self.rows(runtime_needed=False)
        self.assertEqual(gui.run_reason(rows, False), "")
        self.assertIn("not needed", dict((k, t) for k, _ok, t in rows)["runtime"])
        self.assertTrue(gui.run_reason(self.rows(), False))

    def test_root_is_refused_on_every_route(self):
        rows = self.rows(runtime_found=True, as_root=True)
        self.assertEqual(rows[0], ("root", False, gui.ROOT_MESSAGE))
        self.assertIn(gui.ROOT_MESSAGE, gui.run_reason(rows, False))
        self.assertIn(gui.ROOT_MESSAGE, gui.create_reason(None, False, False, as_root=True))
        self.assertEqual(gui.create_reason(None, False, False), "")


class NativePlanTests(NativeBase):
    def native(self):
        self.arc("NAME-01.bin", self.blocks()[0])
        self.arc("NAME-02.bin", self.blocks()[1])
        det, choice, err = gui.window_route(self.setup, "p1", env=self.env)
        self.assertIsNone(err)
        self.assertEqual(choice.route, "native")
        return det, choice

    def blocks(self):
        try:
            from test_route import chain_blocks, lzma_blocks
        except ImportError:
            from tests.test_route import chain_blocks, lzma_blocks
        return lzma_blocks(), chain_blocks()

    def test_plan_for_a_missing_prefix_with_create(self):
        _det, choice = self.native()
        plan, stops, mem = gui.native_plan_for(self.setup, choice, create_ok=True,
                                               memory_on=False, wine="/x/wine",
                                               meminfo=self.meminfo())
        self.assertTrue(plan.create_prefix)
        self.assertEqual(plan.wine, "/x/wine")
        self.assertEqual(stops, [])
        self.assertTrue(mem)  # the memory limit's lines for wineboot
        self.assertTrue(mem[0].startswith("for the prefix creation: "))

    def test_stops_without_create_or_wine(self):
        _det, choice = self.native()
        _plan, stops, _mem = gui.native_plan_for(self.setup, choice, create_ok=False,
                                                 memory_on=False, meminfo=self.meminfo())
        self.assertEqual([s.kind for s in stops], ["no_prefix"])
        _plan, stops, _mem = gui.native_plan_for(self.setup, choice, create_ok=True,
                                                 memory_on=False, wine=None,
                                                 meminfo=self.meminfo())
        self.assertEqual([s.kind for s in stops], ["no_wine"])

    def test_wine_is_not_named_for_an_existing_prefix(self):
        self.make_prefix()
        _det, choice = self.native()
        plan, stops, mem = gui.native_plan_for(self.setup, choice, create_ok=True,
                                               memory_on=True, wine="/x/wine",
                                               meminfo=self.meminfo())
        self.assertFalse(plan.create_prefix)
        self.assertIsNone(plan.wine)
        self.assertEqual((stops, mem), ([], []))

    def test_approval_text(self):
        self.make_prefix()
        det, choice = self.native()
        plan, _s, _m = gui.native_plan_for(self.setup, choice, create_ok=True,
                                           memory_on=True, meminfo=self.meminfo())
        text = gui.native_plan_text(plan, det, preflight="free: plenty")
        self.assertIn("route: native (chosen by gitfurl)", text)
        self.assertIn("free: plenty", text)
        self.assertIn(gui.NATIVE_STOP_LINE, text)
        self.assertTrue(text.endswith("Only unpack installers you are authorised to use."))
        self.assertNotIn("--route", text)
        self.assertNotIn("--no-memory-limit", text)


class ProgressAndEndTests(unittest.TestCase):
    def test_sample(self):
        s = gui.NativeSample(50, 200, 2.0)
        self.assertEqual(s.percent, 25.0)
        self.assertTrue(s.line())
        self.assertEqual(gui.NativeSample(0, 0, 0).percent, 100.0)

    def result(self, status):
        return NativeResult(status, mock.Mock(), Path("/l/native.log"), None,
                            ["files: 3 written", "program listing: --route wine"])

    def test_end_texts(self):
        text, colour = gui.native_end_text(NativeOutcome(0, None, self.result(0)),
                                           "Cleanup: deleted 2 file(s)", ["scan: clean"])
        self.assertEqual(colour, "ok")
        self.assertTrue(text.startswith("Unpacked (Setup was not run)."))
        for part in ("files: 3 written", "Cleanup: deleted 2", "scan: clean",
                     "Local log: /l/native.log"):
            self.assertIn(part, text)
        self.assertNotIn("--route", text)
        text, colour = gui.native_end_text(NativeOutcome(130, None, self.result(130)),
                                           "skipped", [])
        self.assertEqual(colour, "warn")
        self.assertIn("Files already written are kept", text)
        self.assertIn("Cleanup skipped.", text)
        text, colour = gui.native_end_text(NativeOutcome(1, None, self.result(1)), None, [])
        self.assertEqual(colour, "warn")
        self.assertNotIn("Cleanup", text)
        text, colour = gui.native_end_text(NativeOutcome(130, "Stopped before the unpack."),
                                           None, [])
        self.assertEqual((text, colour), ("Stopped. Stopped before the unpack.", "warn"))
        text, colour = gui.native_end_text(NativeOutcome(1, "no room."), None, [])
        self.assertEqual((text, colour), ("Not unpacked. no room.", "error"))

    def test_cleanup_only_after_a_complete_unstopped_unpack(self):
        ok = NativeOutcome(0, None, self.result(0))
        self.assertTrue(gui.native_cleanup_allowed(ok, False))
        self.assertFalse(gui.native_cleanup_allowed(ok, True))  # Stop after the unpack
        for out in (NativeOutcome(130, None, self.result(130)),
                    NativeOutcome(1, None, self.result(1)), NativeOutcome(1, "x")):
            self.assertFalse(gui.native_cleanup_allowed(out, False))
        text, _c = gui.native_end_text(ok, "Cleanup skipped: you pressed Stop.", [])
        self.assertIn("Cleanup skipped: you pressed Stop.", text)

    def test_many_notes_are_shortened(self):
        text, _c = gui.native_end_text(NativeOutcome(1, "x"), None,
                                       [f"n{i}" for i in range(8)])
        self.assertEqual(text.splitlines()[1:], ["n0", "...", "n4", "n5", "n6", "n7"])


if __name__ == "__main__":
    unittest.main()
