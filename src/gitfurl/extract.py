"""Native extraction of FreeArc archives, without running the installer.

Methods decoded: storing, lzma, exe, x86, delta, rep, 4x4 (with lzma inside), srep, rolz and
the cm stage (with gitfurl's optional native module), alone or chained (farc_codecs.py,
farc_rolz.py).

Off unless asked for, per run, after an approval that shows the plan. gitfurl reads each
FreeArc archive beside the installer itself (gitfurl.farc) and writes the files into a new
folder in the prefix, C:\\Games\\<prefix> (the folder the unattended install uses) unless
another folder on the prefix's drive C: was chosen (--dest; parse_destination has the rules).
The installer is not run: no registry entries, no redistributables, no shortcuts and none of its
own steps, so the result may not work.

Refused before anything is written: no FreeArc archive found; an archive gitfurl cannot read;
any data block whose method is not a chain of those (including encryption); a
name that is not a safe relative path; a name Windows cannot use (a trailing dot or space, a
character from <>:"|?*, or a device name such as CON or NUL); two files, or a file and a
folder, whose names would be the same with letter case ignored (as Windows does); more than
MAX_FOLDERS folders; too little free space (blocks and inodes); a destination folder that
already exists; a destination that is not a new folder below C:\\ on the prefix's own drive_c
(Wine's own folders refused, no link followed on the way: ExtractPlan.dest_problem); a prefix
that does not exist. Folders whose names differ only in letter case
are one folder, as on Windows: the first spelling is used.

While writing: every file is created new (O_CREAT|O_EXCL|O_NOFOLLOW) inside folders this
run created, opened relative to the destination's own file descriptor, so nothing outside it
is written and nothing is overwritten. Output per data block stops at the sum of its files'
declared sizes; every decoder's buffers are capped before the block is read, and damaged data
is reported in the result. Each file's CRC-32 is checked; a file that does not match is removed
again and named in the result (as extract-inno does with its checksums), unless the run was
explicitly asked to keep such files (--keep-bad-file-crc, per run): then it is kept and listed
with its stored and computed CRC-32. Sizes, the decoders' own checks and "nothing is
overwritten" apply either way.
File times are set through the open file. Nothing from an archive is run or passed to a
program. Stop (a cancel flag, or Ctrl-C) ends the extraction between chunks; files already
written stay, and nothing else is deleted (only files removed for a CRC-32 mismatch). At most _Tree.CACHE folders are held open at a time.
"""

from __future__ import annotations

import errno
import os
import re
import stat
import threading
import zlib
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from pathlib import Path

from gitfurl import farc, farc_codecs
from gitfurl.detect import clean
from gitfurl.prefixes import prefix_path, validate_prefix_name

MAX_ARCHIVES = 64  # FreeArc files beside the installer that are read
MAX_DIR_ENTRIES = 10_000  # directory entries looked at when searching for them
CHUNK = 1 << 20  # bytes read or decoded at a time (also how often Stop is checked)
NAME_MAX = 255  # bytes of one path part (Linux)
PATH_MAX = 4000  # bytes of one relative path
MAX_FOLDERS = 1_000_000  # distinct folders in one extraction (all archives together)
FILE = -1  # in plan_extract's name table: the name is a file

_WIN_BAD = re.compile(r'[<>:"|?*]')
_WIN_DEVICES = frozenset(["CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
                          *(f"LPT{i}" for i in range(1, 10))])

KEEP_BAD_CRC_WARNING = (
    "WARNING: files whose CRC-32 does not match the archive are KEPT (--keep-bad-file-crc). "
    "gitfurl cannot tell a damaged file from an archive with wrong checksums; use this only "
    "for an installer you trust.")
KEEP_BAD_CRC_SUGGESTION = (
    "If you trust this installer, run it again with --keep-bad-file-crc to keep files whose "
    "CRC-32 does not match (gitfurl cannot tell a damaged file from an archive with wrong "
    "checksums; the destination must not exist yet, so delete it or use another prefix).")
SHOWN_KEPT = 5  # files kept with a CRC-32 mismatch named in the end text (the log has all)

# --- the destination (C:\Games\<prefix> unless another folder is chosen) -------------------

DEFAULT_PARENT = "Games"
# Wine's own folders on drive C: (case folded): nothing is written in or below them.
WINE_FOLDERS = {"windows": "C:\\windows", "users": "C:\\users",
                "programdata": "C:\\ProgramData"}
# Wine's program folders: only a folder that is new directly below them may be chosen.
PROGRAM_FILES = {"program files": "C:\\Program Files",
                 "program files (x86)": "C:\\Program Files (x86)"}
DEST_EXAMPLE = "C:\\Games\\My Game"


class DestinationRefused(ValueError):
    """The chosen destination is not a folder gitfurl writes into. Nothing was written.
    Not an ExtractRefused: a bad --dest is the user's input, not a reason to pick another
    route."""


def default_destination(prefix_name: str) -> str:
    """The Windows path the native route uses when no folder was chosen."""
    return f"C:\\{DEFAULT_PARENT}\\{prefix_name}"


def windows_path(parts) -> str:
    """C:\\ followed by the parts, joined with backslashes."""
    return "C:\\" + "\\".join(parts)


