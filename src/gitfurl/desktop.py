"""An optional application-menu entry for the window (a freedesktop .desktop file).

Only on request (`gitfurl cli desktop-entry`), after an approval that shows the plan. It
writes two files in the user's own data folder, and creates the folders for them that are
missing (the plan lists them; --remove leaves folders): the entry
($XDG_DATA_HOME/applications/gitfurl.desktop) and a copy of the window's icon
($XDG_DATA_HOME/icons/hicolor/64x64/apps/gitfurl.png). No process is started (the menu
picks the entry up by itself; gitfurl does not run update-desktop-database). A file is
replaced or removed only when gitfurl wrote it: the entry carries MARKER, and the icon must
hold gitfurl's icon or sit beside gitfurl's entry. A link in place of the entry or the icon
is refused; folders on the way (a linked ~/.local/share/applications) are used as they are.
"""

from __future__ import annotations

import os
import shutil
import stat
import sys
from dataclasses import dataclass, field
from pathlib import Path

ENTRY_NAME = "gitfurl.desktop"
ICON_NAME = "gitfurl.png"
MARKER = "X-Gitfurl-Generated=true"
PACKAGE_ICON = Path(__file__).resolve().parent / "icon.png"
TEMP_TAG = ".gitfurl-"  # temporary files are ".<name>.gitfurl-<pid>"


class DesktopRefused(Exception):
    """Why the entry cannot be written or removed; nothing was changed."""


def data_home(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME", "").strip()
    if base and Path(base).is_absolute():
        return Path(base)
    home = env.get("HOME", "").strip()
    if not home:
        raise DesktopRefused("HOME is not set, so the user's data folder is not known")
    return Path(home) / ".local" / "share"


def launch_argv(env: dict[str, str] | None = None) -> tuple[list[str], str | None]:
    """(command, working folder or None) the menu entry runs: the built program itself, else
    the `gitfurl` on PATH, else this Python with `-m gitfurl` run from the folder that holds
    the package (so a source checkout works too). Absolute paths; links are kept as they are
    (a virtual environment's python is a link that must stay one)."""
    env = os.environ if env is None else env
    if getattr(sys, "frozen", False):  # a PyInstaller build
        return [os.path.abspath(sys.executable)], None
    found = shutil.which("gitfurl", path=env.get("PATH"))
    if found:
        return [os.path.abspath(found)], None
    return ([os.path.abspath(sys.executable), "-m", "gitfurl"],
            str(Path(__file__).resolve().parent.parent))


def _exec_quote(arg: str) -> str:
    """One argument for the Exec key, as written in the file. The Desktop Entry spec escapes
    twice: an argument with a reserved character is quoted, with `"`, `` ` ``, `$` and `\\`
    escaped by a backslash inside the quotes, and `%` is doubled; then, because Exec is a
    string value, every backslash is written twice."""
    if any(c in arg for c in "\n\r\t") or not arg:
        raise DesktopRefused("the program path holds a character a menu entry cannot carry")
    arg = arg.replace("%", "%%")
    if any(c in arg for c in ' "\'\\><~|&;$*?#()`'):
        quoted = '"' + "".join("\\" + c if c in '"`$\\' else c for c in arg) + '"'
        return quoted.replace("\\", "\\\\")
    return arg


def _path_value(path: str) -> str:
    if any(c in path for c in "\n\r"):
        raise DesktopRefused("the program folder holds a character a menu entry cannot carry")
    return path.replace("\\", "\\\\")


def entry_text(argv: list[str], workdir: str | None = None) -> str:
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=gitfurl",
        "Comment=Run a Windows installer you already have in a dedicated Wine prefix",
        "Exec=" + " ".join(_exec_quote(a) for a in argv),
    ]
    if workdir:
        lines.append("Path=" + _path_value(workdir))
    lines += [
        "Icon=gitfurl",
        "StartupWMClass=Gitfurl",
        "Terminal=false",
        "Categories=Utility;",
        MARKER,
        "",
    ]
    return "\n".join(lines)


def _missing_dirs(path: Path) -> list[Path]:
    """The folders above `path` that do not exist yet, the topmost first."""
    out: list[Path] = []
    cur = path.parent
    while not os.path.lexists(cur) and cur != cur.parent:
        out.append(cur)
        cur = cur.parent
    return list(reversed(out))


