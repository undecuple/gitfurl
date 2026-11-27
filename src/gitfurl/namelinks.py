"""Link the archives under the names the setup expects (opt-in: --link-names, or the window's
switch).

An Inno Setup script names the archives it reads beside itself ([Files] external sources and
[Code] strings, see gitfurl.inno.beside_files). Sometimes the folder holds the same archives
under another base name (the script expects fg-01.bin, fg-02.bin; the folder has
other-01.bin, other-02.bin), so Setup cannot find them.

propose() is pure: it proposes a mapping only when it is unambiguous. The missing names form
one numbered series (same base, numbers 1 to N), and the folder holds exactly one other
numbered series of FreeArc archives with the same numbers; number maps to number. Anything
else gives no mapping, with the reason. A name that exists already is never mapped, and a
missing name with no counterpart (an .inf, say) stays missing.

propose_for() reads the installer's setup data and the folder's names for it (read-only).

A Setup run with a mapping is started from a staging folder under gitfurl's state folder
($XDG_STATE_HOME/gitfurl/staging/NAME, mode 0700): it holds a symbolic link for every file
and folder beside the installer, by its own name, plus the expected names pointing at the
mapped archives. The user's files are never renamed, moved or written. After the run,
whatever its outcome, Staging.remove() removes the links it made (each checked to be that
link, with that target) and deletes the folder; anything else in it is left, and the folder
is then kept. The native route needs no staging folder: it reads the archives directly.
"""

from __future__ import annotations

import os
import re
import stat
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path

from gitfurl.detect import clean

# Same pattern as gitfurl.inno._NUMBERED: base, number, extension ("fg-01.bin").
_NUMBERED = re.compile(r"^(.*?)(\d+)(\.[A-Za-z0-9]{1,8})$")

MAX_ENTRIES = 1000  # files and folders beside the installer that a staging folder may link
MAX_SHOWN = 6  # names shown in a reason before "..."


def _shown(names) -> str:
    names = list(names)
    return ", ".join(clean(n, 60) for n in names[:MAX_SHOWN]) + (
        ", ..." if len(names) > MAX_SHOWN else "")


def _series(names) -> tuple[dict[tuple[str, str], dict[int, str]], list[str], list[str]]:
    """(numbered series keyed by (base, extension) casefolded -> {number: name}, names that
    are not numbered, names whose number repeats within their series)."""
    groups: dict[tuple[str, str], dict[int, str]] = {}
    plain: list[str] = []
    repeated: list[str] = []
    for name in names:
        m = _NUMBERED.match(name)
        if not m:
            plain.append(name)
            continue
        stem, num, ext = m.groups()
        group = groups.setdefault((stem.casefold(), ext.casefold()), {})
        if int(num) in group:
            repeated.append(name)
        else:
            group[int(num)] = name
    return groups, plain, repeated


def _range_text(group: dict[int, str]) -> str:
    nums = sorted(group)
    if len(nums) == 1:
        return clean(group[nums[0]], 60)
    return f"{clean(group[nums[0]], 60)} to {clean(group[nums[-1]], 60)}"


@dataclass(frozen=True)
class LinkProposal:
    """The names to link, or why there are none."""
    links: tuple[tuple[str, str], ...] = ()  # (name the setup expects, archive in the folder)
    reason: str = ""  # with links: what was matched; without: why nothing is proposed
    unmatched: tuple[str, ...] = ()  # expected and missing, with no counterpart

    @property
    def ok(self) -> bool:
        return bool(self.links)

    def mapped_archives(self) -> set[str]:
        """Casefolded names of the archives that stand for an expected name."""
        return {target.casefold() for _name, target in self.links}

    def stands_for(self) -> str:
        """'other-01.bin stands for fg-01.bin, other-02.bin for fg-02.bin'."""
        parts = [f"{clean(t, 60)} stands for {clean(n, 60)}" if i == 0
                 else f"{clean(t, 60)} for {clean(n, 60)}"
                 for i, (n, t) in enumerate(self.links)]
        return ", ".join(parts)

    def lines(self) -> list[str]:
        if not self.ok:
            out = [f"link names: no mapping ({self.reason})"]
        else:
            out = [f"link names: {self.reason}", f"  names: {self.stands_for()}"]
        if self.unmatched:
            out.append("  still missing, no counterpart in the folder (nothing is made up "
                       f"for it): {_shown(self.unmatched)}")
        return out


