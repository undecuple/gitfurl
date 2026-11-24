"""Titles of the X11 windows that a run's own processes have open. Read-only.

Used only to explain a crash or a stall ("an error window may be waiting for an answer"):
it lists windows, it never clicks, closes or types into one. It needs `xwininfo` and
`xprop` on PATH and a DISPLAY (Wine draws its windows through X11, also under XWayland);
without them it returns None and the note says nothing about windows.

How: `xwininfo -root -tree` lists every window with its title and class. Wine names a
window's class after the program (for example "setup.tmp"), so only windows whose class
matches a program running in the prefix are checked further, and each of those must have a
_NET_WM_PID (from `xprop`) that is one of the prefix's processes. Every call has a short
timeout, all of them together at most TOTAL_S, and at most MAX_CHECKED windows are checked.
In a portable run (bwrap) the window pids are the sandbox's own, so the runner does not ask.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import time

from gitfurl.detect import clean

TIMEOUT_S = 2.0
TOTAL_S = 3.0  # for all probes of one call, below progress.STOP_WAIT_S
MAX_CHECKED = 16
MAX_TITLES = 6
# Hidden windows Windows (and Wine) give every GUI program for keyboard input: never what a
# user waits on ("Default IME" was listed in the sandbox run of 2026-11-24).
HIDDEN_TITLES = frozenset(["Default IME", "MSCTFIME UI"])

# `     0x1e00007 "Title": ("res_name" "res_class")  400x200+0+0  +100+100`
# xwininfo prints titles without escaping, so the title runs to the last '": (' on the line.
_LINE = re.compile(r'^\s*(0x[0-9a-fA-F]+) "(.*)": \("([^"\n]*)" "([^"\n]*)"\)', re.M)
_PID = re.compile(r"_NET_WM_PID\(CARDINAL\) = (\d+)")


def _probe(argv: list[str], env: dict[str, str], run, timeout: float = TIMEOUT_S) -> str | None:
    try:
        proc = run(argv, capture_output=True, text=True, errors="replace", timeout=timeout,
                   check=False, stdin=subprocess.DEVNULL, env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _matches(cls: str, programs: list[str]) -> bool:
    """A window class against /proc names (which the kernel cuts to 15 characters)."""
    low = cls.casefold()
    return any(low == p or (len(p) == 15 and low.startswith(p)) for p in programs)


def window_titles(pids, programs, env: dict[str, str] | None = None,
                  run=subprocess.run) -> list[str] | None:
    """Titles of the windows open by `pids` (the prefix's processes), whose classes match
    `programs` (their /proc names). None when the windows cannot be read (no DISPLAY, no
    xwininfo or xprop, or a probe failed). Never raises."""
    env = dict(os.environ if env is None else env)
    pids = {int(p) for p in pids}
    programs = [str(p).casefold() for p in programs if p]
    if not env.get("DISPLAY"):
        return None
    xwininfo = shutil.which("xwininfo", path=env.get("PATH"))
    xprop = shutil.which("xprop", path=env.get("PATH"))
    if not (xwininfo and xprop):
        return None
    if not pids or not programs:
        return []
    deadline = time.monotonic() + TOTAL_S
    tree = _probe([xwininfo, "-root", "-tree"], env, run)
    if tree is None:
        return None
    titles: list[str] = []
    checked = 0
    for wid, title, name, cls in _LINE.findall(tree):
        if not title.strip() or title in HIDDEN_TITLES \
                or not (_matches(cls, programs) or _matches(name, programs)):
            continue
        checked += 1
        left = deadline - time.monotonic()
        if checked > MAX_CHECKED or left <= 0:
            break
        out = _probe([xprop, "-id", wid, "_NET_WM_PID"], env, run, min(TIMEOUT_S, left))
        m = _PID.search(out or "")
        shown = clean(title, 80)
        if m and int(m.group(1)) in pids and shown not in titles:
            titles.append(shown)
            if len(titles) >= MAX_TITLES:
                break
    return titles


def titles_text(titles: list[str] | None) -> str:
    """One sentence for a note, or "" when nothing is known."""
    if not titles:
        return ""
    shown = ", ".join(f'"{t}"' for t in titles)
    return (f" Windows open in the run: {shown}; one of them may be waiting for an answer "
            "(gitfurl does not answer or close them).")
