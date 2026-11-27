"""Deleting a prefix gitfurl made, with the menu entries gitfurl wrote for it.

Only after an approval of a plan (`gitfurl cli prefix delete NAME`, or the library's "Delete
prefix..."). plan_delete_prefix() reads names and metadata only and refuses, with a plain
reason, anything that is not exactly prefixes_root()/NAME: a bad name, a link anywhere on the
path, not a folder, not a Wine prefix (neither drive_c nor system.reg), a Wine server for the
prefix still running, or a run on it in progress. The plan lists the folder with its size and
file count (counted without following links, up to MAX_COUNT entries) and every menu entry
it removes:

  $XDG_DATA_HOME/applications/gitfurl-<prefix>-*.desktop, a regular file (opened with
  O_NOFOLLOW) that carries desktop.MARKER and whose Exec line starts with
  `env WINEPREFIX=<this prefix's path> ` - the line setup_entries.shortcut_text() writes.

A look-alike that fails any of these (another prefix whose name starts the same, a file
without the marker, a link) is kept and listed with the reason. gitfurl.desktop (the window's
own entry) never matches.

perform_delete_prefix() checks everything again just before acting (the folder's device and
inode, no link on the path, no Wine server, no run) and then deletes the tree through file
descriptors: every folder is opened with O_NOFOLLOW relative to its parent's descriptor and
compared with what lstat saw, entries are removed with unlink/rmdir relative to that
descriptor, links are removed as links (what they point to is kept), and nothing on another
file system is entered. It stops at the first surprise and reports what was removed and what
is left. A log and a timeline of what was done are written to the run log folder (mode 0600).
"""

from __future__ import annotations

import fnmatch
import os
import stat
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gitfurl.prefixes import prefixes_root, validate_prefix_name

MAX_COUNT = 1_000_000  # entries counted for the plan; beyond that the plan says "at least"
MAX_DEPTH = 256  # folder depth the deletion goes into
MAX_MENU = 200  # menu-entry candidates looked at
READ_MAX = 1 << 20  # bytes of one menu entry read
SHOWN_ENTRIES = 30  # menu entries named in the window's text


class DeleteRefused(Exception):
    """Why the prefix is not deleted; nothing was changed."""


@dataclass(frozen=True)
class MenuEntry:
    path: Path
    ident: tuple[int, int, int, int]  # (st_dev, st_ino, st_size, st_mtime_ns) when planned


@dataclass
class DeletePlan:
    name: str
    path: Path
    ident: tuple[int, int]  # the folder's (st_dev, st_ino)
    files: int = 0  # regular files and other non-folders, links counted separately
    dirs: int = 0  # folders inside it
    links: int = 0
    size: int = 0  # bytes of the regular files (st_size; not followed)
    capped: bool = False  # the count stopped at MAX_COUNT entries
    menu: list[MenuEntry] = field(default_factory=list)
    kept: list[tuple[Path, str]] = field(default_factory=list)  # look-alikes kept, and why
    notes: list[str] = field(default_factory=list)
    env: dict[str, str] | None = None

    def describe(self) -> list[str]:
        from gitfurl.timeline import size_text

        least = "at least " if self.capped else ""
        out = [f"delete the prefix folder {self.path} and everything in it: {least}"
               f"{self.files} file(s), {self.dirs} folder(s), {least}{size_text(self.size)}"
               + (f"; {self.links} link(s) are removed as links, what they point to is kept"
                  if self.links else "")]
        if self.capped:
            out.append(f"(counting stopped after {MAX_COUNT} entries; the deletion goes on "
                       "to the end)")
        for e in self.menu:
            out.append(f"remove the menu entry {e.path} (gitfurl wrote it for this prefix)")
        if not self.menu:
            out.append("no menu entry of gitfurl's for this prefix was found")
        for p, why in self.kept:
            out.append(f"keep {p}: {why}")
        out += list(self.notes)
        out.append("This cannot be undone. Nothing else is deleted: no other prefix, not "
                   "~/.wine, not gitfurl.desktop. Everything is checked again just before "
                   "deleting; if anything changed, nothing is deleted. Links inside the "
                   "prefix are removed as links and never followed.")
        return out


# --- checks shared by the plan and the deletion --------------------------------------------

