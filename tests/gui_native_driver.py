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
        settle(root)
        preview = ui["timeline_box"].get("1.0", "end")
        assert "Route: native" in preview and "Nothing has run yet" in preview, preview
        answers["Unpack this installer?"] = True
        ui["run_clicked"]()
        assert dialogs[-1] == "Unpack this installer?", dialogs
        assert "route: native (chosen by gitfurl)" in messages[-1]
        pump(root, lambda: "Unpacked (Setup was not run)." in texts(), "the unpack", 30)
        assert "Local log:" in texts()
        assert not ui["busy"]["on"]
        # after the unpack: shown as done, not as a problem; no stale percentage
        settle(root)
        assert ui["head_var"].get() == "Unpacked (Setup was not run).", ui["head_var"].get()
        assert ui["body_var"].get().startswith("Extraction finished"), ui["body_var"].get()
        assert ui["progress_var"].get() == gui.RUN_ENDED, ui["progress_var"].get()
        rows = ui["plan_checklist_rows"]()
        assert rows["plan"][1].startswith("Done: the files are in"), rows
        assert ui["reason_var"].get() == gui.DONE_REASON, ui["reason_var"].get()
        assert ui["run_btn"]._state == "disabled"
        assert [ui["menubar"].entrycget(i, "label") for i in (0, 1, 2)] == [
            "File", "View", "Help"], "menu bar"
        check_timeline(root, ui)
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
        settle(root)
        shown = ui["timeline_box"].get("1.0", "end")
        assert "You pressed Stop" in shown, shown
        assert shown.strip().splitlines()[-1].split("  ", 1)[1].startswith("Stopped."), shown
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
        # The setup entries switch: off by default; on, the plan and the approval list it.
        assert ui["entries_var"].get() is False
        assert "add the registry entries" not in plan_text()
        ui["entries_var"].set(True)
        ui["refresh_plan"]()
        settle(root)
        assert "add the registry entries" in plan_text(), plan_text()[-1500:]
        answers["Unpack this installer?"] = False
        ui["run_clicked"]()
        assert "add the registry entries" in messages[-1], messages[-1][-1500:]
        settle(root)
        assert not ui["busy"]["on"] and not started.is_set()
    elif name == "destination":
        check_destination(root, ui, plan_text, texts)
    else:
        raise AssertionError(f"unknown scenario {name}")
    assert not errors, errors


def check_destination(root, ui, plan_text, texts):
    """The Destination row: the default, Browse (the folder dialog is a stand-in) inside and
    outside the prefix's drive_c, a typed folder that is refused, the unpack into a nested new
    folder, and the reset when another prefix is chosen."""
    from gitfurl.prefixes import prefixes_root

    drive_c = prefixes_root() / "default" / "drive_c"
    settle(root)
    assert ui["dest_var"].get() == "C:\\Games\\default", ui["dest_var"].get()
    assert ui["dest_widgets"]["browse"]._state == "normal"
    picked = {"path": str(drive_c / "Apps" / "Studio")}
    seen: list[dict] = []
    filedialog.askdirectory = lambda **k: (seen.append(k), picked["path"])[1]
    ui["browse_dest"]()
    settle(root)
    assert seen[-1]["initialdir"] == str(drive_c), seen
    assert ui["dest_var"].get() == "C:\\Apps\\Studio\\default", ui["dest_var"].get()
    assert "destination: C:\\Apps\\Studio\\default" in plan_text(), plan_text()[-1500:]
    picked["path"] = str(drive_c.parent)  # the prefix folder itself: outside drive_c
    ui["browse_dest"]()
    settle(root)
    assert errors and errors[-1].startswith("Destination refused:"), errors
    assert "is not inside this prefix's drive C:" in errors[-1], errors
    errors.clear()
    assert ui["dest_var"].get() == "C:\\Apps\\Studio\\default"  # unchanged
    ui["dest_var"].set("C:\\windows\\x")  # typed: refused, Run is off
    settle(root)
    assert "Wine's own folder" in plan_text(), plan_text()[-1500:]
    assert ui["run_btn"]._state == "disabled"
    ui["dest_var"].set("C:\\Apps\\Studio\\My Game")
    settle(root)
    assert "destination: C:\\Apps\\Studio\\My Game" in plan_text(), plan_text()[-1500:]
    assert "the folder you chose" in plan_text()
    answers["Unpack this installer?"] = True
    ui["run_clicked"]()
    assert "destination: C:\\Apps\\Studio\\My Game" in messages[-1], messages[-1][-2000:]
    pump(root, lambda: "Unpacked (Setup was not run)." in texts(), "the unpack", 30)
    settle(root)
    assert (drive_c / "Apps" / "Studio" / "My Game" / "bin" / "Game.exe").is_file()
    assert ui["plan_checklist_rows"]()["plan"][1].startswith("Done: the files are in")
    ui["prefix_var"].set("other")  # another prefix: back to the default, nothing remembered
    ui["refresh_plan"]()
    settle(root)
    assert ui["dest_var"].get() == "C:\\Games\\other", ui["dest_var"].get()
    assert ui["dest_widgets"]["browse"]._state == "disabled"  # "other" does not exist yet


def check_timeline(root, ui):
    """The Timeline panel after the unpack: the route and its reason, every step, and the
    done line last; then the library's Last run shows the same saved timeline."""
    settle(root)
    shown = ui["timeline_box"].get("1.0", "end").strip()
    assert "Route: native (gitfurl unpacks the archives itself; gitfurl chose it)" in shown, shown
    assert "gitfurl can unpack them itself" in shown, shown  # the route's reason
    assert "You approved the plan" in shown, shown
    assert "Opened NAME-01.bin" in shown and "files written" in shown, shown
    last = shown.splitlines()[-1]
    assert last.split("  ", 1)[1].startswith("Unpacked (Setup was not run)."), last
    tl = ui["timeline"]["tl"]
    assert tl.ended and tl.path is not None and tl.path.stat().st_mode & 0o777 == 0o600
    ui["show"]("library")
    settle(root)
    assert "default" in ui["timeline"]["buttons"], ui["timeline"]["buttons"]
    ui["timeline"]["buttons"]["default"]._click(None)
    settle(root)
    win, box = ui["timeline"]["last_run"]["default"]
    saved = box.get("1.0", "end")
    assert "Route: native" in saved and "Unpacked (Setup was not run)." in saved, saved
    assert str(box.cget("state")) == "disabled"  # read-only
    win.destroy()
    ui["show"]("details")
    settle(root)


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
