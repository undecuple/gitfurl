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
    found = window_list(pids, programs, env, run)
    if found is None:
        return None
    titles: list[str] = []
    for _wid, title in found:
        if title not in titles:
            titles.append(title)
    return titles


def window_list(pids, programs, env: dict[str, str] | None = None, run=subprocess.run,
                pid_of: dict[str, int] | None = None) -> list[tuple[str, str]] | None:
    """(window id, title) of the windows open by `pids`, as window_titles finds them.
    `pid_of` caches each window id's _NET_WM_PID between calls (an X window keeps its
    owner), so a repeated call asks `xprop` only about new windows. Never raises."""
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
    rows = _LINE.findall(tree)
    if pid_of is not None:  # a closed window's id can be reused by a new window
        listed = {row[0] for row in rows}
        for wid in [w for w in pid_of if w not in listed]:
            del pid_of[wid]
    found: list[tuple[str, str]] = []
    checked, spent = 0, False
    # xwininfo lists bottom to top; the newest windows are usually on top, so they are
    # looked at first and are not the ones the caps leave out.
    for wid, title, name, cls in reversed(rows):
        if not title.strip() or title in HIDDEN_TITLES \
                or not (_matches(cls, programs) or _matches(name, programs)):
            continue
        pid = pid_of.get(wid) if pid_of is not None else None
        if pid is None:
            if spent:
                continue  # no probe left for this one; windows already known still count
            left = deadline - time.monotonic()
            if checked >= MAX_CHECKED or left <= 0:
                spent = True
                continue
            checked += 1
            out = _probe([xprop, "-id", wid, "_NET_WM_PID"], env, run, min(TIMEOUT_S, left))
            m = _PID.search(out or "")
            if not m:
                continue
            pid = int(m.group(1))
            if pid_of is not None:
                pid_of[wid] = pid
        if pid in pids:
            found.append((wid, clean(title, 80)))
            if len(found) >= MAX_TITLES:
                break
    found.reverse()  # back to xwininfo's order
    return found


class WindowTracker:
    """When each of the run's windows first appeared, to name the newest one in a stall
    note. Read-only, like window_list. `lister` is window_list (tests inject a fake)."""

    def __init__(self, env: dict[str, str] | None = None, lister=None):
        self.env, self.lister = env, lister or window_list
        self.pid_of: dict[str, int] = {}
        self.first_seen: dict[str, float] = {}
        self.open: list[tuple[str, str]] = []
        self.observations = 0
        self.first_look: float | None = None

    def observe(self, pids, programs, elapsed_s: float) -> list[tuple[str, str]] | None:
        """One look at the windows (at a progress sample): the windows found, or None when
        they could not be read. Never raises."""
        try:
            found = self.lister(pids, programs, self.env, pid_of=self.pid_of)
        except Exception:  # noqa: BLE001 - advice only; it must never break the run
            found = None
        if found is None:
            self.open = []
            return None
        self.observations += 1
        if self.first_look is None:
            self.first_look = elapsed_s
        now_ids = {wid for wid, _t in found}
        for wid in [w for w in self.first_seen if w not in self.pid_of and w not in now_ids]:
            del self.first_seen[wid]  # closed: its id may come back for a new window
        for wid, _title in found:
            self.first_seen.setdefault(wid, elapsed_s)
        self.open = list(found)
        return found

    def newest(self) -> tuple[str, float] | None:
        """(title, seconds into the run it first appeared) of the open window that appeared
        last, when it appeared after the run's first look at its windows (so it is not just
        the installer's main window), or None."""
        if not self.open or self.observations < 2:
            return None
        wid, title = max(self.open, key=lambda wt: self.first_seen[wt[0]])
        seen = self.first_seen[wid]
        return None if seen <= self.first_look else (title, seen)


def newest_text(newest: tuple[str, float] | None, elapsed_s: float) -> str:
    """One sentence naming the newest window for a stall note, or ""."""
    if newest is None:
        return ""
    title, seen = newest
    ago = max(0, round(elapsed_s - seen))
    return (f' The newest of them, "{title}", opened {round(seen)} s into the run ({ago} s '
            "ago) and is still open; it is the most likely to be waiting for an answer.")


def titles_text(titles: list[str] | None) -> str:
    """One sentence for a note, or "" when nothing is known."""
    if not titles:
        return ""
    shown = ", ".join(f'"{t}"' for t in titles)
    return (f" Windows open in the run: {shown}; one of them may be waiting for an answer "
            "(gitfurl does not answer or close them).")