def propose(missing, archives, named=(), present=()) -> LinkProposal:
    """Propose links from the names the setup expects to archives in the folder. Pure.

    missing: names the setup expects directly beside the installer that are not there.
    archives: names of the FreeArc archives in the folder (the installer itself left out).
    named: names in the folder that the setup names (used as they are, never mapped).
    present: every name in the folder; an expected name that is one of them is never mapped.
    Names are compared case-insensitively, as Windows does.
    """
    seen: set[str] = set()
    wanted: list[str] = []
    for name in missing:
        if "\\" in name or "/" in name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        wanted.append(name)
    taken = {n.casefold() for n in present}
    wanted = [n for n in wanted if n.casefold() not in taken]

    def none(reason: str) -> LinkProposal:
        return LinkProposal((), reason, tuple(wanted))

    if not wanted:
        return none("the setup names no missing file directly beside it")
    groups, plain, repeated = _series(wanted)
    if not groups:
        return none(f"no missing name is numbered ({_shown(plain)})")
    if len(groups) > 1:
        return none("the missing names form more than one numbered series ("
                    + "; ".join(_range_text(g) for g in groups.values()) + ")")
    if repeated:
        return none(f"a number repeats among the missing names ({_shown(repeated)})")
    (expect,) = groups.values()
    n = len(expect)
    if sorted(expect) != list(range(1, n + 1)):
        return none(f"the missing names ({_shown(expect[k] for k in sorted(expect))}) are not "
                    f"numbered 1 to {n}")
    used = {x.casefold() for x in named}
    others = [a for a in archives if a.casefold() not in used]
    if not others:
        return none("the folder holds no FreeArc archive the setup does not already name")
    found, loose, twice = _series(others)
    if loose:
        return none(f"the folder holds FreeArc archives that are not numbered ({_shown(loose)}),"
                    " so gitfurl cannot tell which archive stands for which name")
    if twice:
        return none(f"a number repeats among the FreeArc archives in the folder "
                    f"({_shown(twice)})")
    if len(found) > 1:
        return none("the folder holds more than one numbered series of FreeArc archives ("
                    + "; ".join(_range_text(g) for g in found.values()) + ")")
    (have,) = found.values()
    if sorted(have) != sorted(expect):
        return none(f"the setup expects {n} numbered archive{'s' if n != 1 else ''} "
                    f"({_range_text(expect)}), but the folder's series is "
                    f"{len(have)} ({_shown(have[k] for k in sorted(have))})")
    links = tuple((expect[k], have[k]) for k in sorted(expect))
    rest = tuple(w for w in wanted if w.casefold() not in {e.casefold() for e, _ in links})
    reason = (f"the setup expects {_range_text(expect)} ({n} missing); the folder holds one other "
              f"numbered series of FreeArc archives with the same numbers, "
              f"{_range_text(have)}")
    return LinkProposal(links, reason, rest)


def _folder_names(folder: Path) -> list[str]:
    from gitfurl.inno import MAX_DIR_ENTRIES

    names: list[str] = []
    with os.scandir(folder) as it:
        for k, e in enumerate(it):
            if k >= MAX_DIR_ENTRIES:
                break
            names.append(e.name)
    return names