def parse_destination(text: str) -> tuple[str, ...]:
    """The parts below C:\\ of a chosen destination, such as C:\\Games\\My Game (or
    C:/Games/My Game). Pure: reads nothing. Raises DestinationRefused with the reason.

    Rules: on drive C: and below C:\\ (C:\\ itself is refused); every part a name Windows can
    use (as for the archives' own names), with no empty part, no '.' or '..' and no control
    character; not in or below C:\\windows, C:\\users or C:\\ProgramData; not C:\\Program Files
    or C:\\Program Files (x86) themselves (a folder that is new directly below them is
    allowed). Whether the folders exist is checked later, against the prefix
    (ExtractPlan.dest_problem)."""
    raw = text.strip()
    shown = clean(raw, 120)
    if not raw:
        raise DestinationRefused(f"the destination is empty; give a Windows path such as "
                                 f"{DEST_EXAMPLE}")
    if any(ord(c) < 32 or ord(c) == 127 for c in raw):
        raise DestinationRefused("the destination contains a control character")
    path = raw.replace("/", "\\")
    if len(path) < 3 or path[:3].upper() != "C:\\":
        raise DestinationRefused(f"the destination must be a folder on drive C: of the prefix, "
                                 f"for example {DEST_EXAMPLE} (got {shown!r})")
    rest = path[3:]
    if rest.endswith("\\"):  # one trailing separator, as folder pickers write it
        rest = rest[:-1]
    if not rest:
        raise DestinationRefused(f"the destination cannot be C:\\ itself; give a folder below "
                                 f"it, for example {DEST_EXAMPLE}")
    parts = tuple(rest.split("\\"))
    if any(not p for p in parts):
        raise DestinationRefused(f"the destination has an empty part (two separators in a "
                                 f"row): {shown!r}")
    if any(p in (".", "..") for p in parts):
        raise DestinationRefused(f"the destination may not contain '.' or '..': {shown!r}")
    for p in parts:
        if _windows_problem(p):
            raise DestinationRefused(
                f"the destination has a name Windows cannot use: {clean(p, 80)!r} (a trailing "
                "dot or space, a character from <>:\"|?*, or a device name such as CON or NUL)")
    sizes = [len(p.encode("utf-8", "surrogateescape")) for p in parts]
    if max(sizes) > NAME_MAX or sum(sizes) + len(sizes) > PATH_MAX:
        raise DestinationRefused(f"the destination is too long for this system: {shown!r}")
    top = parts[0].casefold()
    if top in WINE_FOLDERS:
        raise DestinationRefused(f"the destination cannot be in {WINE_FOLDERS[top]} (Wine's own "
                                 f"folder); choose another folder, for example {DEST_EXAMPLE}")
    if top in PROGRAM_FILES and len(parts) == 1:
        raise DestinationRefused(f"the destination cannot be {PROGRAM_FILES[top]} itself; a "
                                 f"folder that is new directly below it is allowed, for "
                                 f"example {PROGRAM_FILES[top]}\\My Game")
    return parts


def _case_variant(folder: Path, name: str) -> str | None:
    """Another entry of `folder` whose name is `name` with letter case ignored, or None.
    Reads at most MAX_DIR_ENTRIES entries; follows no link."""
    key = name.casefold()
    try:
        with os.scandir(folder) as it:
            for n, e in enumerate(it):
                if n >= MAX_DIR_ENTRIES:
                    break
                if e.name != name and e.name.casefold() == key:
                    return e.name
    except OSError:
        return None
    return None


EXTRACTS_ONLY = ("This extracts files only. The installer is not run: no registry entries, no "
                 "redistributables, no shortcuts and none of its own steps. The result may "
                 "not work.")


class ExtractRefused(ValueError):
    """Native extraction cannot be used for this installer, or not now. No file was written.

    `created` lists the empty folders this run made before the refusal (only when creating
    the destination itself failed, for example in a race); they are kept, not deleted.
    """

    def __init__(self, message: str, created=()):
        super().__init__(message)
        self.created = [Path(p) for p in created]

    stopped = False  # True for ExtractStopped

    def outcome(self) -> str:
        if not self.created:
            return "Nothing was written."
        return ("No file was written; empty folders created and kept: "
                + ", ".join(str(p) for p in self.created))


@dataclass
class ArchivePlan:
    path: Path
    archive: farc.Archive
    files: list[tuple[int, str, farc.Entry]]  # (folder id, name, entry), in archive order
    dirs: list[int]  # folder ids of the archive's folder entries