@dataclass
class DesktopPlan:
    entry: Path
    icon: Path
    argv: list[str]
    workdir: str | None = None
    remove: bool = False
    new_dirs: list[Path] = field(default_factory=list)

    def describe(self) -> list[str]:
        if self.remove:
            return [f"remove the menu entry {self.entry} (only if gitfurl wrote it)",
                    f"remove the icon {self.icon} (only if it is gitfurl's own icon or sits "
                    "beside gitfurl's entry)",
                    "remove gitfurl's own temporary files left beside them by an interrupted "
                    "run, if any",
                    "Folders are left. Nothing else is changed; no process is started."]
        out = [f"create the folder {d}" for d in self.new_dirs]
        verb = "replace gitfurl's menu entry" if os.path.lexists(self.entry) else \
            "write the menu entry"
        out.append(f"{verb} {self.entry} (mode 0644) with: Exec="
                   + " ".join(_exec_quote(a) for a in self.argv)
                   + (f" and Path={self.workdir}" if self.workdir else ""))
        verb = "replace gitfurl's icon at" if os.path.lexists(self.icon) else \
            "copy gitfurl's icon to"
        out.append(f"{verb} {self.icon} (mode 0644)")
        out.append("Nothing else is changed; no process is started. The menu may need a "
                   "moment, or a new login, to show it. Remove both files with "
                   "'gitfurl cli desktop-entry --remove' (folders are left).")
        return out


def plan_desktop(env: dict[str, str] | None = None, remove: bool = False) -> DesktopPlan:
    base = data_home(env)
    entry = base / "applications" / ENTRY_NAME
    icon = base / "icons" / "hicolor" / "64x64" / "apps" / ICON_NAME
    argv, workdir = launch_argv(env)
    new_dirs: list[Path] = []
    if not remove:
        for d in _missing_dirs(entry) + _missing_dirs(icon):
            if d not in new_dirs:
                new_dirs.append(d)
    return DesktopPlan(entry, icon, argv, workdir, remove, new_dirs)


def _read_nofollow(path: Path) -> bytes | None:
    """The file's bytes, None when it does not exist. Refuses a link or a non-regular file."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise DesktopRefused(f"{path} could not be read safely ({exc.strerror}); it may be "
                             "a link") from exc
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise DesktopRefused(f"{path} is not a regular file")
        return fh.read(1 << 20)


def _ours_entry(data: bytes | None) -> bool:
    return data is not None and MARKER.encode() in data.splitlines()


def check(plan: DesktopPlan) -> None:
    """Raise DesktopRefused when a file in the way was not written by gitfurl."""
    entry = _read_nofollow(plan.entry)
    if entry is not None and not _ours_entry(entry):
        raise DesktopRefused(f"{plan.entry} exists and was not written by gitfurl; it is "
                             "left as it is")
    icon = _read_nofollow(plan.icon)
    if (icon is not None and icon != PACKAGE_ICON.read_bytes() and not _ours_entry(entry)
            and not plan.remove):
        raise DesktopRefused(f"{plan.icon} exists and is not gitfurl's icon; it is left as it is")


def _write(path: Path, data: bytes) -> None:
    """Write via a new file in the same folder, then rename over the target."""
    tmp = path.with_name(f".{path.name}{TEMP_TAG}{os.getpid()}")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _leftovers(target: Path) -> list[Path]:
    """gitfurl's own temporary files beside `target` (regular files, not links)."""
    out = []
    try:
        names = os.listdir(target.parent)
    except OSError:
        return out
    prefix = f".{target.name}{TEMP_TAG}"
    for name in names:
        rest = name[len(prefix):]
        if name.startswith(prefix) and rest.isdigit():
            p = target.parent / name
            if stat.S_ISREG(os.lstat(p).st_mode):
                out.append(p)
    return out


def apply(plan: DesktopPlan, done: list[str] | None = None) -> list[str]:
    """Carry out an approved plan. Appends what was done to `done` as it goes (so a caller
    can report it when a later step fails) and returns it. Raises DesktopRefused or
    OSError."""
    done = [] if done is None else done
    check(plan)
    if plan.remove:
        entry_ours = _ours_entry(_read_nofollow(plan.entry))
        if entry_ours:
            plan.entry.unlink()
            done.append(f"removed {plan.entry}")
        else:
            done.append(f"no menu entry of gitfurl's at {plan.entry}")
        icon = _read_nofollow(plan.icon)
        if icon is not None and (icon == PACKAGE_ICON.read_bytes() or entry_ours):
            plan.icon.unlink()
            done.append(f"removed {plan.icon}")
        elif icon is not None:
            done.append(f"left {plan.icon}: it is not gitfurl's icon")
        for left in _leftovers(plan.entry) + _leftovers(plan.icon):
            left.unlink()
            done.append(f"removed a leftover temporary file {left}")
        return done
    for d in plan.new_dirs:
        os.mkdir(d, 0o755)
        done.append(f"created {d}")
    for target in (plan.entry, plan.icon):
        if not target.parent.is_dir():
            raise DesktopRefused(f"{target.parent} is not a folder")
    _write(plan.icon, PACKAGE_ICON.read_bytes())
    done.append(f"wrote {plan.icon}")
    _write(plan.entry, entry_text(plan.argv, plan.workdir).encode("utf-8"))
    done.append(f"wrote {plan.entry}")
    return done
