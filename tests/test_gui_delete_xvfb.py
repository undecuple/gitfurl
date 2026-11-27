"""The library's "Delete..." in a real window under Xvfb, with a synthetic prefix and menu
entry in a temporary HOME. No Wine. Skipped when xvfb-run or tkinter is missing."""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from gitfurl.setup_entries import shortcut_text

HERE = Path(__file__).resolve().parent


class GuiDeleteTests(unittest.TestCase):
    def run_scenario(self, name):
        if shutil.which("xvfb-run") is None:
            self.skipTest("xvfb-run not available")
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        with tempfile.TemporaryDirectory() as tmp:
            home = os.path.realpath(tmp)
            prefix = Path(home) / "d" / "gitfurl" / "prefixes" / "game"
            (prefix / "drive_c" / "windows").mkdir(parents=True)
            (prefix / "system.reg").write_text("WINE REGISTRY Version 2\n")
            outside = Path(home) / "outside.txt"
            outside.write_text("keep")
            os.symlink(outside, prefix / "drive_c" / "link.txt")
            apps = Path(home) / "d" / "applications"
            apps.mkdir(parents=True)
            entry = apps / "gitfurl-game-game.desktop"
            entry.write_text(shortcut_text("Game", prefix, "game", "C:\\Games\\game\\g.exe", [],
                                           None))
            env = {**os.environ, "PYTHONPATH": f"{HERE.parent / 'src'}:{HERE}", "HOME": home,
                   "XDG_DATA_HOME": home + "/d", "XDG_STATE_HOME": home + "/s",
                   "XDG_CONFIG_HOME": home + "/c"}
            proc = subprocess.Popen(
                ["xvfb-run", "-a", sys.executable, str(HERE / "gui_delete_driver.py"), name],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            try:
                out, err = proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                self.fail("GUI scenario timed out")
            state = (os.path.lexists(prefix), entry.exists(), outside.read_text())
        self.assertEqual(proc.returncode, 0, (out + err)[-3000:])
        self.assertIn(f"OK {name}", out)
        return state

    def test_delete(self):
        self.assertEqual(self.run_scenario("delete"), (False, False, "keep"))

    def test_cancel(self):
        self.assertEqual(self.run_scenario("cancel"), (True, True, "keep"))


if __name__ == "__main__":
    unittest.main()