@dataclass
class ExtractPlan:
    installer: Path
    prefix_name: str
    prefix: Path
    dest: Path
    archives: list[ArchivePlan]
    total_size: int
    file_count: int
    memory: int  # most bytes the decoders of one data block may hold at once
    notes: list[str] = field(default_factory=list)
    # folder id -> (parent id, name on disk); id 0 is the destination itself
    folders: list[tuple[int, str]] = field(default_factory=lambda: [(-1, "")])

    accept_bad_index_crc: bool = False
    keep_bad_file_crc: bool = False  # keep a file whose CRC-32 does not match (opt-in, per run)
    # the destination's parts below C:\ (empty: C:\Games\<prefix>); dest is drive_c/<parts>
    dest_parts: tuple[str, ...] = ()
    dest_chosen: bool = False  # another folder than the default was chosen (--dest)

    @property
    def win_parts(self) -> tuple[str, ...]:
        return self.dest_parts or (DEFAULT_PARENT, self.prefix_name)

    def with_destination(self, text: str | None) -> ExtractPlan:
        """This plan with the destination `text` (a Windows path, see parse_destination), or
        the default for None. Reads nothing; raises DestinationRefused."""
        default = (DEFAULT_PARENT, self.prefix_name)
        parts = default if text is None else parse_destination(text)
        return replace(self, dest=self.prefix.joinpath("drive_c", *parts), dest_parts=parts,
                       dest_chosen=parts != default)

    def archive_warnings(self, ap: ArchivePlan) -> list[str]:
        """The archive's index warnings, worded for this run's file CRC choice."""
        if not self.keep_bad_file_crc:
            return list(ap.archive.warnings)
        return [w.replace(farc.FILE_CRC_NOT_KEPT, "a file that fails is KEPT "
                          "(--keep-bad-file-crc) and listed.") for w in ap.archive.warnings]

    @property
    def warnings(self) -> list[str]:
        return [f"{clean(ap.path.name, 80)}: {warning}"
                for ap in self.archives for warning in self.archive_warnings(ap)]

    def parts(self, folder: int) -> list[str]:
        """The folder's path below the destination, as parts."""
        out: list[str] = []
        while folder > 0:
            folder, name = self.folders[folder]
            out.append(name)
        out.reverse()
        return out

    @property
    def target(self) -> str:
        return windows_path(self.win_parts)

    def dest_problem(self) -> str | None:
        """What is wrong with the destination in the prefix as it is now, or None: the folder
        must be new; each folder above it that exists must be a real folder (no symbolic link)
        spelt as on disk (Windows ignores letter case); below a Program Files folder, which must
        exist, the folder directly below it must be new. Reads lstat and folder listings only.
        """
        parts = self.win_parts
        at = self.prefix / "drive_c"
        pf = PROGRAM_FILES.get(parts[0].casefold())
        for i, part in enumerate(parts):
            win = clean(windows_path(parts[:i + 1]), 160)
            path = at / part
            try:
                st = os.lstat(path)
            except FileNotFoundError:
                other = _case_variant(at, part)
                if other is not None:
                    there = clean(windows_path((*parts[:i], other)), 160)
                    return (f"{win} would be the same folder as {there}, which exists (Windows "
                            "ignores letter case); use that spelling or another name")
                if pf is not None and i == 0:
                    return (f"{pf} does not exist in this prefix; gitfurl does not create "
                            "Wine's own folders")
                return None  # this folder and the ones below it are created
            except OSError as exc:
                return f"{win} could not be checked ({exc.strerror or exc})"
            if i == len(parts) - 1:
                return (f"the destination already exists: {self.dest}. gitfurl only extracts "
                        "into a new folder")
            if pf is not None and i == 1:
                return (f"{win} already exists; below {pf} gitfurl only creates a folder that "
                        f"is new directly below it (use another name, or C:\\{DEFAULT_PARENT})")
            if stat.S_ISLNK(st.st_mode):
                return (f"{win} is a symbolic link in the prefix ({path}); gitfurl does not "
                        "follow links on the way to the destination")
            if not stat.S_ISDIR(st.st_mode):
                return f"{win} is not a folder in the prefix ({path})"
            at = path
        return None

    def problem(self) -> str | None:
        """What stops a real extraction now (prefix missing, folder exists, space), or None."""
        from gitfurl.prefixes import incomplete_marker

        drive_c = self.prefix / "drive_c"
        if not self.prefix.is_dir() or self.prefix.is_symlink():
            return (f"the prefix does not exist: {self.prefix}. Create it first with "
                    f"'gitfurl cli prefix create {self.prefix_name}'")
        marker = incomplete_marker(self.prefix)
        if marker is not None:
            return (f"the prefix was not completely created ({marker} is still there: "
                    "creating it stopped, timed out or failed). Use a new prefix name")
        if not drive_c.is_dir() or drive_c.is_symlink():
            return f"the prefix has no drive_c folder: {drive_c}"
        return self.dest_problem() or self.space_problem(drive_c)

    def space_problem(self, at: Path, extra: int = 0) -> str | None:
        """Free space and inodes on the file system of `at` against what the files need, plus
        `extra` bytes (for example a prefix that is still to be created). Reads statvfs only."""
        st = os.statvfs(at)
        block = st.f_frsize or st.f_bsize or 4096
        need = self.disk_size(block) + extra
        free = st.f_bavail * block
        if free < need:
            plus = f", plus about {farc._size(extra)} for the new prefix" if extra else ""
            return (f"not enough free space: about {farc._size(need)} needed on disk "
                    f"({farc._size(self.total_size)} of file data{plus}), "
                    f"{farc._size(free)} free")
        inodes = self.file_count + len(self.folders)
        if st.f_files and st.f_favail < inodes:
            return (f"not enough free inodes: {inodes:,} files and folders to create, "
                    f"{st.f_favail:,} free")
        return None

    def space_problem_before_prefix(self, extra: int) -> str | None:
        """space_problem() for a prefix that does not exist yet: measured on the nearest folder
        above it that exists. Creates nothing."""
        at = self.prefix
        while not at.is_dir() and at != at.parent:
            at = at.parent
        return self.space_problem(at, extra)

    def disk_size(self, block: int) -> int:
        """Bytes on disk: each file rounded up to whole blocks, one block per folder."""
        blocks = len(self.folders)
        for ap in self.archives:
            for _, _, entry in ap.files:
                blocks += -(-entry.size // block)
        return blocks * block

    def describe(self) -> list[str]:
        lines = [
            ("NATIVE EXTRACTION (FreeArc: storing, lzma, exe, x86, delta, rep, 4x4, srep, "
             "cm and rolz): "
            "gitfurl reads the archives itself."),
        ]
        if self.keep_bad_file_crc:
            lines.append(f"  {KEEP_BAD_CRC_WARNING}")
        lines += [
            f"  {EXTRACTS_ONLY}",
            (f"  destination: {clean(self.target, 300)} (in the prefix: {self.dest}"
             + ("; the folder you chose" if self.dest_chosen else "")
             + "); it must not exist yet, folders above it that are missing are created, and "
             "no existing file is overwritten"),
        ]
        for ap in self.archives:
            lines.append(f"  archive: {clean(ap.path.name, 80)} ({farc._size(ap.archive.size)}"
                         f", {farc._n(len(ap.files), 'file', 'files')})")
        lines.append(f"  in total: {farc._n(self.file_count, 'file', 'files')}, "
                     f"{farc._size(self.total_size)} to write")
        if self.memory:
            lines.append(f"  memory: up to {farc._size(self.memory)} to decode one data block "
                         "(the decoders of its methods added together)")
        lines.append("  each archive's own folder layout is used; the installer's script may "
                     "have placed files differently")
        if self.keep_bad_file_crc:
            lines.append("  each file's CRC-32 is checked; a file that does not match is KEPT "
                         "and listed with its stored and computed CRC-32 (--keep-bad-file-crc); "
                         "each file's size must still match exactly, a decoder that finds "
                         "damaged data still stops the unpack, and nothing is overwritten")
        else:
            lines.append("  each file's CRC-32 is checked; a file that does not match is "
                         "removed again and listed")
        lines.extend(f"  warning: {warning}" for warning in self.warnings)
        lines.extend(f"  note: {n}" for n in self.notes)
        return lines


def find_archives(installer: Path) -> tuple[list[Path], list[str]]:
    """The installer (if it is one) and the regular files beside it that start with "ArC" 0x01.

    Returns (paths, notes). Symlinks are not followed. At most MAX_DIR_ENTRIES entries are
    looked at and at most MAX_ARCHIVES archives are returned.
    """
    installer = Path(installer)
    found: list[Path] = []
    notes: list[str] = []
    seen = 0
    try:
        it = os.scandir(installer.parent)
    except OSError as exc:
        raise ExtractRefused(f"the installer's folder could not be read ({exc.strerror})") \
            from exc
    with it:
        for e in it:
            seen += 1
            if seen > MAX_DIR_ENTRIES:
                notes.append(f"only the first {MAX_DIR_ENTRIES} entries of the installer's "
                             "folder were looked at")
                break
            try:
                if not e.is_file(follow_symlinks=False):
                    continue
            except OSError:
                continue
            if farc.has_signature(Path(e.path)):
                found.append(Path(e.path))
    found.sort(key=lambda p: (p.name != installer.name, p.name))
    if len(found) > MAX_ARCHIVES:
        raise ExtractRefused(f"more than {MAX_ARCHIVES} FreeArc archives are beside the "
                             "installer")
    return found, notes


@dataclass
class Unpacked:
    """What the FreeArc archives beside an installer unpack to, from their own indexes."""
    total: int  # bytes, summed over the archives whose index was read
    read: int  # archives whose index was read
    found: int  # archives found (some may not have been readable)
    complete: bool = True  # False when the folder listing stopped early (MAX_DIR_ENTRIES)


def unpacked_size(installer: Path) -> Unpacked | None:
    """Sum of the file sizes in the indexes of the FreeArc archives beside the installer,
    or None when no archive is there or none could be read. Read-only (bounded index reads,
    as for the plan); a method gitfurl cannot decode does not matter here, only the sizes."""
    try:
        paths, notes = find_archives(Path(installer))
    except (ExtractRefused, OSError):  # an OSError can come from reading the folder listing
        return None
    total = read = 0
    for path in paths:
        try:
            arc = farc.read_archive(path)
        except farc.FarcError:
            continue
        total += arc.total_size
        read += 1
    return Unpacked(total, read, len(paths), not notes) if read else None


def plan_extract(installer: Path, prefix_name: str,
                 env: dict[str, str] | None = None, *,
                 accept_bad_index_crc: bool = False,
                 keep_bad_file_crc: bool = False,
                 dest: str | None = None) -> ExtractPlan:
    """Read every archive's index and check every name. Writes nothing. Raises ExtractRefused,
    or DestinationRefused for a `dest` (a Windows path on drive C:, default
    C:\\Games\\<prefix>) that parse_destination refuses."""
    validate_prefix_name(prefix_name)
    if dest is not None:
        parse_destination(dest)  # before any archive is read
    installer = Path(installer)
    paths, notes = find_archives(installer)
    if not paths:
        raise ExtractRefused("no FreeArc archive was found (the installer or a file beside it "
                             "that starts with 'ArC' 0x01)")
    prefix = prefix_path(prefix_name, env)
    archives: list[ArchivePlan] = []
    folders: list[tuple[int, str]] = [(-1, "")]  # id -> (parent id, first spelling)
    # (parent folder id, case-folded name) -> folder id, or FILE. One step per path part, so
    # the work and memory are linear in the names' length, however deep the paths are.
    names: dict[tuple[int, str], int] = {}
    merged: str | None = None
    total = count = memory = srep = cm = 0
    for path in paths:
        name = clean(path.name, 80)
        try:
            arc = farc.read_archive(path, accept_bad_index_crc=accept_bad_index_crc)
        except farc.FarcError as exc:
            raise ExtractRefused(f"{name}: {clean(str(exc), 200)}") from exc
        if arc.unsupported:
            raise ExtractRefused(f"{name} uses {farc.KIND_OTHER} "
                                 f"({len(arc.unsupported)} of {len(arc.blocks)} data blocks); "
                                 "only storing, lzma, exe, x86, delta, rep, 4x4 (with "
                                 "lzma inside), srep, cm and rolz, alone or chained, can be "
                                 "extracted")
        if not farc_codecs.cm_available() and any(
                st.name == "cm" for blk in arc.blocks for st in blk.method.stages):
            raise ExtractRefused(f"{name}: {farc_codecs.CM_MISSING}")
        ap = ArchivePlan(path, arc, [], [])
        for entry in arc.entries:
            parts = farc.relative_parts(entry.path)
            if parts is None:
                raise ExtractRefused(f"{name} holds a name that is not safe to extract: "
                                     f"{clean(entry.path, 80)!r}")
            if not parts:
                if entry.is_dir:
                    continue
                raise ExtractRefused(f"{name} holds a file with no name")
            _check_length(name, entry.path, parts)
            if any(_windows_problem(p) for p in parts):
                raise ExtractRefused(f"{name} holds a name Windows cannot use: "
                                     f"{clean(entry.path, 80)!r}")
            same = (f"{name}: two entries would be the same file or folder (letter case "
                    f"ignored, as Windows does): {clean(entry.path, 80)!r}")
            cur, last = 0, len(parts) - 1
            for i, part in enumerate(parts):
                key = (cur, part.casefold())
                got = names.get(key)
                if i == last and not entry.is_dir:
                    if got is not None:
                        raise ExtractRefused(same)
                    names[key] = FILE
                    break
                if got == FILE:
                    if i < last:
                        raise ExtractRefused(f"{name}: {clean(entry.path, 80)!r} would go "
                                             "inside a file of the same name")
                    raise ExtractRefused(same)
                if got is None:
                    if len(folders) > MAX_FOLDERS:
                        raise ExtractRefused(f"more than {MAX_FOLDERS:,} folders")
                    got = len(folders)
                    folders.append((cur, part))
                    names[key] = got
                elif merged is None and folders[got][1] != part:
                    merged = (f"folder names that differ only in letter case are one folder, "
                              f"as on Windows (the first spelling is used), for example "
                              f"{clean(part, 40)!r} goes into {clean(folders[got][1], 40)!r}")
                cur = got
            if entry.is_dir:
                ap.dirs.append(cur)
            else:
                ap.files.append((cur, parts[-1], entry))
                total += entry.size
                count += 1
        for blk in arc.blocks:
            memory = max(memory, farc_codecs.chain_memory(blk.method, blk.origsize, CHUNK))
            srep = max([srep] + [p.budget for p in farc_codecs.stage_plan(blk.method,
                                                                          blk.origsize)
                                 if p.stage.name == "srep"])
            cm = max([cm] + [p.budget for p in farc_codecs.stage_plan(blk.method, blk.origsize)
                             if p.stage.name == "cm"])
        archives.append(ap)
    if merged:
        notes.append(merged)
    if srep:
        notes.append(f"srep keeps up to {farc._size(srep)} of earlier output, pending matches "
                     "or its input between its blocks (counted in the memory line); data that "
                     "needs more is refused and the extraction stops with an error")
    if cm:
        notes.append(f"the cm stage decodes {cm} job{'s' if cm > 1 else ''} at a time, each "
                     f"up to {farc._size(farc_codecs.CM_JOB_MEMORY)} (counted in the memory "
                     "line), with gitfurl's native decoder module")
    plan = ExtractPlan(installer, prefix_name, prefix,
                       prefix / "drive_c" / DEFAULT_PARENT / prefix_name, archives, total,
                       count, memory, notes, folders, accept_bad_index_crc, keep_bad_file_crc)
    return plan.with_destination(dest)


def _windows_problem(part: str) -> bool:
    """True for a name Windows (and Wine's Windows paths) would not open as itself."""
    return (part[-1] in ". " or bool(_WIN_BAD.search(part))
            or part.split(".", 1)[0].rstrip(" ").upper() in _WIN_DEVICES)


def _check_length(name: str, stored: str, parts: list[str]) -> None:
    sizes = [len(p.encode("utf-8", "surrogateescape")) for p in parts]
    if max(sizes) > NAME_MAX or sum(sizes) + len(sizes) > PATH_MAX:
        raise ExtractRefused(f"{name} holds a name too long for this system: "
                             f"{clean(stored, 80)!r}")


# --- extraction ---------------------------------------------------------------------------

class _Stopped(Exception):
    pass


class _DataError(Exception):
    pass


@dataclass
class ExtractResult:
    dest: Path
    files: int = 0
    folders: int = 0
    bytes_written: int = 0
    crc_errors: list[str] = field(default_factory=list)  # removed again
    crc_kept: list[str] = field(default_factory=list)  # failed the CRC, kept (opt-in)
    # every CRC-32 mismatch: (name, stored, computed, what happened to the file)
    crc_details: list[tuple[str, int, int, str]] = field(default_factory=list)
    # archives every one of whose files was checked and failed: (name, number of files)
    all_failed: list[tuple[str, int]] = field(default_factory=list)
    keep_bad_file_crc: bool = False
    not_removed: list[str] = field(default_factory=list)  # failed the CRC, removal failed
    incomplete: str | None = None  # a file that was being written when it ended early
    stopped: bool = False
    error: str | None = None

    @property
    def ok(self) -> bool:
        return not (self.stopped or self.error or self.crc_errors)

    def lines(self) -> list[str]:
        head = "stopped" if self.stopped else "ended with an error" if self.error else "finished"
        out = [(f"Extraction {head}: {farc._n(self.files, 'file', 'files')} "
               f"({farc._size(self.bytes_written)}) written to {self.dest}. "
               "Files extracted; nothing was installed.")]
        if self.error:
            out.append(f"error: {self.error}")
        if self.incomplete:
            out.append(f"incomplete file (kept): {self.incomplete}")
        if self.crc_errors:
            out.append(f"{farc._n(len(self.crc_errors), 'file', 'files')} failed the CRC-32 "
                       "check (removed again): " + ", ".join(self.crc_errors[:5])
                       + (", ..." if len(self.crc_errors) > 5 else ""))
        if self.not_removed:
            out.append("could not be removed after failing the CRC-32 check (damaged, still "
                       "there): " + ", ".join(self.not_removed[:5])
                       + (", ..." if len(self.not_removed) > 5 else ""))
        if self.crc_kept:
            kept = [d for d in self.crc_details if d[3] == "kept"]
            shown = ", ".join(f"{n} (stored {s:08X}, computed {c:08X})"
                              for n, s, c, _ in kept[:SHOWN_KEPT])
            more = len(kept) - SHOWN_KEPT
            out.append(f"WARNING: {farc._n(len(self.crc_kept), 'file', 'files')} kept although "
                       "the CRC-32 does not match the archive (--keep-bad-file-crc): " + shown
                       + (f", and {more} more (every one is in the log)" if more > 0 else ""))
        for name, count in self.all_failed:
            out.append(f"note: every one of the {farc._n(count, 'file', 'files')} in {name} "
                       "failed its CRC-32 check")
        if self.crc_errors and not self.keep_bad_file_crc:
            out.append(KEEP_BAD_CRC_SUGGESTION)
        if self.stopped or self.error:
            out.append("Files already written were kept, except those removed for a CRC-32 "
                       "mismatch (listed above)." if self.crc_errors else
                       "Files already written were kept; nothing was deleted.")
        return out


class _Tree:
    """Folders under the destination, created by this run and opened without following links.

    At most CACHE folder fds are kept open (least recently used are closed), so the number of
    folders and the depth of a path do not run into the open-file limit. A folder that is not
    open is reached from its nearest open ancestor (or the destination), one part at a time,
    with at most two fds of the walk open at once.
    """

    CACHE = 64

    def __init__(self, root_fd: int, folders: list[tuple[int, str]]):
        self.root = root_fd
        self.folders = folders
        self.open: OrderedDict[int, int] = OrderedDict()  # folder id -> fd
        self.made = 0

    def fd(self, folder: int) -> int:
        """The folder's fd, creating it and any missing parents (no recursion: paths can be
        deep). Only folders this run made exist below the root, so an existing name is one
        of them; O_NOFOLLOW and O_DIRECTORY refuse anything else. The fd stays valid until
        the next call."""
        if folder == 0:
            return self.root
        if folder in self.open:
            self.open.move_to_end(folder)
            return self.open[folder]
        chain = []
        cur = folder
        while cur != 0 and cur not in self.open:
            chain.append(cur)
            cur = self.folders[cur][0]
        parent = self.root if cur == 0 else self.open[cur]
        walked = None  # an fd this walk opened, closed once its child is open
        try:
            for fid in reversed(chain):
                name = self.folders[fid][1]
                try:
                    os.mkdir(name, 0o755, dir_fd=parent)
                    self.made += 1
                except FileExistsError:
                    pass
                child = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
                                | os.O_CLOEXEC, dir_fd=parent)
                if walked is not None:
                    os.close(walked)
                walked = parent = child
        except BaseException:
            if walked is not None:
                os.close(walked)
            raise
        self.open[folder] = walked
        if len(self.open) > self.CACHE:
            os.close(self.open.popitem(last=False)[1])
        return walked

    def close(self) -> None:
        for fd in self.open.values():
            os.close(fd)
        self.open.clear()