def _link_on_path(path: Path) -> str | None:
    """The first part of `path` (from the top) that is a link, or None. lstat only."""
    cur = Path(path.anchor)
    for part in path.parts[1:]:
        cur = cur / part
        try:
            if stat.S_ISLNK(os.lstat(cur).st_mode):
                return str(cur)
        except OSError:
            return None
    return None


def _target(name: str, env) -> Path:
    try:
        validate_prefix_name(name)
    except ValueError as exc:
        raise DeleteRefused(str(exc)) from None
    root = prefixes_root(env)
    if not root.is_absolute() or os.path.normpath(root) != str(root):
        raise DeleteRefused(f"the prefix folder {root} is not a plain absolute path")
    path = root / name
    if path.parent != root or path.name != name:
        raise DeleteRefused(f"{path} is not exactly {root}/{name}")
    return path


def in_use(prefix: Path, proc: str = "/proc", skip_pid: int | None = None) -> str | None:
    """Why a process seems to run in or use `prefix`, or None. Read-only, own processes only:
    WINEPREFIX set to it, a working folder inside it, or a file inside it held open."""
    want = str(prefix)
    want_env = f"WINEPREFIX={want}".encode()
    skip = os.getpid() if skip_pid is None else skip_pid
    try:
        entries = os.listdir(proc)
    except OSError:
        return None

    def inside(p: str) -> bool:
        return p == want or p.startswith(want + "/")

    for entry in entries:
        if not entry.isdigit() or int(entry) == skip:
            continue
        base = f"{proc}/{entry}"
        try:
            with open(f"{base}/environ", "rb") as fh:
                if want_env in fh.read(1 << 20).split(b"\0"):
                    return f"process {entry} runs with WINEPREFIX set to this prefix"
        except OSError:
            pass
        try:
            if inside(os.readlink(f"{base}/cwd")):
                return f"process {entry} works in a folder inside this prefix"
        except OSError:
            pass
        try:
            fds = os.listdir(f"{base}/fd")
        except OSError:
            continue
        for fd in fds[:4096]:
            try:
                if inside(os.readlink(f"{base}/fd/{fd}")):
                    return f"process {entry} holds a file inside this prefix open"
            except OSError:
                continue
    return None


def _checks(name: str, env, busy: str | None, running, using) -> tuple[Path, os.stat_result]:
    """The refusals, in order. Returns (path, its lstat)."""
    path = _target(name, env)
    link = _link_on_path(path)
    if link is not None:
        raise DeleteRefused(f"{link} is a link; gitfurl deletes a prefix only through a path "
                            "without links")
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        raise DeleteRefused(f"there is no prefix {name} ({path})") from None
    except OSError as exc:
        raise DeleteRefused(f"{path} could not be read ({exc.strerror})") from None
    if not stat.S_ISDIR(st.st_mode):
        raise DeleteRefused(f"{path} is not a folder")
    drive_c = _lstat_or_none(path / "drive_c")
    system = _lstat_or_none(path / "system.reg")
    if not ((drive_c and stat.S_ISDIR(drive_c.st_mode))
            or (system and stat.S_ISREG(system.st_mode))):
        raise DeleteRefused(f"{path} does not look like a Wine prefix (it has neither drive_c "
                            "nor system.reg); delete it yourself if you are sure")
    if busy:
        raise DeleteRefused(busy)
    why = running(path)
    if why is not None:
        raise DeleteRefused(f"a Wine server for this prefix is running ({why}); end what runs "
                            "in it first")
    why = using(path)
    if why is not None:
        raise DeleteRefused(f"a run on this prefix seems to be in progress ({why})")
    return path, st


def _lstat_or_none(p: Path):
    try:
        return os.lstat(p)
    except OSError:
        return None


def _defaults(running, using):
    if running is None:
        from gitfurl.procgroup import prefix_server_running as running
    return running, (in_use if using is None else using)


# --- the plan ------------------------------------------------------------------------------

