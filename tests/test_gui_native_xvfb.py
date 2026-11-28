"""Drive the window's native route (Run, the approval, the unpack, Stop and the 'Run Setup
instead' switch) under Xvfb with a synthetic installer. No Wine and no real installer: the
prefix folder is made beforehand, so nothing but gitfurl's own unpack runs. Skipped when
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

try:
    from farc_writer import make_archive
    from test_route import INNO, chain_blocks, lzma_blocks
except ImportError:  # python -m unittest tests.test_gui_native_xvfb
    from tests.farc_writer import make_archive
    from tests.test_route import INNO, chain_blocks, lzma_blocks

HERE = Path(__file__).resolve().parent


class GuiNativeTests(unittest.TestCase):
    def run_scenario(self, name):
        if shutil.which("xvfb-run") is None:
            self.skipTest("xvfb-run not available")
        try:
            import tkinter  # noqa: F401
        except ImportError:
            self.skipTest("tkinter not installed")
        with tempfile.TemporaryDirectory() as home:
            src = Path(home) / "NAME game"
            src.mkdir()
            installer = src / "setup.exe"
            installer.write_bytes(INNO)
            for n, blocks in (("NAME-01.bin", lzma_blocks()), ("NAME-02.bin", chain_blocks())):
                (src / n).write_bytes(make_archive(*blocks))
            prefix = Path(home) / "d" / "gitfurl" / "prefixes" / "default"
            (prefix / "drive_c").mkdir(parents=True)
            bin_dir = Path(home) / "bin"
            bin_dir.mkdir()
            fake_wine = bin_dir / "wine"
            fake_wine.write_text("#!/bin/sh\necho wine-9.0\n")
            fake_wine.chmod(0o755)
            env = {**os.environ, "PYTHONPATH": f"{HERE.parent / 'src'}:{HERE}", "HOME": home,
                   "XDG_DATA_HOME": home + "/d", "XDG_STATE_HOME": home + "/s",
                   "XDG_CONFIG_HOME": home + "/c", "PATH": f"{bin_dir}:{os.environ['PATH']}"}
            env.pop("PROTONPATH", None)
            proc = subprocess.Popen(
                ["xvfb-run", "-a", sys.executable, str(HERE / "gui_native_driver.py"), name,
                 str(installer)],
                env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                start_new_session=True)
            try:
                out, err = proc.communicate(timeout=90)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
                proc.communicate()
                self.fail("GUI scenario timed out")
            written = list(prefix.rglob("Game.exe"))
        self.assertEqual(proc.returncode, 0, (out + err)[-3000:])
        self.assertIn(f"OK {name}", out)
        return written

    def test_unpack(self):
        self.assertEqual(len(self.run_scenario("unpack")), 1)

    def test_stop(self):
        self.assertEqual(self.run_scenario("stop"), [])

    def test_setup_instead_switch(self):
        self.assertEqual(self.run_scenario("setup_instead"), [])

    def test_declined(self):
        self.assertEqual(self.run_scenario("declined"), [])

    def test_destination(self):
        written = self.run_scenario("destination")
        self.assertEqual(len(written), 1, written)
        self.assertEqual(written[0].parts[-5:], ("Apps", "Studio", "My Game", "bin",
                                                 "Game.exe"))


if __name__ == "__main__":
    unittest.main()
