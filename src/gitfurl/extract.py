"""Native extraction of FreeArc archives (storing and lzma only), without running the installer.

Off unless asked for, per run, after an approval that shows the plan. gitfurl reads each
FreeArc archive beside the installer itself (gitfurl.farc) and writes the files into a new
folder in the prefix, C:\\Games\\<prefix> (the folder the unattended install uses). The
installer is not run: no registry entries, no redistributables, no shortcuts and none of its
own steps, so the result may not work.

Refused before anything is written: no FreeArc archive found; an archive gitfurl cannot read;
any data block with a method other than storing or a single lzma (including encryption); a
name that is not a safe relative path; two names that would be the same file (letter case
ignored, as Windows does); a name used both as a file and as a folder; too little free space;
a destination folder that already exists; a prefix that does not exist.

While writing: every file is created new (O_CREAT|O_EXCL|O_NOFOLLOW) inside folders this
run created, opened relative to the destination's own file descriptor, so nothing outside it
is written and nothing is overwritten. Output per data block stops at the sum of its files'
declared sizes. Each file's CRC-32 is checked; a mismatch is reported and the file is kept.
Nothing from an archive is run or passed to a program. Stop (a cancel flag, or Ctrl-C) ends
the extraction between chunks; files already written stay, and nothing is deleted.
"""

from __future__ import annotations

import lzma
import os
import shutil
import threading
import zlib
from dataclasses import dataclass, field
from pathlib import Path

from gitfurl import farc
from gitfurl.detect import clean
from gitfurl.prefixes import prefix_path, validate_prefix_name

MAX_ARCHIVES = 64  # FreeArc files beside the installer that are read
MAX_DIR_ENTRIES = 10_000  # directory entries looked at when searching for them
CHUNK = 1 << 20  # bytes read or decoded at a time (also how often Stop is checked)
NAME_MAX = 255  # bytes of one path part (Linux)
PATH_MAX = 4000  # bytes of one relative path

EXTRACTS_ONLY = ("This extracts files only. The installer is not run: no registry entries, no "
                 "redistributables, no shortcuts and none of its own steps. The result may "
                 "not work.")


class ExtractRefused(ValueError):
    """Native extraction cannot be used for this installer, or not now. Nothing was written."""


@dataclass
class ArchivePlan:
    path: Path
    archive: farc.Archive
    files: list[tuple[tuple[str, ...], farc.Entry]]  # in archive order
    dirs: list[tuple[str, ...]]


@dataclass
class ExtractPlan:
    installer: Path
    prefix_name: str
    prefix: Path
    dest: Path
    archives: list[ArchivePlan]
    total_size: int
    file_count: int
    max_dict: int  # largest lzma dictionary the decoder will allocate
    notes: list[str] = field(default_factory=list)

    @property
    def target(self) -> str:
        return f"C:\\Games\\{self.prefix_name}"

    def problem(self) -> str | None:
        """What stops a real extraction now (prefix missing, folder exists, space), or None."""
        drive_c = self.prefix / "drive_c"
        if not self.prefix.is_dir() or self.prefix.is_symlink():
            return (f"the prefix does not exist: {self.prefix}. Create it first with "
                    f"'gitfurl cli prefix create {self.prefix_name}'")
        if not drive_c.is_dir() or drive_c.is_symlink():
            return f"the prefix has no drive_c folder: {drive_c}"
        if os.path.lexists(self.dest):
            return (f"the destination already exists: {self.dest}. gitfurl only extracts "
                    "into a new folder")
        free = shutil.disk_usage(drive_c).free
        if free < self.total_size:
            return (f"not enough free space: {farc._size(self.total_size)} needed, "
                    f"{farc._size(free)} free")
        return None

    def describe(self) -> list[str]:
        lines = [
            ("NATIVE EXTRACTION (FreeArc, storing and lzma only): gitfurl reads the archives "
            "itself."),
            f"  {EXTRACTS_ONLY}",
            (f"  destination: {self.target} (in the prefix: {self.dest}); it must not exist "
            "yet, and no existing file is overwritten"),
        ]
        for ap in self.archives:
            lines.append(f"  archive: {clean(ap.path.name, 80)} ({farc._size(ap.archive.size)}"
                         f", {farc._n(len(ap.files), 'file', 'files')})")
        lines.append(f"  in total: {farc._n(self.file_count, 'file', 'files')}, "
                     f"{farc._size(self.total_size)} to write")
        if self.max_dict:
            lines.append(f"  memory: up to {farc._size(self.max_dict)} for the lzma decoder")
        lines.append("  each archive's own folder layout is used; the installer's script may "
                     "have placed files differently")
        lines.append("  each file's CRC-32 is checked; a file that does not match is kept and "
                     "listed")
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