def _count(plan: DeletePlan) -> None:
    """Count what the folder holds (lstat, links not followed, one file system)."""
    stack, seen = [plan.path], 0
    dev = plan.ident[0]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError as exc:
            raise DeleteRefused(f"{d} could not be read ({exc.strerror})") from None
        with it:
            for e in it:
                seen += 1
                if seen > MAX_COUNT:
                    plan.capped = True
                    return
                try:
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    plan.files += 1
                    continue
                if stat.S_ISLNK(st.st_mode):
                    plan.links += 1
                elif stat.S_ISDIR(st.st_mode):
                    if st.st_dev != dev:
                        raise DeleteRefused(f"{e.path} is another file system mounted inside "
                                            "the prefix; unmount it first")
                    plan.dirs += 1
                    stack.append(Path(e.path))
                else:
                    plan.files += 1
                    if stat.S_ISREG(st.st_mode):
                        plan.size += st.st_size


def _read_entry(path: Path) -> tuple[bytes, os.stat_result]:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise DeleteRefused("it is not a regular file")
        return fh.read(READ_MAX), st


def exec_start(prefix: Path) -> str:
    """How the Exec line of an entry gitfurl wrote for `prefix` starts (with the space)."""
    from gitfurl.desktop import _exec_quote

    return "Exec=env " + _exec_quote(f"WINEPREFIX={prefix}") + " "


def entry_reason(data: bytes, prefix: Path) -> str | None:
    """None when the entry's text says gitfurl wrote it for `prefix`, else why it is kept."""
    from gitfurl.desktop import MARKER

    lines = data.decode("utf-8", errors="replace").splitlines()
    if MARKER not in lines:
        return "it does not carry gitfurl's marker line, so gitfurl did not write it"
    execs = [ln for ln in lines if ln.startswith("Exec=")]
    if not execs:
        return "it has no Exec line"
    from gitfurl.desktop import DesktopRefused

    try:
        start = exec_start(prefix)
    except DesktopRefused:
        return "this prefix's path cannot appear in a menu entry"
    if not execs[0].startswith(start):
        return "its program does not start in this prefix (another WINEPREFIX)"
    return None


def _ident(st: os.stat_result) -> tuple[int, int, int, int]:
    return (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)


def _plan_menu(plan: DeletePlan) -> None:
    from gitfurl.desktop import DesktopRefused, data_home

    try:
        apps = data_home(plan.env) / "applications"
    except DesktopRefused as exc:
        plan.notes.append(f"menu entries were not looked for: {exc}")
        return
    try:
        names = sorted(os.listdir(apps))
    except FileNotFoundError:
        return
    except OSError as exc:
        plan.notes.append(f"menu entries were not looked for: {apps} could not be read "
                          f"({exc.strerror})")
        return
    pattern = f"gitfurl-{plan.name}-*.desktop"
    found = [n for n in names if fnmatch.fnmatchcase(n, pattern)]
    if len(found) > MAX_MENU:
        plan.notes.append(f"only the first {MAX_MENU} of {len(found)} menu entries named "
                          f"{pattern} were looked at")
        found = found[:MAX_MENU]
    for n in found:
        p = apps / n
        try:
            data, st = _read_entry(p)
        except DeleteRefused as exc:
            plan.kept.append((p, str(exc)))
            continue
        except OSError as exc:
            plan.kept.append((p, f"it could not be opened safely ({exc.strerror}); it may "
                                 "be a link"))
            continue
        why = entry_reason(data, plan.path)
        if why is None:
            plan.menu.append(MenuEntry(p, _ident(st)))
        else:
            plan.kept.append((p, why))


MOUNTINFO = Path("/proc/self/mountinfo")


def _unescape_mount(field: str) -> str:
    """Mount points in mountinfo escape space, tab, newline and backslash as \\ooo."""
    out, i = [], 0
    while i < len(field):
        if field[i] == "\\" and i + 3 < len(field) and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return "".join(out)


def mounts_inside(path: Path, mountinfo: Path | None = None) -> list[str]:
    """Mount points strictly inside `path`, from the mount table. A bind mount of the same
    file system has the same device number, so the walk alone cannot see it; this does.
    Raises DeleteRefused when the table cannot be read (deleting without it is not safe)."""
    src = MOUNTINFO if mountinfo is None else mountinfo
    try:
        text = src.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as exc:
        raise DeleteRefused(f"the mount table ({src}) could not be read ({exc.strerror}); "
                            "without it a mount inside the prefix cannot be ruled out") from None
    base = str(path).rstrip("/") + "/"
    found = []
    for line in text.splitlines():
        fields = line.split(" ")
        if len(fields) < 5:
            continue
        point = _unescape_mount(fields[4])
        if point.startswith(base):
            found.append(point)
    return found


