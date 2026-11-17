"""Optional removal of the original installer after a successful run.

Off by default. Nothing is deleted unless the installer exited with status 0 and the user
asked for it. Symlinks are removed as links, never followed. The list of what would be
deleted is built first so it can be shown for approval.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

MODES = ("none", "installer", "folder")
MAX_ENTRIES = 200
USER_DIRS = ("Downloads", "Desktop", "Documents", "Music", "Pictures", "Videos", "Public",
             "Templates", "Games", "Projects")
SYSTEM_DIRS = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc", "/nix", "/var", "/opt",
               "/boot", "/proc", "/sys", "/dev", "/run")


@dataclass
class CleanupPlan:
    mode: str
    files: list[Path] = field(default_factory=list)
    dirs: list[Path] = field(default_factory=list)  # removed only if empty, deepest first

    def describe(self) -> str:
        if self.mode == "none" or not (self.files or self.dirs):
            return "nothing will be deleted"
        lines = [f"will delete {len(self.files)} file(s):"]
        lines += [f"  {p}" for p in self.files[:20]]
        if len(self.files) > 20:
            lines.append(f"  ... and {len(self.files) - 20} more")
        if self.dirs:
            lines.append(f"and remove {len(self.dirs)} folder(s) once empty")
        return "\n".join(lines)


def _unsafe_folder(folder: Path, protect: list[Path]) -> str | None:
    home = Path(os.path.expanduser("~")).resolve()
    if folder == Path(folder.anchor) or folder == home or folder in home.parents:
        return f"refusing to delete a whole folder that is {folder}"
    for name in USER_DIRS:
        if folder == home / name:
            return (f"refusing to delete the whole of {folder}: it is a general-purpose folder. "
                    "Put the installer in its own folder first, or use the 'installer' mode")
    if folder.is_mount():
        return f"refusing to delete a mount point: {folder}"
    for p in protect:
        p = p.resolve()
        if p == folder or folder in p.parents:
            return f"refusing: {folder} contains {p}, which gitfurl needs"
    return None


def plan_cleanup(installer: Path, mode: str, protect: list[Path] | None = None) -> CleanupPlan:
    """Build the list of what mode would delete. Raises ValueError when it is not safe."""
    if mode not in MODES:
        raise ValueError(f"unknown cleanup mode: {mode}")
    plan = CleanupPlan(mode)
    if mode == "none":
        return plan
    installer = installer.resolve()
    for sysdir in SYSTEM_DIRS:
        if installer == Path(sysdir) or Path(sysdir) in installer.parents:
            raise ValueError(f"refusing to delete anything under {sysdir}")
    if mode == "installer":
        plan.files = [installer]
        return plan
    folder = installer.parent
    problem = _unsafe_folder(folder, protect or [])
    if problem:
        raise ValueError(problem)
    for root, dirs, files in os.walk(folder, followlinks=False):
        for name in files:
            plan.files.append(Path(root, name))
        for name in dirs:
            path = Path(root, name)
            (plan.files if path.is_symlink() else plan.dirs).append(path)
        if len(plan.files) + len(plan.dirs) > MAX_ENTRIES:
            raise ValueError(f"refusing: {folder} holds more than {MAX_ENTRIES} entries; "
                             "delete it yourself")
    plan.dirs = sorted((d for d in plan.dirs if not d.is_symlink()),
                       key=lambda p: len(p.parts), reverse=True) + [folder]
    return plan


def perform_cleanup(plan: CleanupPlan, status: int) -> tuple[int, list[str]]:
    """Delete per the plan only when status is 0. Returns (files deleted, problems)."""
    if status != 0 or plan.mode == "none":
        return 0, []
    deleted, problems = 0, []
    for path in plan.files:
        try:
            path.unlink()
            deleted += 1
        except OSError as exc:
            problems.append(f"{path}: {exc.strerror or exc}")
    for d in plan.dirs:
        try:
            d.rmdir()
        except OSError:
            pass  # not empty or already gone; leave it
    return deleted, problems