def plan_extract(installer: Path, prefix_name: str,
                 env: dict[str, str] | None = None) -> ExtractPlan:
    """Read every archive's index and check every name. Writes nothing. Raises ExtractRefused."""
    validate_prefix_name(prefix_name)
    installer = Path(installer)
    paths, notes = find_archives(installer)
    if not paths:
        raise ExtractRefused("no FreeArc archive was found (the installer or a file beside it "
                             "that starts with 'ArC' 0x01)")
    prefix = prefix_path(prefix_name, env)
    archives: list[ArchivePlan] = []
    taken: dict[tuple[str, ...], str] = {}  # case-folded path -> "file" or "folder"
    total = count = max_dict = 0
    for path in paths:
        name = clean(path.name, 80)
        try:
            arc = farc.read_archive(path)
        except farc.FarcError as exc:
            raise ExtractRefused(f"{name}: {clean(str(exc), 200)}") from exc
        if arc.unsupported:
            raise ExtractRefused(f"{name} uses {farc.KIND_OTHER} "
                                 f"({len(arc.unsupported)} of {len(arc.blocks)} data blocks); "
                                 "only storing and lzma can be extracted")
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
            key = tuple(p.casefold() for p in parts)
            kind = "folder" if entry.is_dir else "file"
            for i in range(1, len(key)):
                if taken.get(key[:i]) == "file":
                    raise ExtractRefused(f"{name}: {clean(entry.path, 80)!r} would go inside "
                                         "a file of the same name")
                taken.setdefault(key[:i], "folder")
            if key in taken and (kind == "file" or taken[key] == "file"):
                raise ExtractRefused(f"{name}: two entries would be the same file or folder "
                                     f"(letter case ignored, as Windows does): "
                                     f"{clean(entry.path, 80)!r}")
            taken[key] = kind
            if entry.is_dir:
                ap.dirs.append(tuple(parts))
            else:
                ap.files.append((tuple(parts), entry))
                total += entry.size
                count += 1
        for blk in arc.blocks:
            if blk.method.lzma:
                max_dict = max(max_dict, _dict_size(blk))
        archives.append(ap)
    dest = prefix / "drive_c" / "Games" / prefix_name
    return ExtractPlan(installer, prefix_name, prefix, dest, archives, total, count, max_dict,
                       notes)


def _check_length(name: str, stored: str, parts: list[str]) -> None:
    sizes = [len(p.encode("utf-8", "surrogateescape")) for p in parts]
    if max(sizes) > NAME_MAX or sum(sizes) + len(sizes) > PATH_MAX:
        raise ExtractRefused(f"{name} holds a name too long for this system: "
                             f"{clean(stored, 80)!r}")


def _dict_size(blk: farc.DataBlock) -> int:
    """The decoder never needs a dictionary larger than the block's output."""
    return min(blk.method.lzma["dict_size"], max(4096, blk.origsize))


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
    crc_errors: list[str] = field(default_factory=list)
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
                       "check (kept): " + ", ".join(self.crc_errors[:5])
                       + (", ..." if len(self.crc_errors) > 5 else ""))
        if self.stopped or self.error:
            out.append("Files already written were kept; nothing was deleted.")
        return out


class _Tree:
    """Folders under the destination, created by this run and opened without following links."""

    def __init__(self, root_fd: int):
        self.fds: dict[tuple[str, ...], int] = {(): root_fd}
        self.made = 0

    def fd(self, parts: tuple[str, ...]) -> int:
        """The folder's fd, creating it and any missing parents (no recursion: paths can be
        deep). Only folders this run made exist below the root, so an existing name is one
        of them; O_NOFOLLOW and O_DIRECTORY refuse anything else."""
        i = len(parts)
        while parts[:i] not in self.fds:
            i -= 1
        while i < len(parts):
            parent = self.fds[parts[:i]]
            try:
                os.mkdir(parts[i], 0o755, dir_fd=parent)
                self.made += 1
            except FileExistsError:
                pass
            self.fds[parts[:i + 1]] = os.open(
                parts[i], os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                dir_fd=parent)
            i += 1
        return self.fds[parts]

    def close(self) -> None:
        for parts, fd in self.fds.items():
            if parts:
                os.close(fd)


def _open_dir(parent: int, name: str, create: bool) -> int:
    if create:
        os.mkdir(name, 0o755, dir_fd=parent)
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                   dir_fd=parent)