def _no_mounts(path: Path, mountinfo: Path | None) -> None:
    inside = mounts_inside(path, mountinfo)
    if inside:
        shown = ", ".join(inside[:3]) + (f" and {len(inside) - 3} more" if len(inside) > 3 else "")
        raise DeleteRefused(f"something is mounted inside the prefix ({shown}); unmount it "
                            "first")


def plan_delete_prefix(name: str, env: dict[str, str] | None = None, *, busy: str | None = None,
                       running=None, using=None, mountinfo: Path | None = None) -> DeletePlan:
    """What deleting the prefix would do. Reads names and metadata only; raises
    DeleteRefused with the reason when it is not safe. `busy` is a reason the caller already
    knows (the window's own run). `running(path)` and `using(path)` say why a Wine server
    runs for it or a process uses it (defaults: procgroup.prefix_server_running, in_use)."""
    running, using = _defaults(running, using)
    path, st = _checks(name, env, busy, running, using)
    _no_mounts(path, mountinfo)
    plan = DeletePlan(name, path, (st.st_dev, st.st_ino), env=env)
    _count(plan)
    _plan_menu(plan)
    return plan


# --- the deletion --------------------------------------------------------------------------

class _Stop(Exception):
    """Something changed during the deletion; it stops here."""


@dataclass
class DeleteResult:
    removed_files: int = 0  # files and links
    removed_dirs: int = 0
    folder_gone: bool = False
    menu_removed: list[Path] = field(default_factory=list)
    left: list[str] = field(default_factory=list)  # "what: why"
    log_path: Path | None = None
    timeline_path: Path | None = None

    def lines(self, plan: DeletePlan) -> list[str]:
        out = []
        if self.folder_gone:
            out.append(f"deleted the prefix folder {plan.path} ({self.removed_files} file(s) "
                       f"and link(s), {self.removed_dirs} folder(s))")
        elif self.removed_files or self.removed_dirs:
            out.append(f"the prefix folder {plan.path} was only partly deleted: "
                       f"{self.removed_files} file(s) and link(s), {self.removed_dirs} "
                       "folder(s) removed")
        else:
            out.append(f"nothing was deleted in {plan.path}")
        out += [f"removed the menu entry {p}" for p in self.menu_removed]
        out += [f"left: {w}" for w in self.left]
        return out

    def headline(self, plan: DeletePlan) -> str:
        if self.folder_gone and not self.left:
            n = len(self.menu_removed)
            return f"Deleted the prefix {plan.name}" + (
                f" and {n} menu entr{'y' if n == 1 else 'ies'}." if n else ".")
        if self.folder_gone:
            return f"Deleted the prefix {plan.name}; some things were left."
        return f"The prefix {plan.name} was not fully deleted."


_DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


def _open_path_nofollow(path: Path) -> int:
    """A descriptor for the folder `path`, opened part by part from / with O_NOFOLLOW."""
    fd = os.open("/", _DIR_FLAGS)
    try:
        for part in path.parts[1:]:
            nfd = os.open(part, _DIR_FLAGS, dir_fd=fd)
            os.close(fd)
            fd = nfd
    except OSError:
        os.close(fd)
        raise
    return fd