def propose_for(installer: Path) -> LinkProposal:
    """propose() for an installer: the names its setup script expects beside it (from
    gitfurl.inno.beside_files) and the FreeArc archives in its folder. Reads the installer's
    setup data, the folder's names and the first 4 bytes of the files in it; writes nothing.
    Never raises for a file or folder it cannot read."""
    from gitfurl import inno
    from gitfurl.extract import ExtractRefused, find_archives

    installer = Path(installer)
    try:
        setup = inno.read_setup(installer)
    except Exception:  # noqa: BLE001 - a hostile file must not stop the plan
        return LinkProposal((), "the setup script could not be read for the names it uses")
    folder = installer.parent
    try:
        beside = inno.beside_files(setup, folder)
        named = inno.named_beside(setup, folder)
    except Exception:  # noqa: BLE001
        return LinkProposal((), "the setup script could not be read for the names it uses")
    missing = [b.name for b in beside if b.checked and not b.present and "\\" not in b.name]
    try:
        paths, _notes = find_archives(installer)
        present = _folder_names(folder)
    except (ExtractRefused, OSError) as exc:
        return LinkProposal((), f"the installer's folder could not be read ({exc})",
                            tuple(missing))
    archives = [p.name for p in paths if p.name != installer.name]
    return propose(missing, archives, named, present)


# --- the staging folder for a Setup run -----------------------------------------------------

def staging_root(env: dict[str, str] | None = None) -> Path:
    """$XDG_STATE_HOME/gitfurl/staging (or ~/.local/state/gitfurl/staging), beside the logs."""
    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME", "~")).expanduser()
                                            / ".local/state")
    return Path(base) / "gitfurl" / "staging"


class StagingError(RuntimeError):
    """The staging folder could not be made; Setup was not started."""


