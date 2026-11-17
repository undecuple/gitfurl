"""Advisory checks shown before a run: what can be measured, and what cannot.

Everything here is read-only and local. Measured values come from the machine; anything
that is a guess is labelled "estimate". Nothing here blocks a run: the install's real space
need is unknown to gitfurl, so no multiplier is applied to the installer's size.
"""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

LOW_MEMORY_BYTES = 2 * 1024**3  # an estimate threshold for a warning, not a requirement
MAX_ENTRIES = 20000
MAX_SECONDS = 3.0


@dataclass
class Preflight:
    free_bytes: int | None  # measured: free space on the filesystem that will hold the prefix
    free_where: Path
    folder_bytes: int  # measured: regular files in the installer's folder
    folder_complete: bool  # False when counting stopped early (the value is "at least")
    mem_bytes: int | None  # measured: MemAvailable
    cpu_threads: int  # measured

    def lines(self) -> list[str]:
        def gb(n):
            return f"{n / 1e9:.2f} GB"

        out = []
        out.append("measured: free space where the prefix will live ("
                   f"{self.free_where}): " + ("unknown" if self.free_bytes is None else gb(self.free_bytes)))
        size = gb(self.folder_bytes)
        out.append("measured: size of the installer's own files: "
                   + (size if self.folder_complete else f"at least {size} (stopped counting early)"))
        out.append("measured: memory available: "
                   + ("unknown" if self.mem_bytes is None else gb(self.mem_bytes))
                   + f", {self.cpu_threads} CPU threads")
        out.append("space the install needs: unknown. estimate: the installed size is usually larger "
                   "than the installer's files, by an amount gitfurl cannot tell.")
        return out

    def warnings(self) -> list[str]:
        out = []
        if self.free_bytes is not None and self.free_bytes < self.folder_bytes:
            out.append("advisory, estimate: free space is smaller than the installer's own files, "
                       "so the install will very likely not fit. You can still go ahead.")
        if self.mem_bytes is not None and self.mem_bytes < LOW_MEMORY_BYTES:
            out.append("advisory, estimate: under 2 GB of memory is available; heavy decompression "
                       "may be slow or fail. You can still go ahead.")
        return out

    def describe(self) -> str:
        return "\n".join(self.lines() + self.warnings())


def folder_size(folder: Path, max_entries: int = MAX_ENTRIES,
                max_seconds: float = MAX_SECONDS) -> tuple[int, bool]:
    """Total bytes of regular files under folder. Symlinks are not followed or counted.

    Returns (bytes, complete). It stops after max_entries entries or max_seconds, and then
    complete is False and the number is a lower bound.
    """
    total, seen, t0 = 0, 0, time.monotonic()
    stack = [folder]
    while stack:
        cur = stack.pop()
        try:
            with os.scandir(cur) as it:
                for e in it:
                    seen += 1
                    if seen > max_entries or time.monotonic() - t0 > max_seconds:
                        return total, False
                    try:
                        if e.is_dir(follow_symlinks=False):
                            stack.append(Path(e.path))
                        elif e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total, True


def nearest_existing(path: Path) -> Path:
    cur = path
    while not cur.exists() and cur != cur.parent:
        cur = cur.parent
    return cur


def memory_available(meminfo: str = "/proc/meminfo") -> int | None:
    try:
        with open(meminfo, encoding="ascii") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return None


def measure(installer: Path, prefix: Path, meminfo: str = "/proc/meminfo") -> Preflight:
    where = nearest_existing(prefix)
    try:
        free = shutil.disk_usage(where).free
    except OSError:
        free = None
    size, complete = folder_size(installer.parent)
    return Preflight(free, where, size, complete, memory_available(meminfo), os.cpu_count() or 0)