def _empty(dfd: int, where: Path, dev: int, depth: int, res: DeleteResult) -> None:
    """Remove everything in the folder open as `dfd`. Raises _Stop on a surprise."""
    if depth > MAX_DEPTH:
        raise _Stop(f"{where} is more than {MAX_DEPTH} folders deep")
    with os.scandir(dfd) as it:
        names = [e.name for e in it]
    for n in names:
        p = where / n
        try:
            st = os.stat(n, dir_fd=dfd, follow_symlinks=False)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise _Stop(f"{p} could not be read ({exc.strerror})") from None
        if stat.S_ISDIR(st.st_mode):
            if st.st_dev != dev:
                raise _Stop(f"{p} is on another file system; it is not entered")
            try:
                cfd = os.open(n, _DIR_FLAGS, dir_fd=dfd)
            except OSError as exc:
                raise _Stop(f"{p} could not be opened as a folder without following links "
                            f"({exc.strerror}); it may have been swapped for a link") from None
            try:
                cst = os.fstat(cfd)
                if (cst.st_dev, cst.st_ino) != (st.st_dev, st.st_ino):
                    raise _Stop(f"{p} changed while it was being deleted")
                _empty(cfd, p, dev, depth + 1, res)
            finally:
                os.close(cfd)
            try:
                os.rmdir(n, dir_fd=dfd)
            except OSError as exc:
                raise _Stop(f"{p} could not be removed ({exc.strerror}); something may have "
                            "been added meanwhile") from None
            res.removed_dirs += 1
        else:
            try:
                os.unlink(n, dir_fd=dfd)  # a link goes as a link
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise _Stop(f"{p} could not be removed ({exc.strerror})") from None
            res.removed_files += 1


def _remove_menu(plan: DeletePlan, res: DeleteResult) -> None:
    for e in plan.menu:
        try:
            data, st = _read_entry(e.path)
        except FileNotFoundError:
            res.left.append(f"menu entry {e.path}: it was already gone")
            continue
        except (DeleteRefused, OSError) as exc:
            why = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            res.left.append(f"menu entry {e.path}: kept, it could not be opened safely ({why})")
            continue
        if _ident(st) != e.ident or entry_reason(data, plan.path) is not None:
            res.left.append(f"menu entry {e.path}: kept, it changed after the plan")
            continue
        try:
            dfd = _open_path_nofollow(e.path.parent)
        except OSError as exc:
            res.left.append(f"menu entry {e.path}: kept, its folder could not be opened "
                            f"without following links ({exc.strerror})")
            continue
        try:
            now = os.stat(e.path.name, dir_fd=dfd, follow_symlinks=False)
            if _ident(now) != e.ident or not stat.S_ISREG(now.st_mode):
                res.left.append(f"menu entry {e.path}: kept, it changed after the plan")
                continue
            os.unlink(e.path.name, dir_fd=dfd)
            res.menu_removed.append(e.path)
        except OSError as exc:
            res.left.append(f"menu entry {e.path}: {exc.strerror or exc}")
        finally:
            os.close(dfd)


def _write_log(plan: DeletePlan, res: DeleteResult, log_dir: Path, stamp: str,
               how: str) -> None:
    try:
        log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        for n in range(1, 10):
            path = log_dir / (f"{plan.name}-{stamp}" + (f"-{n}" if n > 1 else "")
                              + ".delete.log")
            try:
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
                             | os.O_CLOEXEC, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(f"gitfurl: delete the prefix {plan.name} (approved: {how})\n")
                fh.write("plan:\n" + "".join(f"  {ln}\n" for ln in plan.describe()))
                fh.write("done:\n" + "".join(f"  {ln}\n" for ln in res.lines(plan)))
            res.log_path = path
            return
        res.left.append("the log could not be written (too many in one second)")
    except OSError as exc:
        res.left.append(f"the log could not be written ({exc.strerror or exc})")


def _timeline(plan: DeletePlan, res: DeleteResult, log_dir: Path, how: str) -> None:
    from gitfurl.timeline import Timeline, approval_entry, size_text

    tl = Timeline()
    tl.add("plan", f"Delete the prefix {plan.name}",
           f"{plan.files} file(s), {plan.dirs} folder(s), {size_text(plan.size)}; "
           f"{len(plan.menu)} menu entr{'y' if len(plan.menu) == 1 else 'ies'} to remove")
    approval_entry(tl, how)
    tl.add("prefix", "Prefix folder deleted" if res.folder_gone else
           "Prefix folder not fully deleted",
           f"{res.removed_files} file(s) and link(s), {res.removed_dirs} folder(s) removed; "
           "links were removed as links, never followed")
    if plan.menu:
        tl.add("cleanup", f"Menu entries: {len(res.menu_removed)} of {len(plan.menu)} removed",
               "only entries gitfurl wrote for this prefix")
    for w in res.left[:20]:
        tl.add("note", "Left", w)
    tl.save(log_dir, plan.name)
    tl.end(res.headline(plan))
    res.timeline_path = tl.path


