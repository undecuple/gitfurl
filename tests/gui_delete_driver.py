"""Drives the library's "Delete..." inside gitfurl.gui.main() (run by
tests/test_gui_delete_xvfb.py).

Run under a virtual display: `xvfb-run -a python tests/gui_delete_driver.py SCENARIO`, with
HOME and the XDG folders pointing at a temporary folder that holds a synthetic prefix "game".
Only the outside of the window is patched: the message boxes.
"""
from __future__ import annotations

import sys
from tkinter import messagebox

from gui_stop_driver import button, pump, settle

from gitfurl import gui

asked: list[tuple[str, str, dict]] = []
errors: list[str] = []


def main() -> int:
    name = sys.argv[1]
    messagebox.askokcancel = lambda title, message="", **kw: (
        asked.append((title, message, kw)) or name == "delete")
    messagebox.showerror = lambda title, msg, **_k: errors.append(f"{title}: {msg}")
    result = {}

    def driver(root, ui):
        settle(root)
        button(root, "Delete...")._click(None)
        assert asked, f"no approval was asked ({errors})"
        title, message, kw = asked[0]
        assert title == "Delete prefix?", title
        assert kw.get("default") == "cancel", kw
        assert "This cannot be undone." in message, message
        assert "Menu entries to remove (1):" in message, message
        if name == "delete":
            pump(root, lambda: ui["lib_status_var"].get().startswith("Deleted"),
                 "the deletion to finish")
            assert "game" not in ui["delete_buttons"], ui["delete_buttons"]
            assert not ui["busy"]["on"]
        else:
            settle(root)
            assert ui["lib_status_var"].get() == "", ui["lib_status_var"].get()
            assert "game" in ui["delete_buttons"]
        result["ok"] = True
        root.destroy()

    gui.main(start="library", driver=driver)
    assert result.get("ok"), "scenario did not finish"
    print("OK", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
