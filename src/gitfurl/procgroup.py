"""Small helpers shared by anything that starts Wine in its own process group."""

from __future__ import annotations

import os
import shutil
import signal
import threading
from pathlib import Path


class SigintGuard:
    """Hold SIGINT back on the main thread: remember it instead of raising KeyboardInterrupt.

    Outside the main thread (where Python never delivers SIGINT) it does nothing.
    """

    def __init__(self):
        self.hits = 0
        self._previous = None
        self._active = False

    def __enter__(self):
        if threading.current_thread() is threading.main_thread():
            try:
                self._previous = signal.signal(signal.SIGINT, self._note)
                self._active = True
            except (ValueError, OSError):
                pass
        return self

    def _note(self, signum, frame):
        self.hits += 1

    def release(self) -> None:
        """Put the previous SIGINT handler back. Safe to call more than once."""
        if self._active and self._previous is not None:
            self._active = False
            signal.signal(signal.SIGINT, self._previous)

    def __exit__(self, *exc):
        self.release()
        return False


def group_alive(pgid: int) -> bool:
    """True while a live (not zombie) process is in process group `pgid`. Read-only.

    The caller must keep the group leader unreaped, so the id cannot be reused meanwhile.
    """
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", encoding="utf-8", errors="replace") as fh:
                fields = fh.read().rpartition(")")[2].split()
            if int(fields[2]) == pgid and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def wineserver_for(wine: str) -> str | None:
    """`wineserver` beside an absolute wine path, else found on PATH. Always an absolute path.

    A relative result (an empty or "." PATH entry) would resolve against the current
    directory, so it is refused.
    """
    if os.path.isabs(wine):
        beside = Path(wine).with_name("wineserver")
        if beside.is_file():
            return str(beside)
    found = shutil.which("wineserver")
    return found if found and os.path.isabs(found) else None