def perform_delete_prefix(plan: DeletePlan, *, how: str = "typed", busy: str | None = None,
                          running=None, using=None, log_dir: Path | None = None,
                          mountinfo: Path | None = None) -> DeleteResult:
    """Carry out an approved plan. Raises DeleteRefused (nothing changed) when a check made
    again now fails; otherwise returns what was removed and what was left, with reasons.
    `how` is how it was approved, for the timeline ("typed", "yes" or "window")."""
    from gitfurl.runner import logs_root

    running, using = _defaults(running, using)
    path, st = _checks(plan.name, plan.env, busy, running, using)
    if path != plan.path or (st.st_dev, st.st_ino) != plan.ident:
        raise DeleteRefused(f"{path} is not the folder the plan was made for (it was replaced "
                            "meanwhile)")
    _no_mounts(path, mountinfo)
    res = DeleteResult()
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    try:
        parent = _open_path_nofollow(path.parent)
    except OSError as exc:
        raise DeleteRefused(f"{path.parent} could not be opened without following links "
                            f"({exc.strerror})") from None
    try:
        try:
            pfd = os.open(plan.name, _DIR_FLAGS, dir_fd=parent)
        except OSError as exc:
            raise DeleteRefused(f"{path} could not be opened without following links "
                                f"({exc.strerror})") from None
        try:
            pst = os.fstat(pfd)
            if (pst.st_dev, pst.st_ino) != plan.ident:
                raise DeleteRefused(f"{path} is not the folder the plan was made for")
            _empty(pfd, path, pst.st_dev, 0, res)
        except _Stop as exc:
            res.left.append(f"{path}: stopped: {exc}; the rest of the folder is kept")
        finally:
            os.close(pfd)
        if not res.left:
            try:
                now = os.stat(plan.name, dir_fd=parent, follow_symlinks=False)
                same = (now.st_dev, now.st_ino) == plan.ident
            except OSError:
                same = False
            if not same:
                res.left.append(f"{path}: kept, it was replaced while it was being emptied")
            else:
                try:
                    os.rmdir(plan.name, dir_fd=parent)
                    res.folder_gone = True
                except OSError as exc:
                    res.left.append(f"{path}: the emptied folder could not be removed "
                                    f"({exc.strerror}); something may have been added "
                                    "meanwhile")
    finally:
        os.close(parent)
    _remove_menu(plan, res)
    log_dir = logs_root(plan.env) if log_dir is None else Path(log_dir)
    _write_log(plan, res, log_dir, stamp, how)
    _timeline(plan, res, log_dir, how)
    return res


def window_text(plan: DeletePlan) -> str:
    """The window's approval text: the warning first, then the plan."""
    from gitfurl.timeline import size_text

    least = "at least " if plan.capped else ""
    lines = [f"Delete the prefix {plan.name}? This cannot be undone.", "",
             f"Folder: {plan.path}",
             f"Size: {least}{size_text(plan.size)} in {least}{plan.files} file(s) and "
             f"{plan.dirs} folder(s)"
             + (f"; {plan.links} link(s) are removed as links, what they point to is kept"
                if plan.links else ""), ""]
    if plan.menu:
        lines.append(f"Menu entries to remove ({len(plan.menu)}):")
        lines += [f"  {e.path.name}" for e in plan.menu[:SHOWN_ENTRIES]]
        if len(plan.menu) > SHOWN_ENTRIES:
            lines.append(f"  ... and {len(plan.menu) - SHOWN_ENTRIES} more")
    else:
        lines.append("Menu entries to remove: none")
    if plan.kept:
        lines.append(f"Kept ({len(plan.kept)}), not gitfurl's for this prefix:")
        lines += [f"  {p.name}: {why}" for p, why in plan.kept[:SHOWN_ENTRIES]]
        if len(plan.kept) > SHOWN_ENTRIES:
            lines.append(f"  ... and {len(plan.kept) - SHOWN_ENTRIES} more")
    lines += [*plan.notes, "",
              "Nothing else is deleted: no other prefix, not ~/.wine, not gitfurl's own menu "
              "entry. Everything is checked again just before deleting. A log is kept in "
              "gitfurl's log folder."]
    return "\n".join(lines)