@dataclass
class Staging:
    """A planned staging folder: where it goes and every link it holds."""
    folder: Path  # created when the run starts; must not exist before
    installer: Path  # the original installer
    links: tuple[tuple[str, Path], ...]  # (link name, target), in the order they are made
    proposal: LinkProposal
    made: list[tuple[str, Path]] | None = field(default=None, repr=False)  # links made

    @property
    def staged_installer(self) -> Path:
        return self.folder / self.installer.name

    def describe(self) -> list[str]:
        mapped = {n.casefold() for n, _t in self.proposal.links}
        out = self.proposal.lines()
        out.append(f"staging folder: {self.folder} (new, mode 0700, made when the run starts; "
                   "Setup is started from it, so it finds the names it expects)")
        out.append(f"  {len(self.links)} symbolic links in it (link name -> target):")
        for name, target in self.links:
            tag = "   (a name the setup expects)" if name.casefold() in mapped else ""
            out.append(f"  {clean(name, 120)} -> {clean(str(target), 300)}{tag}")
        out.append("  your files are not renamed, moved or written by gitfurl; after the run, "
                   "whatever its outcome (also Stop or Ctrl-C), gitfurl removes the links it "
                   "made and deletes the folder (anything else found in it is left, and the "
                   "folder is then kept)")
        return out

    def create(self) -> None:
        """Make the folder (mode 0700) and its links. On any failure, what was made is removed
        again and StagingError is raised. Nothing outside the folder is written."""
        root = self.folder.parent
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            st = os.lstat(root)
        except OSError as exc:
            raise StagingError(f"the staging folder's parent {root} could not be made "
                               f"({exc.strerror or exc}); Setup was not started") from exc
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise StagingError(f"{root} is not a folder of yours (or is a symbolic link); "
                               "Setup was not started")
        try:
            os.mkdir(self.folder, 0o700)
        except OSError as exc:
            raise StagingError(f"the staging folder {self.folder} could not be made "
                               f"({exc.strerror or exc}); Setup was not started") from exc
        self.made = []
        try:
            fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            self.remove()
            raise StagingError(f"the staging folder {self.folder} could not be opened "
                               f"({exc.strerror or exc}); Setup was not started") from exc
        try:
            os.fchmod(fd, 0o700)
            for name, target in self.links:
                os.symlink(str(target), name, dir_fd=fd)
                self.made.append((name, target))
        except OSError as exc:
            os.close(fd)
            fd = -1
            self.remove()
            raise StagingError(f"a link in the staging folder could not be made "
                               f"({exc.strerror or exc}); what was made is removed again, and "
                               "Setup was not started") from exc
        finally:
            if fd >= 0:
                os.close(fd)

    def remove(self) -> list[str]:
        """Remove the links this run made and delete the folder. Only links it made, with the
        target it gave them, are removed; nothing is followed. Returns lines for the user.
        Does nothing (returns []) when create() did not make the folder."""
        if self.made is None:
            return []
        made, self.made = self.made, None
        try:
            fd = os.open(self.folder, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except OSError as exc:
            return [f"staging folder: {self.folder} could not be opened to remove its links "
                    f"({exc.strerror or exc}); it is left as it is"]
        left: list[str] = []
        try:
            for name, target in made:
                try:
                    st = os.lstat(name, dir_fd=fd)
                    if stat.S_ISLNK(st.st_mode) and os.readlink(name, dir_fd=fd) == str(target):
                        os.unlink(name, dir_fd=fd)
                    else:
                        left.append(name)
                except FileNotFoundError:
                    continue
                except OSError:
                    left.append(name)
        finally:
            os.close(fd)
        try:
            os.rmdir(self.folder)
        except OSError:
            what = (f"links that changed ({_shown(left)})" if left else
                    "files gitfurl did not make (Setup may have written them)")
            return [f"staging folder: kept {self.folder}: it holds {what}; look, then delete "
                    "it yourself"]
        return [f"staging folder: its {len(made)} links were removed and {self.folder} was "
                "deleted; your files were not touched"]


def plan_staging(installer: Path, proposal: LinkProposal, prefix_name: str,
                 env: dict[str, str] | None = None, now: datetime | None = None) -> Staging:
    """The staging folder for a Setup run with `proposal` (which must have links). Reads the
    installer's folder's names only. Raises ValueError when it cannot be planned safely."""
    if not proposal.ok:
        raise ValueError("there are no names to link")
    installer = Path(installer)
    source = installer.parent
    entries: list[str] = []
    try:
        with os.scandir(source) as it:
            for e in it:
                try:
                    if not (e.is_file() or e.is_dir()):  # as Wine sees them: links followed
                        continue
                except OSError:
                    continue
                entries.append(e.name)
                if len(entries) > MAX_ENTRIES:
                    raise ValueError(f"the installer's folder holds more than {MAX_ENTRIES} "
                                     "files and folders; --link-names does not stage that many")
    except OSError as exc:
        raise ValueError(f"the installer's folder could not be read ({exc.strerror or exc})") \
            from exc
    entries.sort(key=lambda n: (n != installer.name, n.casefold()))
    taken = {n.casefold() for n in entries}
    links = [(name, source / name) for name in entries]
    for name, target in proposal.links:
        if name.casefold() in taken:  # never shadow a file that is there
            raise ValueError(f"{clean(name, 60)} is already in the installer's folder; it is "
                             "not linked")
        if target.casefold() not in taken:
            raise ValueError(f"{clean(target, 60)} is no longer in the installer's folder")
        taken.add(name.casefold())
        links.append((name, source / target))
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    folder = staging_root(env) / f"{prefix_name}-{stamp}-{os.getpid()}"
    return Staging(folder, installer, tuple(links), proposal)


def staged_plan(plan, staging: Staging):
    """The runner.InstallPlan started from the staging folder: the installer path and the
    working folder inside it; `source` keeps the original installer (progress reads it)."""
    return replace(plan, argv=[plan.argv[0], str(staging.staged_installer), *plan.argv[2:]],
                   cwd=staging.folder, source=staging.installer)


def run_staged(staging: Staging | None, fn, on_line=None):
    """Make the staging folder, call fn(), then always remove it (also on an error or Ctrl-C).
    on_line receives the removal lines. Returns fn()'s result."""
    if staging is None:
        return fn()
    staging.create()
    try:
        return fn()
    finally:
        for line in staging.remove():
            if on_line is not None:
                on_line(line)
