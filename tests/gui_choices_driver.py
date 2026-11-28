"""Drives the window's "Setup's choices" card inside gitfurl.gui.main() (run by
tests/test_setup_choices.py under Xvfb).

`xvfb-run -a python tests/gui_choices_driver.py INSTALLER OTHER`: two synthetic Inno Setup
installers (tests/inno_writer.py), no archives beside them, so the route is Wine/Proton. The
card starts at the script's defaults, the unattended switch puts the answers into the plan's
command line, a changed checkbox changes the plan, the approval shows the answers (it is
declined, so nothing runs) and another installer starts again at its defaults. Only the
outside of the window is patched: mainloop, the message boxes and the file dialog.
"""
from __future__ import annotations

import sys
from tkinter import filedialog, messagebox

from gui_stop_driver import button, pump, settle

from gitfurl import gui

messages: list[str] = []
errors: list[str] = []


def main() -> int:
    installer, other = sys.argv[1], sys.argv[2]
    chosen = {"path": installer}
    messagebox.askyesno = lambda title, message="", *_a, **_k: messages.append(message) or False
    messagebox.showerror = lambda title, msg, **_k: errors.append(f"{title}: {msg}")
    filedialog.askopenfilename = lambda **_k: chosen["path"]
    result = {}

    def driver(root, ui):
        plan = lambda: ui["plan_box"].get("1.0", "end")  # noqa: E731
        panel = ui["choices_panel"]
        root.update()
        button(root, "Browse...")._click(None)
        pump(root, lambda: panel.loaded is not None, "the setup's questions")
        assert "For information only" in panel.note.cget("text"), panel.note.cget("text")
        assert panel.vars["t:quicklaunch"].get() is False  # unchecked in the script
        assert panel.vars["t:desktopicon"].get() is True
        ui["unattended_var"].set(True)
        ui["refresh_plan"]()
        settle(root)
        assert "/TASKS=desktopicon,!quicklaunch" in plan(), plan()
        panel.vars["t:quicklaunch"].set(True)
        panel._changed()
        pump(root, lambda: "/TASKS=desktopicon,quicklaunch" in plan(), "the changed answer")
        assert "tasks desktopicon, quicklaunch" in plan(), plan()
        ui["wine_path"]["value"] = "/nonexistent/wine"  # never started: the approval is declined
        ui["run_clicked"]()
        assert messages and "/TASKS=desktopicon,quicklaunch" in messages[-1], messages
        assert "choices: " in messages[-1]
        chosen["path"] = other
        button(root, "Browse...")._click(None)
        pump(root, lambda: panel.key is not None and panel.key[0] == other, "the other installer")
        settle(root)
        assert panel.vars["t:quicklaunch"].get() is False, "not reset for another installer"
        assert "/TASKS=desktopicon,!quicklaunch" in plan(), plan()
        result["ok"] = True
        root.destroy()

    gui.main(start="details", driver=driver)
    assert result.get("ok"), "scenario did not finish"
    assert not errors, errors
    print("OK choices")
    return 0


if __name__ == "__main__":
    sys.exit(main())
