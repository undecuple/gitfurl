"""Drives the window's native route inside gitfurl.gui.main() (run by tests/test_gui_native_xvfb.py).

Run under a virtual display: `xvfb-run -a python tests/gui_native_driver.py SCENARIO INSTALLER`.
The installer is a synthetic one (an Inno Setup header and FreeArc archives from
tests/farc_writer.py) and the prefix folder already exists, so no Wine is used. Only the
outside of the window is patched: mainloop, the message boxes, the file dialog and, for the
stop scenario, the unpack (a fake that waits for Stop). HOME and the XDG folders must point at
a temporary folder.
"""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from tkinter import filedialog, messagebox

from gui_stop_driver import button, label_texts, pump, settle

from gitfurl import gui, route
from gitfurl.extract import ExtractResult

dialogs: list[str] = []
errors: list[str] = []
answers: dict[str, object] = {}
started = threading.Event()


messages: list[str] = []


def ask(title, message="", *_a, **_k):
    dialogs.append(title)
    messages.append(message)
    ans = answers[title]
    return ans() if callable(ans) else ans


def waiting_run(plan, cancel=None, **_kw):
    """Stands in for the unpack: waits for Stop, then reports a stopped run."""
    started.set()
    assert cancel.wait(30), "Stop never reached the unpack"
    return route.NativeResult(130, ExtractResult(plan.extract.dest, stopped=True),
                              Path("native.log"), None,
                              ["stopped: files already written are kept"])


def scenario(root, ui, name):
    texts = lambda: label_texts(root)  # noqa: E731
    plan_text = lambda: ui["plan_box"].get("1.0", "end")  # noqa: E731
    pump(root, lambda: "Setup is not run" in plan_text() or "native" in plan_text(),
         "the native plan preview")
    rows = ui["plan_checklist_rows"]()
    assert "not needed" in rows["runtime"][1], rows
    if name == "unpack":
        answers["Unpack this installer?"] = True
        ui["run_clicked"]()
        assert dialogs[-1] == "Unpack this installer?", dialogs
        assert "route: native (chosen by gitfurl)" in messages[-1]
        pump(root, lambda: "Unpacked (Setup was not run)." in texts(), "the unpack", 30)
        assert "Local log:" in texts()
        assert not ui["busy"]["on"]
    elif name == "stop":
        answers["Unpack this installer?"] = True
        ui["run_clicked"]()
        pump(root, started.is_set, "the unpack to start")
        assert ui["run"]["kind"] == "native"
        answers["Stop the unpack?"] = True
        ui["stop_clicked"]()
        assert dialogs[-1] == "Stop the unpack?", dialogs
        pump(root, lambda: "Stopped." in texts(), "the stopped result", 30)
        assert "Files already written are kept" in texts(), texts()
    elif name == "setup_instead":
        ui["setup_var"].set(True)
        ui["refresh_plan"]()
        settle(root)
        assert "not needed" not in ui["plan_checklist_rows"]()["runtime"][1]
        answers["Run this installer?"] = False
        ui["run_clicked"]()
        assert dialogs[-1] == "Run this installer?", dialogs
        assert "Unpack this installer?" not in dialogs
        # The approval names the route that runs, not the automatic one.
        assert "route: wine (chosen by you)" in messages[-1], messages[-1][-1500:]
        assert "Setup is not run" not in messages[-1]
    elif name == "declined":
        answers["Unpack this installer?"] = False
        ui["run_clicked"]()
        settle(root)
        assert not ui["busy"]["on"] and not started.is_set()
    else:
        raise AssertionError(f"unknown scenario {name}")
    assert not errors, errors


def main() -> int:
    name, installer = sys.argv[1], sys.argv[2]
    if name == "stop":
        real = route.perform_native
        route.perform_native = lambda *a, **kw: real(*a, run=waiting_run, **kw)
    messagebox.askyesno = ask
    messagebox.showerror = lambda title, msg, **_k: errors.append(f"{title}: {msg}")
    filedialog.askopenfilename = lambda **_k: installer
    result = {}

    def driver(root, ui):
        root.update()
        button(root, "Browse...")._click(None)
        root.update()
        scenario(root, ui, name)  # an exception here is reported by gui.main's driver hook
        result["ok"] = True
        root.destroy()

    gui.main(start="details", driver=driver)
    assert result.get("ok"), "scenario did not finish"
    print("OK", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