def _make_dest(plan: ExtractPlan) -> int:
    """Create the destination folder and return its fd. Raises ExtractRefused."""
    try:
        drive_c = os.open(plan.prefix / "drive_c",
                          os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except OSError as exc:
        raise ExtractRefused(f"the prefix's drive_c could not be opened ({exc.strerror})") \
            from exc
    try:
        try:
            games = _open_dir(drive_c, "Games", create=False)
        except FileNotFoundError:
            games = _open_dir(drive_c, "Games", create=True)
        try:
            return _open_dir(games, plan.prefix_name, create=True)
        finally:
            os.close(games)
    except FileExistsError as exc:
        raise ExtractRefused(f"the destination already exists: {plan.dest}") from exc
    except OSError as exc:
        raise ExtractRefused(f"the destination could not be created ({exc.strerror})") from exc
    finally:
        os.close(drive_c)


def _chunks(fd: int, blk: farc.DataBlock, stop):
    """The block's decoded bytes, exactly blk.origsize of them, CHUNK at a time."""
    pos, end = blk.offset, blk.offset + blk.compsize

    def read_in() -> bytes:
        nonlocal pos
        n = min(CHUNK, end - pos)
        data = os.pread(fd, n, pos)
        if len(data) != n:
            raise _DataError("the archive could not be read completely (did it change?)")
        pos += n
        return data

    left = blk.origsize
    if blk.method.lzma is None:  # storing: compsize == origsize was checked by farc
        while left:
            stop()
            data = read_in()
            left -= len(data)
            yield data
        return
    opts = dict(blk.method.lzma, dict_size=_dict_size(blk))
    dec = lzma.LZMADecompressor(lzma.FORMAT_RAW, filters=[{"id": lzma.FILTER_LZMA1, **opts}])
    while left:
        stop()
        if dec.eof:
            raise _DataError("a data block ended before all of its files were complete")
        if dec.needs_input:
            if pos >= end:
                raise _DataError("a data block ended before all of its files were complete")
            data = read_in()
        else:
            data = b""
        try:
            out = dec.decompress(data, max_length=min(CHUNK, left))
        except (lzma.LZMAError, EOFError) as exc:
            raise _DataError("a data block could not be decompressed (damaged data)") from exc
        left -= len(out)
        if out:
            yield out


def extract(plan: ExtractPlan, cancel: threading.Event | None = None,
            on_progress=None) -> ExtractResult:
    """Write the planned files. Raises ExtractRefused before anything is written; once the
    destination exists, every problem is reported in the result instead."""
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
                again = farc.read_fd(fd, ap.path)
            except farc.FarcError as exc:
                raise ExtractRefused(f"{clean(ap.path.name, 80)}: {clean(str(exc), 200)}") \
                    from exc
            if (again.size, again.blocks, again.entries) != \
                    (ap.archive.size, ap.archive.blocks, ap.archive.entries):
                raise ExtractRefused(f"{clean(ap.path.name, 80)} changed after the plan was "
                                     "made; make the plan again")
        root = _make_dest(plan)
        return _write_all(plan, opened, root, cancel, on_progress)
    finally:
        for fd, _ in opened:
            os.close(fd)


def _write_all(plan, opened, root, cancel, on_progress) -> ExtractResult:
    res = ExtractResult(plan.dest)
    tree = _Tree(root)

    def stop():
        if cancel is not None and cancel.is_set():
            raise _Stopped

    current: list[str] = []
    try:
        for fd, ap in opened:
            for parts in ap.dirs:
                tree.fd(parts)
            by_block: dict[int, list] = {}
            for parts, entry in ap.files:
                by_block.setdefault(entry.block, []).append((parts, entry))
            for k, blk in enumerate(ap.archive.blocks):
                files = by_block.get(k, [])
                stream = _chunks(fd, blk, stop)
                buf, at = b"", 0
                for parts, entry in files:
                    stop()
                    shown = clean("/".join(parts), 120)
                    out = os.open(parts[-1], os.O_WRONLY | os.O_CREAT | os.O_EXCL
                                  | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644,
                                  dir_fd=tree.fd(parts[:-1]))
                    current[:] = [shown]
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
                    finally:
                        os.close(out)
                    res.bytes_written += entry.size
                    current.clear()
                    res.files += 1
                    if crc != entry.crc:
                        res.crc_errors.append(shown)
                    try:
                        os.utime(parts[-1], (entry.mtime, entry.mtime),
                                 dir_fd=tree.fd(parts[:-1]), follow_symlinks=False)
                    except (OSError, OverflowError, ValueError):
                        pass
                    if on_progress is not None:
                        on_progress(res.bytes_written, plan.total_size)
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
    finally:
        res.folders = tree.made
        tree.close()
        os.close(root)
    if current:
        res.incomplete = current[0]
    return res
