"""Drive the real Stop button of the window (stop_clicked, its dialog, poll) under Xvfb.

Only dummy things are used: a fake run function that waits for an event (no process, no Wine),
patched dialogs, a temporary HOME and a fake `wine` that only prints a version. Skipped when
xvfb-run or tkinter is missing.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent


class GuiStopTests(unittest.TestCase):
    def run_scenario(self, name):
        if shutil.which("xvfb-run") is None:
            self.skipTest("xvfb-run not available")
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        with tempfile.TemporaryDirectory() as home:
            bin_dir = Path(home) / "bin"
            bin_dir.mkdir()
            fake_wine = bin_dir / "wine"
            fake_wine.write_text("#!/bin/sh\necho wine-9.0\n")
            fake_wine.chmod(0o755)
            installer = Path(home) / "setup.exe"
            installer.write_bytes(b"not a real installer")
            env = {**os.environ, "PYTHONPATH": str(HERE.parent / "src"), "HOME": home,
                   "XDG_DATA_HOME": home + "/d", "XDG_STATE_HOME": home + "/s",
                   "XDG_CONFIG_HOME": home + "/c", "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            env.pop("PROTONPATH", None)
            # Own session, so a timeout stops exactly this group and nothing else.
            proc = subprocess.Popen(
                ["xvfb-run", "-a", sys.executable, str(HERE / "gui_stop_driver.py"), name,
                 str(installer)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            try:
                out, err = proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                self.fail("GUI scenario timed out")
        self.assertEqual(proc.returncode, 0, (out + err)[-2000:])
        self.assertIn(f"OK {name}", out)

    def test_yes_stops_the_run(self):
        self.run_scenario("yes")

    def test_no_changes_nothing(self):
        self.run_scenario("no")

    def test_run_ending_while_dialog_is_open(self):
        self.run_scenario("ended_while_open")

    def test_stop_during_prefix_creation(self):
        self.run_scenario("create_yes")

    def test_no_keeps_prefix_creation_running(self):
        self.run_scenario("create_no")

    def test_prefix_creation_ending_while_dialog_is_open(self):
        self.run_scenario("create_ended_while_open")


if __name__ == "__main__":
    unittest.main()
