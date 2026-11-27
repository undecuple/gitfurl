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


def wine_server_dir(prefix: Path, tmp: str = "/tmp") -> Path:
    """The folder a Wine server for `prefix` works in: <tmp>/.wine-<uid>/server-<dev>-<inode>,
    from the prefix folder's device and inode numbers (hex), as Wine names it. Reads the
    prefix folder's metadata only."""
    st = os.stat(prefix)
    return Path(tmp) / f".wine-{os.getuid()}" / f"server-{st.st_dev:x}-{st.st_ino:x}"


def prefix_server_running(prefix: Path, proc: str = "/proc", tmp: str = "/tmp") -> str | None:
    """Why a Wine server for `prefix` seems to be running, or None. Read-only.

    It looks for a process whose working folder is the prefix's server folder (a Wine server
    works from there) and for a lock held on that folder's lock file (/proc/locks). A
    left-over socket of a server that has ended is not counted. Processes of other users
    cannot be looked at; their servers use other folders anyway.
    """
    try:
        sdir = wine_server_dir(Path(prefix), tmp)
    except OSError:
        return None
    if not os.path.isdir(sdir):
        return None
    want = str(sdir)
    try:
        entries = os.listdir(proc)
    except OSError:
        entries = []
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            if os.readlink(f"{proc}/{entry}/cwd") == want:
                return f"process {entry} works in this prefix's Wine server folder ({sdir})"
        except OSError:
            continue
    try:
        st = os.lstat(sdir / "lock")
    except OSError:
        return None
    try:
        with open(f"{proc}/locks", encoding="ascii", errors="replace") as fh:
            locks = fh.read(1 << 20).splitlines()
    except OSError:
        return None
    for line in locks:
        fields = line.split()
        if "->" in fields or len(fields) < 6:
            continue
        try:
            major, minor, inode = fields[5].split(":")
            if (int(major, 16), int(minor, 16), int(inode)) == \
                    (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino):
                return f"the lock file of this prefix's Wine server is held ({sdir / 'lock'})"
        except ValueError:
            continue
    return None
