"""The window's view settings file, the result headline, the done state after an unpack, and
the Help texts. Pure functions and a temporary folder only; no window is opened."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from gitfurl import viewprefs
from gitfurl.gui import (DONE_REASON, about_text, done_rows, key_help_text, run_reason,
                         split_result)


class ViewPrefsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "c" / "gitfurl" / "view.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_path_follows_xdg_config_home(self):
        self.assertEqual(viewprefs.prefs_path({"XDG_CONFIG_HOME": "/x", "HOME": "/h"}),
                         Path("/x/gitfurl/view.json"))
        self.assertEqual(viewprefs.prefs_path({"HOME": "/h"}), Path("/h/.config/gitfurl/view.json"))

    def test_missing_or_damaged_file_gives_defaults(self):
        self.assertEqual(viewprefs.load(self.path), viewprefs.ViewPrefs())
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json")
        self.assertEqual(viewprefs.load(self.path), viewprefs.ViewPrefs())
        self.path.write_text("x" * (viewprefs.MAX_BYTES + 1))
        self.assertEqual(viewprefs.load(self.path), viewprefs.ViewPrefs())

    def test_unknown_or_bad_values_are_dropped(self):
        p = viewprefs.clean({"text_size": "huge", "reduced_motion": "yes", "remember_size": True,
                             "size": "1x1; rm", "extra": 1})
        self.assertEqual(p, viewprefs.ViewPrefs(remember_size=True))
        self.assertEqual(viewprefs.clean({"size": "1200x800"}).size, "1200x800")
        self.assertEqual(viewprefs.clean({"size": "1200x800+10+10"}).size, "")
        self.assertEqual(viewprefs.clean([1, 2]), viewprefs.ViewPrefs())

    def test_save_and_load_round_trip_with_mode_0600(self):
        prefs = viewprefs.ViewPrefs("larger", True, True, "1200x800")
        self.assertIsNone(viewprefs.save(prefs, self.path))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)
        self.assertEqual(viewprefs.load(self.path), prefs)
        self.assertEqual(viewprefs.load(self.path).scale, 1.2)
        self.assertFalse(self.path.with_name("view.json.tmp").exists())

    def test_size_is_not_kept_when_remember_is_off(self):
        viewprefs.save(viewprefs.ViewPrefs(size="1200x800"), self.path)
        self.assertEqual(json.loads(self.path.read_text())["size"], "")

    def test_a_symbolic_link_is_neither_read_nor_written(self):
        self.path.parent.mkdir(parents=True)
        target = Path(self.tmp.name) / "elsewhere.json"
        target.write_text(json.dumps({"text_size": "larger"}))
        os.symlink(target, self.path)
        self.assertEqual(viewprefs.load(self.path), viewprefs.ViewPrefs())
        self.assertIn("symbolic link", viewprefs.save(viewprefs.ViewPrefs("smaller"), self.path))
        self.assertEqual(json.loads(target.read_text()), {"text_size": "larger"})


class ResultTextTests(unittest.TestCase):
    def test_split_result(self):
        self.assertEqual(split_result("Unpacked (Setup was not run).\nExtraction finished.\nLog: x"),
                         ("Unpacked (Setup was not run).", "Extraction finished.\nLog: x"))
        self.assertEqual(split_result("Stopping..."), ("Stopping...", ""))
        self.assertEqual(split_result(""), ("", ""))

    def test_done_rows_replace_the_destination_problem(self):
        rows = [("installer", True, "Installer file chosen"), ("prefix", True, "Prefix exists"),
                ("plan", False, "the destination already exists: /p/drive_c/Games/g")]
        out = done_rows(rows, "/p/drive_c/Games/g")
        self.assertEqual(out[-1], ("plan", True, "Done: the files are in /p/drive_c/Games/g"))
        self.assertTrue(all(ok for _k, ok, _t in out))
        self.assertEqual(len(out), 3)
        # Run is still off after a done unpack: the window shows DONE_REASON instead
        self.assertEqual(run_reason(out, False), "")
        self.assertIn("choose another prefix", DONE_REASON)


class HelpTextTests(unittest.TestCase):
    def test_keys_name_niri_keys_only_on_niri(self):
        niri = key_help_text("niri")
        self.assertIn("Mod+F", niri)
        self.assertIn("niri has no minimize", niri)
        self.assertIn("Ctrl+O", niri)
        other = key_help_text("GNOME")
        self.assertNotIn("Mod+F", other)
        self.assertIn("title bar", other)

    def test_about(self):
        text = about_text("9.9.9")
        self.assertIn("gitfurl 9.9.9", text)
        self.assertIn("GPL-3.0-or-later", text)


if __name__ == "__main__":
    unittest.main()
