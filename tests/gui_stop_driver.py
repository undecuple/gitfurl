"""Drives the real Stop handler inside gitfurl.gui.main() (run by tests/test_gui_stop.py).

Run under a virtual display: `xvfb-run -a python tests/gui_stop_driver.py SCENARIO INSTALLER`.
It only patches the outside of the window: tkinter's mainloop (the scenario runs instead of
blocking), the message boxes, the file dialog and `run_tracked` (a fake that waits for an event;
no process is started, no Wine is used). The widgets, `run_clicked`, `stop_clicked`, `poll`,
`start_busy` and `end_busy` are the real ones. HOME and the XDG folders must point at a
temporary folder.
"""
from __future__ import annotations

import sys
import threading
import time
import tkinter
from pathlib import Path
from tkinter import filedialog, messagebox

from gitfurl import gui
from gitfurl.memlimit import Availability

dialogs: list[str] = []  # titles of the yes/no dialogs shown, in order
errors: list[str] = []
answers: dict[str, object] = {}  # title -> bool, or a callable returning bool
released = threading.Event()  # lets the fake installer run end
controls: list = []  # the RunControl of each run started


def fake_run_tracked(plan, tool, create, track, env=None, run=None, options=None):
    controls.append(options.control)
    released.wait()
    return (0, Path("fake.log"), None)


def ask(title, *_a, **_k):
    dialogs.append(title)
    ans = answers[title]
    return ans() if callable(ans) else ans


def walk(widget):
    yield widget
    for child in widget.winfo_children():
        yield from walk(child)


def button(root, text):
    for wd in walk(root):
        if getattr(wd, "text", None) == text and hasattr(wd, "_click"):
            return wd
    raise AssertionError(f"no button {text!r}")


def label_texts(root):
    out = []
    for wd in walk(root):
        if isinstance(wd, tkinter.Label):
            try:
                out.append(root.getvar(str(wd.cget("textvariable"))))
            except tkinter.TclError:
                pass
    return "\n".join(out)


def pump(root, until, what, seconds=10.0):
    end = time.monotonic() + seconds
    while not until():
        assert time.monotonic() < end, f"timed out waiting for {what}"
        root.update()
        time.sleep(0.005)


def settle(root, rounds=20):
    for _ in range(rounds):
        root.update()
        time.sleep(0.005)


def start_run(root):
    n = len(controls)
    answers["Run this installer?"] = True
    button(root, "Run installer")._click(None)
    pump(root, lambda: len(controls) > n, "the run to start")
    return controls[-1]


def stop_visible(root):
    return button(root, "Stop").winfo_manager() == "pack"


def stop_enabled(root):
    return button(root, "Stop")._state == "normal"


def scenario(root, name):
    stop = button(root, "Stop")
    control = start_run(root)
    assert stop_visible(root) and stop_enabled(root)
    assert "Close its window" in label_texts(root)
    if name == "yes":
        answers["Stop the installer?"] = True
        stop._click(None)
        assert dialogs[-1] == "Stop the installer?"
        assert not stop_enabled(root)
        assert "Stopping the installer..." in label_texts(root)
        pump(root, lambda: control.cancelled, "the stop request")
        released.set()
        pump(root, lambda: "Cancelled." in label_texts(root), "the cancelled result")
        assert not stop_visible(root)
    elif name == "no":
        answers["Stop the installer?"] = False
        stop._click(None)
        assert dialogs[-1] == "Stop the installer?"
        settle(root)
        assert not control.cancelled
        assert stop_enabled(root) and stop_visible(root)
        text = label_texts(root)
        assert "Close its window" in text and "Stopping" not in text
        released.set()
        pump(root, lambda: "Installer exited with status 0" in label_texts(root), "the result")
    elif name == "ended_while_open":
        def run_ends_then_yes():
            released.set()  # the run ends while the dialog is open
            pump(root, lambda: "Installer exited with status 0" in label_texts(root),
                 "the result while the dialog is open")
            return True

        answers["Stop the installer?"] = run_ends_then_yes
        stop._click(None)
        settle(root)
        text = label_texts(root)
        assert "Installer exited with status 0" in text, text
        assert "Stopping" not in text and "Cancelled" not in text
        assert not control.cancelled  # the late Yes did not stop or mark anything
        assert stop_enabled(root) and not stop_visible(root)
        # The next run's Stop must be usable: the stale Yes must not have left it disabled.
        released.clear()
        control2 = start_run(root)
        assert control2 is not control
        assert stop_visible(root) and stop_enabled(root)
        assert "Close its window" in label_texts(root)
        answers["Stop the installer?"] = False
        stop._click(None)
        assert dialogs.count("Stop the installer?") == 2
        settle(root)
        assert not control2.cancelled and stop_enabled(root)
        released.set()
        pump(root, lambda: not stop_visible(root), "the second run to end")
    else:
        raise AssertionError(f"unknown scenario {name}")
    assert not errors, errors


def main() -> int:
    name, installer = sys.argv[1], sys.argv[2]
    gui.run_tracked = fake_run_tracked
    # The memory limit stays on (its default) but is reported as available, so the case does
    # not depend on this machine's systemd; run_tracked is a fake, so no scope is created.
    gui.memory_availability = lambda *_a, **_k: Availability(
        True, "test", "/fake/systemd-run", "/fake/systemctl")
    gui.memory_problem = lambda *_a, **_k: None
    messagebox.askyesno = ask
    messagebox.showerror = lambda title, msg, **_k: errors.append(f"{title}: {msg}")
    filedialog.askopenfilename = lambda **_k: installer
    result = {}

    def fake_mainloop(self, n=0):
        try:
            self.update()
            button(self, "Browse...")._click(None)
            self.update()
            scenario(self, name)
            result["ok"] = True
        finally:
            self.destroy()

    tkinter.Tk.mainloop = fake_mainloop
    gui.main(start="details")
    assert result.get("ok"), "scenario did not finish"
    print("OK", name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
