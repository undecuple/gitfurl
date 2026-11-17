"""Optional report of what an installer changed.

Compares before and after snapshots of (a) the dedicated prefix, including which registry
keys appeared, and (b) a short list of host locations an installer could touch outside the
prefix (menu entries, autostart, the home folder's top level). It records names, sizes and
times only, never file contents, except registry key names. This is a best-effort view, not
proof of everything an installer did: it does not see changes elsewhere on the disk.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

REG_FILES = ("system.reg", "user.reg", "userdef.reg")
MAX_LISTED = 40


def host_watch(home: Path | None = None) -> list[tuple[Path, bool]]:
    """(path, recursive). Small, specific places only; big folders are listed one level deep."""
    h = home or Path(os.path.expanduser("~"))
    return [
        (h, False), (h / ".config", False), (h / ".local" / "share", False),
        (h / ".local" / "bin", True), (h / "Desktop", True), (h / ".config" / "autostart", True),
        (h / ".local" / "share" / "applications", True), (h / ".local" / "share" / "icons", True),
        (h / ".local" / "share" / "mime", True),
    ]


def _snap_tree(root: Path, recursive: bool, out: dict, skip: set[Path]) -> None:
    if not root.exists():
        return
    stack = [root]
    while stack:
        cur = stack.pop()
        try:
            entries = list(os.scandir(cur))
        except OSError:
            continue
        for e in entries:
            p = Path(e.path)
            if p in skip:
                continue
            try:
                st = e.stat(follow_symlinks=False)
            except OSError:
                continue
            out[str(p)] = (st.st_size, st.st_mtime_ns)
            if recursive and e.is_dir(follow_symlinks=False):
                stack.append(p)


def registry_keys(prefix: Path) -> set[str]:
    keys: set[str] = set()
    for name in REG_FILES:
        f = prefix / name
        try:
            with open(f, encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if line.startswith("["):
                        keys.add(f"{name}:{line[1:line.find(']')]}")
        except OSError:
            continue
    return keys


@dataclass
class Snapshot:
    prefix_files: dict = field(default_factory=dict)
    host_files: dict = field(default_factory=dict)
    keys: set = field(default_factory=set)


def take_snapshot(prefix: Path, skip: list[Path] | None = None, home: Path | None = None) -> Snapshot:
    skip_set = {Path(p) for p in (skip or [])}
    snap = Snapshot()
    _snap_tree(prefix, True, snap.prefix_files, skip_set)
    for path, rec in host_watch(home):
        _snap_tree(path, rec, snap.host_files, skip_set)
    snap.keys = registry_keys(prefix)
    return snap


_SKIP_DIRS = ("windows", "programdata/microsoft", "common files", "microsoft.net")
_JUNK = re.compile(
    r"(unins\d*|uninst\w*|vc_?redist\w*|vcredist\w*|dxsetup|dxwebsetup|directx\w*|dotnet\w*|ndp\d+\w*|"
    r"crash\w*|unitycrash\w*|redist\w*|oalinst|physx\w*|setup|installer|update\w*)\.exe$",
    re.IGNORECASE,
)


def find_game_executables(prefix: Path, new_files: list[str]) -> list[tuple[str, int]]:
    """Rank new .exe files in the prefix as candidates for the installed program.

    Returns (path, size) largest first. A guess from names and sizes only; nothing is read or run.
    """
    drive_c = str(prefix / "drive_c") + "/"
    out = []
    for f in new_files:
        if not f.lower().endswith(".exe") or not f.startswith(drive_c):
            continue
        rel = f[len(drive_c):].lower()
        padded = "/" + rel
        if any(f"/{d}/" in padded for d in _SKIP_DIRS) or "/temp/" in padded:
            continue
        if _JUNK.search(os.path.basename(f)):
            continue
        try:
            out.append((f, os.stat(f).st_size))
        except OSError:
            continue
    return sorted(out, key=lambda t: t[1], reverse=True)


@dataclass
class ChangeReport:
    prefix_new: list = field(default_factory=list)
    prefix_changed: list = field(default_factory=list)
    prefix_removed: list = field(default_factory=list)
    host_new: list = field(default_factory=list)
    host_changed: list = field(default_factory=list)
    host_removed: list = field(default_factory=list)
    keys_new: list = field(default_factory=list)
    executables: list = field(default_factory=list)  # (path, size), best guess first

    def describe_executables(self, prefix: Path | None = None) -> str:
        if not self.executables:
            return ("No new program found in the prefix. The installer may have put its files "
                    "somewhere else, or the run did not finish installing.")
        lines = ["Possible installed program(s), largest first (a guess from names and sizes):"]
        lines += [f"  {p}  ({n / 1e6:.1f} MB)" for p, n in self.executables[:10]]
        if prefix is not None:
            best = self.executables[0][0]
            lines.append(f'To run the first one yourself: WINEPREFIX="{prefix}" wine "{best}"')
        lines.append("gitfurl did not run it or add it to any launcher.")
        return "\n".join(lines)

    def host_touched(self) -> bool:
        return bool(self.host_new or self.host_changed or self.host_removed)

    def describe(self) -> str:
        def part(title, items):
            if not items:
                return []
            shown = items[:MAX_LISTED]
            more = [f"  ... and {len(items) - MAX_LISTED} more"] if len(items) > MAX_LISTED else []
            return [f"{title} ({len(items)}):"] + [f"  {i}" for i in shown] + more

        lines = ["Changes outside the prefix (on your system):" if self.host_touched()
                 else "No changes found outside the prefix in the places gitfurl watches."]
        lines += part("new", self.host_new) + part("changed", self.host_changed) \
            + part("removed", self.host_removed)
        lines.append("Inside the prefix: "
                     f"{len(self.prefix_new)} new, {len(self.prefix_changed)} changed, "
                     f"{len(self.prefix_removed)} removed files; "
                     f"{len(self.keys_new)} new registry keys.")
        lines += part("new registry keys", self.keys_new)
        lines.append("This is best effort: it names files and registry keys, not their contents, "
                     "and does not see the rest of the disk.")
        return "\n".join(lines)


def _diff(before: dict, after: dict) -> tuple[list, list, list]:
    new = sorted(k for k in after if k not in before)
    removed = sorted(k for k in before if k not in after)
    changed = sorted(k for k in after if k in before and after[k] != before[k])
    return new, changed, removed


def compare(before: Snapshot, after: Snapshot, prefix: Path | None = None) -> ChangeReport:
    r = ChangeReport()
    r.prefix_new, r.prefix_changed, r.prefix_removed = _diff(before.prefix_files, after.prefix_files)
    r.host_new, r.host_changed, r.host_removed = _diff(before.host_files, after.host_files)
    r.keys_new = sorted(after.keys - before.keys)
    if prefix is not None:
        r.executables = find_game_executables(prefix, r.prefix_new)
    return r