def _open_dir(parent: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent)


def _make_dest(plan: ExtractPlan) -> int:
    """Create the destination folder and return its fd. The folders above it are walked from
    drive_c one part at a time through fds, never following a link (O_NOFOLLOW); a missing one
    is created (mode 0755), except a Program Files folder, which is Wine's own. Raises
    ExtractRefused, which lists the (empty) folders this call created before it failed."""
    try:
        drive_c = os.open(plan.prefix / "drive_c",
                          os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ExtractRefused(f"the prefix's drive_c could not be opened ({exc.strerror})") \
            from exc
    parts = plan.win_parts
    created: list[Path] = []
    cur = drive_c
    path = plan.prefix / "drive_c"
    try:
        for i, part in enumerate(parts[:-1]):
            path = path / part
            try:
                nxt = _open_dir(cur, part)
            except FileNotFoundError:
                if i == 0 and part.casefold() in PROGRAM_FILES:
                    raise ExtractRefused(f"{PROGRAM_FILES[part.casefold()]} does not exist in "
                                         "this prefix; gitfurl does not create Wine's own "
                                         "folders", created) from None
                try:
                    os.mkdir(part, 0o755, dir_fd=cur)
                    created.append(path)
                except FileExistsError:  # made by something else meanwhile; opened as any other
                    pass
                nxt = _open_dir(cur, part)
            if cur != drive_c:
                os.close(cur)
            cur = nxt
        path = plan.dest
        os.mkdir(parts[-1], 0o755, dir_fd=cur)
        created.append(plan.dest)
        return _open_dir(cur, parts[-1])
    except FileExistsError as exc:
        raise ExtractRefused(f"the destination already exists: {plan.dest}", created) from exc
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise ExtractRefused(f"a folder on the way to the destination is a symbolic link or "
                                 f"not a folder ({path}); gitfurl does not follow links",
                                 created) from exc
        raise ExtractRefused(f"the destination could not be created ({exc.strerror})",
                             created) from exc
    finally:
        if cur != drive_c:
            os.close(cur)
        os.close(drive_c)


def _chunks(fd: int, blk: farc.DataBlock, stop, parts: list | None = None):
    """The block's decoded bytes, exactly blk.origsize of them, CHUNK at a time. Every decoder
    of the chain is made here, before the block's first file is opened; raises _DataError.
    `parts` receives the chain's readers (see farc_codecs.open_chain)."""
    try:
        top = farc_codecs.open_chain(blk.method, fd, blk.offset, blk.compsize, blk.origsize,
                                     CHUNK, stop, parts)
    except farc_codecs.StartError as exc:
        raise _DataError(str(exc)) from exc
    return _decode(top, blk.origsize, stop)


def _decode(top, size: int, stop):
    left = size
    while left:
        stop()
        try:
            out = top.read(min(CHUNK, left))
        except farc_codecs.CodecError as exc:
            raise _DataError(f"a data block could not be decompressed ({exc})") from exc
        except MemoryError as exc:
            raise _DataError("a data block could not be decompressed (not enough memory)") \
                from exc
        if not out:
            raise _DataError("a data block ended before all of its files were complete")
        left -= len(out)
        yield out


class ExtractStopped(ExtractRefused):
    """Stop was asked for before the destination was created. No file or folder was written."""

    stopped = True


def extract(plan: ExtractPlan, cancel: threading.Event | None = None,
            on_progress=None, on_event=None, activity=None) -> ExtractResult:
    """Write the planned files. Raises ExtractRefused before anything is written; once the
    destination exists, every problem is reported in the result instead.

    `on_event(kind, archive_plan, *details)` (optional, on this thread) is told when each
    archive's index has been read again ("archive"), and when each data block starts
    ("block", block number, DataBlock, number of its files); the timeline uses it. When a
    block's files are all written it is told ("block_end", block number, DataBlock, the
    stage rows of gitfurl.activity.summary_row, the block's seconds). `activity` (a gitfurl.activity.Activity)
    is kept up to date for a live view; one is made when none is given."""
    problem = plan.problem()
    if problem:
        raise ExtractRefused(problem)
    # Read every index again from the files that will be read, and compare with the plan.
    opened: list[tuple[int, ArchivePlan]] = []
    try:
        for ap in plan.archives:
            try:
                fd = os.open(ap.path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            except OSError as exc:
                raise ExtractRefused(f"{clean(ap.path.name, 80)} could not be opened "
                                     f"({exc.strerror})") from exc
            opened.append((fd, ap))
            try:
                again = farc.read_fd(fd, ap.path,
                                     accept_bad_index_crc=plan.accept_bad_index_crc)
            except farc.FarcError as exc:
                raise ExtractRefused(f"{clean(ap.path.name, 80)}: {clean(str(exc), 200)}") \
                    from exc
            if (again.size, again.blocks, again.entries, again.warnings) != \
                    (ap.archive.size, ap.archive.blocks, ap.archive.entries, ap.archive.warnings):
                raise ExtractRefused(f"{clean(ap.path.name, 80)} changed after the plan was "
                                     "made; make the plan again")
            if on_event is not None:
                on_event("archive", ap)
        if cancel is not None and cancel.is_set():
            raise ExtractStopped("stopped before the unpack started")
        root = _make_dest(plan)
        return _write_all(plan, opened, root, cancel, on_progress, on_event, activity)
    finally:
        for fd, _ in opened:
            os.close(fd)


def _write_all(plan, opened, root, cancel, on_progress, on_event=None,
               act=None) -> ExtractResult:
    from gitfurl.activity import Activity

    res = ExtractResult(plan.dest, keep_bad_file_crc=plan.keep_bad_file_crc)
    act = act if act is not None else Activity()
    used = [{e.block for _f, _n, e in ap.files} for _fd, ap in opened]
    act.begin_run(sum(b.compsize for (_fd, ap), ks in zip(opened, used)
                      for k, b in enumerate(ap.archive.blocks) if k in ks),
                  sum(len(ap.files) for _fd, ap in opened))
    tree = _Tree(root, plan.folders)
    counts: list[list] = []  # per archive: [name, files, files checked, files that failed]

    def stop():
        if cancel is not None and cancel.is_set():
            raise _Stopped

    current: list[str] = []
    done = 0  # bytes decoded into files, for progress (removed files included)
    try:
        for fd, ap in opened:
            count = [clean(ap.path.name, 80), len(ap.files), 0, 0]
            counts.append(count)
            for folder in ap.dirs:
                tree.fd(folder)
            by_block: dict[int, list] = {}
            for folder, name, entry in ap.files:
                by_block.setdefault(entry.block, []).append((folder, name, entry))
            for k, blk in enumerate(ap.archive.blocks):
                files = by_block.get(k)
                if not files:
                    continue
                if on_event is not None:
                    on_event("block", ap, k, blk, len(files))
                parts: list = []
                stream = _chunks(fd, blk, stop, parts)
                act.begin_block(ap.path.name, k, len(ap.archive.blocks), blk.method.kind,
                                blk.compsize, blk.origsize, len(files), parts)
                buf, at = b"", 0
                for folder, name, entry in files:
                    stop()
                    shown = clean("/".join([*plan.parts(folder), name]), 120)
                    out = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                  | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                                  dir_fd=tree.fd(folder))
                    current[:] = [shown]
                    act.file_start(shown)
                    crc, left = 0, entry.size
                    try:
                        while left:
                            if at == len(buf):
                                buf, at = next(stream), 0
                            piece = buf[at:at + left]
                            at += len(piece)
                            left -= len(piece)
                            crc = zlib.crc32(piece, crc)
                            while piece:
                                piece = piece[os.write(out, piece):]
                        try:  # through the open file: never another file of that name
                            os.utime(out, (entry.mtime, entry.mtime))
                        except (OSError, OverflowError, ValueError):
                            pass
                    finally:
                        os.close(out)
                    current.clear()
                    act.file_end()
                    done += entry.size
                    count[2] += 1
                    if crc != entry.crc:
                        count[3] += 1
                        if plan.keep_bad_file_crc:
                            # Asked for, for this run: the file is kept (its size matched).
                            res.crc_kept.append(shown)
                            res.crc_details.append((shown, entry.crc, crc, "kept"))
                            res.bytes_written += entry.size
                            res.files += 1
                        else:
                            res.crc_errors.append(shown)
                            try:  # by name in the folder this run created; no link followed
                                os.unlink(name, dir_fd=tree.fd(folder))
                                what = "removed again"
                            except OSError:
                                res.not_removed.append(shown)
                                what = "could not be removed"
                            res.crc_details.append((shown, entry.crc, crc, what))
                    else:
                        res.bytes_written += entry.size
                        res.files += 1
                    if on_progress is not None:
                        on_progress(done, plan.total_size)
                rows = act.end_block()
                if on_event is not None:
                    on_event("block_end", ap, k, blk, rows, act.last_block_s)
    except _Stopped:
        res.stopped = True
    except KeyboardInterrupt:
        res.stopped = True
    except StopIteration:
        res.error = "a data block ended before all of its files were complete"
    except _DataError as exc:
        res.error = str(exc)
    except OSError as exc:
        res.error = f"writing failed ({exc.strerror or exc})"
    except MemoryError:
        res.error = "not enough memory"
    except Exception as exc:  # noqa: BLE001 - once the destination exists, report, never raise
        res.error = f"unexpected error ({type(exc).__name__})"
    finally:
        act.finish()
        res.folders = tree.made
        tree.close()
        os.close(root)
    if current:
        res.incomplete = current[0]
    res.all_failed = [(name, files) for name, files, checked, failed in counts
                      if files and checked == files == failed]
    return res
