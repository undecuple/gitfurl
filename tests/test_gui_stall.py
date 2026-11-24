"""The window's stall advice: which notes stay on screen and the stall time it uses. No
display needed."""

import unittest

try:
    from gitfurl import gui
except ImportError as exc:  # no tkinter
    raise unittest.SkipTest(str(exc)) from exc


class StallNoteTests(unittest.TestCase):
    def test_crash_and_stall_notes_stay(self):
        self.assertTrue(gui.keeps_note("possibly stalled: no payload read ..."))
        self.assertTrue(gui.keeps_note("crash: Wine's crash handler ..."))
        self.assertFalse(gui.keeps_note("memory limit raised to 6 GiB"))
        self.assertFalse(gui.keeps_note("scan: clean"))

    def test_window_stall_time_matches_its_plan_line(self):
        self.assertEqual(gui.WINDOW_STALL_S, 120.0)
        self.assertIn("2 minutes", gui.STALL_PLAN_LINE)
        self.assertIn("nothing is stopped", gui.STALL_PLAN_LINE)


if __name__ == "__main__":
    unittest.main()
