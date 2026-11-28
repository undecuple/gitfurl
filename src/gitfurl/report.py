"""A scrubbed support report from gitfurl's own files for a run, to paste into an issue.

It keeps what helps to debug gitfurl: its version, the route and the reason for it, gitfurl's
names for the FreeArc stages, the Inno Setup data format version, the steps with times
relative to the run's start, exit status, gitfurl's own warnings and errors, the options used,
counts, the CRC outcome counts and the environment (Wine version, runtime, Python, the OS
family, the distribution ID and version, the architecture and the native module). It removes
what identifies the software or the user, with an allowlist mindset: when unsure, remove.

Every line goes through three passes:
1. Known names, collected from the run's own data: the installer and archive paths in the log,
   the files the log lists, the names beside the installer, the Inno Setup header fields and
   the archives' own file lists (both read-only, only while the installer is still there),
   the prefix, your user name, real name, home folder, host name and machine ID. Each is
   replaced exactly, longest first, ignoring case, by a placeholder that stays the same for
   the whole report (`<installer>.exe`, `<archive-1>.bin`, `<file-17>.dll`, `<app>`, `<prefix>`).
2. Generic patterns as a second net: URLs, e-mail, IP and MAC addresses, UUIDs and GUIDs,
   dates and clock times, time zones, absolute paths (only gitfurl's own folders stay, as
   placeholders), Windows paths and user profiles, registry keys and values, file names with
   an extension, quoted texts (window titles), CRC-32, SHA-256 and MD5 values, long hex and
   base64 strings, uid/gid numbers and environment values gitfurl did not set. Sizes are
   rounded to two significant figures ("about 9.4 GB") unless exact sizes are asked for.
3. A check: if any known name, the user name or the host name is still in the scrubbed text,
   no report is made (ReportRefused says which kind of name, never the name itself).

Reads gitfurl's own files only (O_NOFOLLOW, regular files, capped), and, read-only, the headers
of the installer and archives named in them while they are still there. Starts no process; the
caller passes the Wine version in (gitfurl.doctor's probe). Nothing is sent anywhere: the
report is printed, copied or written to a new file the user names.
"""

from __future__ import annotations

import json
import math
import os
import platform
import re
import shlex
import stat
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from gitfurl import __version__

HEADER = ("gitfurl support report — scrubbed: software names, file names, checksums, user, "
          "host and paths replaced; {sizes}. Nothing was sent anywhere.")
SIZES_ROUNDED = "sizes rounded"
SIZES_EXACT = "sizes exact, as chosen"
WINDOW_NOTICE = ("This is everything the report contains. Names of the installer, its files "
                 "and your user and machine were replaced; sizes are rounded. Check it before "
                 "you share it.")

KEEP_EXT = (".exe", ".dll", ".bin", ".inf", ".iss", ".reg", ".desktop", ".msi", ".cab")
READ_MAX = 8 << 20  # bytes of one file read (a larger log keeps its start and its end)
HEAD_KEEP = 1 << 20
WINE_LINES = 60  # lines of Wine's own output kept (the last ones that look like messages)
FILE_NAMES_MAX = 50_000  # names collected from the archives' indexes and the setup header
LISTDIR_MAX = 1000  # names looked at beside the installer
PHRASES_MAX = 3000  # names with spaces matched anywhere (more file names: whole tokens only)

# Words that name nothing in particular: as a whole name they are not replaced on their own
# (only inside a path, where every part is replaced) and the check does not look for them.
GENERIC = frozenset("""
setup install installer installers bin bin32 bin64 data game games redist _redist
_commonredist commonredist redistributables prerequisites _prerequisites directx common
sound sounds audio music video videos movies maps docs doc lang language languages localization
system system32 syswow64 engine content assets textures saves save config support tools plugins
x64 x86 win32 win64 app apps readme license licence eula manual default temp tmp files file
program programs launcher uninstall unins000 logs log cache shaders fonts media resources res
lib libs dlc patch update updates base core main english en de fr es it ru ja zh binaries
windows drive_c users public prefix prefixes wine proton gitfurl home downloads desktop
documents projects
""".split())

# Folder names kept inside Windows and prefix paths (Wine's and Windows' own layout).
KEEP_DIRS = frozenset(x.casefold() for x in [
    "drive_c", "dosdevices", "Games", "users", "Public", "Default", "steamuser", "windows",
    "system32", "syswow64", "Program Files", "Program Files (x86)", "ProgramData", "AppData",
    "Local", "LocalLow", "Roaming", "Temp", "Start Menu", "Programs", "Desktop", "Documents",
    "Saved Games", "Microsoft", "Fonts", "Application Data", "Local Settings", "Common Files",
    "PREFIX", "{app}", "{autopf}", "{pf}", "{pf32}", "{pf64}", "{commonpf}", "{tmp}", "{src}",
    "{win}", "{sys}", "{group}", "{userappdata}", "{commonappdata}", "{localappdata}",
    "{userdocs}", "{commondocs}", "{userdesktop}", "{commondesktop}", "{autodesktop}",
    "{autoprograms}", "{userprograms}", "{commonprograms}", "{fonts}", "{syswow64}"])
REG_KEEP = frozenset(x.casefold() for x in [
    "Software", "Classes", "Microsoft", "Windows", "CurrentVersion", "Uninstall", "Run",
    "RunOnce", "App Paths", "Wow6432Node", "Environment", "Explorer", "Shell Folders",
    "User Shell Folders", "Policies", "System", "CurrentControlSet", "Control", "Services",
    "Fonts", "Windows NT"])
KEEP_FILES = frozenset(x.casefold() for x in [
    "system.reg", "user.reg", "userdef.reg", "winedbg.exe", "wineboot.exe", "winemenubuilder.exe",
    "wineserver", "explorer.exe", "services.exe", "rpcss.exe", "plugplay.exe", "svchost.exe",
    "winedevice.exe", "conhost.exe", "start.exe", "rundll32.exe", "setup.tmp", "gitfurl.desktop",
    "view.json", "arc.ini", "os-release", "meminfo"])
KEEP_QUOTED = (re.compile(r"(?:My )?Inno Setup(?: Extensions)? Setup Data \(\d+(?:\.\d+)+[A-Za-z]?\)"
                          r"(?: \([uU]\))?"),
               re.compile(r"i1\.2\.10--(?:16|32)"),
               re.compile(r"Program Error|Wine Mono Installer|Wine Gecko Installer|ArC"))
STAGES = ("storing", "lzma", "exe", "x86", "delta", "rep", "4x4", "srep", "cm", "rolz")
TOOLS = ("wine", "wine64", "umu-run", "bwrap", "systemd-run", "wineserver", "wineboot", "env",
         "clamscan")
ENV_KEEP_VALUE = frozenset(["WINEDLLOVERRIDES", "WINEDEBUG", "GAMEID", "TMPDIR"])
ENV_PATHS = {"WINEPREFIX": "<prefix-path>"}
SINGLE = frozenset(["installer", "installer-folder", "app", "app-version", "app-folder",
                    "publisher", "prefix", "user", "host", "machine-id", "real-name"])

CATEGORY = {  # placeholder kind -> what --show-removed calls it
    "installer": "software names", "archive": "software names", "name": "software names",
    "app": "software names", "app-version": "software names", "app-folder": "software names",
    "publisher": "software names", "menu-entry": "software names",
    "file": "file and folder names", "folder": "file and folder names",
    "key": "registry keys and values", "value": "registry keys and values",
    "text": "quoted texts and window titles", "prefix": "prefix names",
    "user": "user and machine", "real-name": "user and machine", "host": "user and machine",
    "machine-id": "user and machine", "uid": "user and machine",
    "path": "paths", "home": "paths", "installer-folder": "paths",
    "url": "URLs and addresses", "email": "URLs and addresses", "ip": "URLs and addresses",
    "mac": "URLs and addresses", "guid": "IDs and long strings", "hex": "IDs and long strings",
    "base64": "IDs and long strings", "crc": "checksums", "date": "dates and times",
    "time": "dates and times", "tz": "dates and times", "env": "environment values",
    "size": "sizes rounded", "wine-output": "lines of Wine's own output left out",
    "event": "detailed log fields left out",
}

PLACEHOLDER = re.compile(r"<[a-z][a-z0-9]*(?:-[a-z0-9]+)*>")
_FILE_RE = re.compile(r"(?P<p>.+)-(?P<s>\d{8}-\d{6})(?:-(?P<n>\d))?"
                      r"(?P<k>\.delete\.log|\.timeline\.txt|\.events\.jsonl|\.log|-system\.reg"
                      r"|-user\.reg)")
_TL_ROW = re.compile(r"^(\d{2}):(\d{2}):(\d{2})  (.*)$")


class ReportRefused(Exception):
    """No report was made; the message says why (never with the name that was left)."""


@dataclass
class Report:
    text: str
    removed: dict[str, int] = field(default_factory=dict)  # category -> how many
    runs: int = 0

    def removed_lines(self) -> list[str]:
        if not self.removed:
            return ["removed: nothing"]
        return ["removed (categories and counts only, never the values):",
                *(f"  {cat}: {n}" for cat, n in sorted(self.removed.items()))]


# --- finding a run's files ---------------------------------------------------------------------

@dataclass
class RunFiles:
    prefix: str | None
    stamp: str | None
    log: Path | None = None
    timeline: Path | None = None
    events: Path | None = None
    delete: Path | None = None
    hives: int = 0  # registry copies made before the setup entries (counted, never read)
    other: list[Path] = field(default_factory=list)  # a file given directly, not named as ours

    def key(self):
        return (self.stamp or "", self.prefix or "", tuple(map(str, self.other)))


def _name_parts(name: str):
    m = _FILE_RE.fullmatch(name)
    if m is None:
        return None
    return m["p"], m["s"] + (f"-{m['n']}" if m["n"] else ""), m["k"]


def find_runs(log_dir: Path, prefix: str | None = None) -> list[RunFiles]:
    """gitfurl's files in `log_dir`, grouped by run (prefix and stamp), oldest first. Reads
    names and file types only; links are left out."""
    runs: dict[tuple[str, str], RunFiles] = {}
    try:
        names = os.listdir(log_dir)
    except OSError:
        return []
    for name in names:
        parts = _name_parts(name)
        if parts is None or (prefix is not None and parts[0] != prefix):
            continue
        path = Path(log_dir) / name
        if path.is_symlink() or not path.is_file():
            continue
        p, stamp, kind = parts
        run = runs.setdefault((p, stamp), RunFiles(p, stamp))
        attr = {".log": "log", ".timeline.txt": "timeline", ".events.jsonl": "events",
                ".delete.log": "delete"}.get(kind)
        if attr:
            setattr(run, attr, path)
        else:
            run.hives += 1
    return sorted(runs.values(), key=RunFiles.key)


def run_of(path: Path) -> RunFiles:
    """The run a file belongs to (the files beside it with the same prefix and stamp), or the
    file alone when its name is not one of gitfurl's."""
    path = Path(path)
    parts = _name_parts(path.name)
    if parts is not None:
        for run in find_runs(path.parent, parts[0]):
            if run.stamp == parts[1]:
                return run
    return RunFiles(parts[0] if parts else None, parts[1] if parts else None, other=[path])


def _read(path: Path) -> str:
    """A file of ours, never through a link; at most READ_MAX bytes (its start and its end)."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise OSError("not a regular file")
        if st.st_size <= READ_MAX:
            data = fh.read(READ_MAX)
        else:
            head = fh.read(HEAD_KEEP)
            fh.seek(st.st_size - (READ_MAX - HEAD_KEEP))
            data = head + b"\n--- (part of the file was not read)\n" + fh.read(READ_MAX)
    # Printable text only: no terminal escapes or direction overrides reach the report.
    return _CONTROL.sub("?", data.decode("utf-8", errors="replace"))


_CONTROL = re.compile("[\x00-\x08\x0b-\x1f\x7f-\x9f‎‏‪-‮⁦-⁩]")


# --- sizes -------------------------------------------------------------------------------------

def _ext(name: str) -> str:
    low = name.casefold()
    for e in KEEP_EXT:
        if low.endswith(e) and len(low) > len(e):
            return e
    return ""


def _is_generic(name: str) -> bool:
    low = name.casefold().strip()
    stem = low.rsplit(".", 1)[0] if "." in low else low
    return low in GENERIC or stem in GENERIC or low in KEEP_FILES or len(low) < 3


def _sig2(value: float) -> str:
    """Two significant figures: 9.4, 47, 120."""
    if value == 0:
        return "0"
    digits = 1 - int(math.floor(math.log10(abs(value))))
    v = round(value, digits)
    return f"{v:.{digits}f}" if digits > 0 else f"{int(v)}"


_UNITS = {"b": 1, "byte": 1, "bytes": 1, "kb": 1e3, "mb": 1e6, "gb": 1e9, "tb": 1e12,
          "kib": 1 << 10, "mib": 1 << 20, "gib": 1 << 30, "tib": 1 << 40}


def rounded_size(n: float) -> str:
    """A byte count as 'about 9.4 GB' (two significant figures, powers of ten)."""
    for unit, scale in (("TB", 1e12), ("GB", 1e9), ("MB", 1e6), ("KB", 1e3)):
        if n >= scale:
            return f"about {_sig2(n / scale)} {unit}"
    return f"about {_sig2(n)} bytes"


# --- the patterns of the second pass -----------------------------------------------------------

_WORD = re.compile(r"(?<![\w.\-+~])\w[\w.\-+~]*(?![\w\-+~])")
_URL = re.compile(r"\b(?:https?|ftps?|file|smb|ssh|git)://[^\s'\"<>]+|\bwww\.[^\s'\"<>]+",
                  re.IGNORECASE)
_EMAIL = re.compile(r"\b[\w.+\-]+@[\w\-]+(?:\.[\w\-]+)+\b")
_GUID = re.compile(r"\{?\b[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
                   r"[0-9A-Fa-f]{12}\b\}?")
_MAC = re.compile(r"\b[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}\b")
_ISO = re.compile(r"\b(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?"
                  r"(?:Z|[+-]\d{2}:?\d{2})?)(?![\d:])")
_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}(?:[ T]\d{2}:\d{2}(?::\d{2})?)?\b")
_STAMP = re.compile(r"\b\d{8}-\d{6}(?:-\d)?\b")
_TZ = re.compile(r"\b(?:Africa|America|Antarctica|Asia|Atlantic|Australia|Europe|Indian|Pacific|"
                 r"Etc|US|Canada|Brazil|Mexico)/[A-Za-z_\-]+(?:/[A-Za-z_\-]+)?\b"
                 r"|\b(?:UTC|GMT)[+-]\d{1,2}(?::?\d{2})?\b")
_IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?:/\d{1,2})?(?![\w.])")
_IPV6 = re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}(?![\w:])"
                   r"|(?<![\w:])(?=[0-9A-Fa-f:]*[0-9A-Fa-f])(?:[0-9A-Fa-f]{1,4}:)*"
                   r"[0-9A-Fa-f]{0,4}::(?:[0-9A-Fa-f]{1,4}:)*[0-9A-Fa-f]{0,4}(?![\w:])")
_CLOCK = re.compile(r"(?<![\d:+\-])\b\d{1,2}:\d{2}:\d{2}\b(?!:)")
_UID = re.compile(r"((?:\buid|\bgid|\buser-|\buser@|/run/user/|\.wine-)[=: ]?)(\d+)")
_CRC_CTX = re.compile(r"(\b(?:stored|computed|crc(?:-?32)?|checksum)[:= ]+(?:0x)?)"
                      r"[0-9A-Fa-f]{8}\b", re.IGNORECASE)
_SHA = re.compile(r"\b(?:[0-9A-Fa-f]{64}|[0-9A-Fa-f]{40}|[0-9A-Fa-f]{32})\b")
_HEX = re.compile(r"\b(?:0x)?[0-9A-Fa-f]{16,}\b")
_CRC8 = re.compile(r"\b(?:0x)?(?=[0-9A-Fa-f]*\d)(?=[0-9A-Fa-f]*[A-Fa-f])[0-9A-Fa-f]{8}\b")
_B64 = re.compile(r"(?<![\w+/])(?=[A-Za-z0-9+/]*\d)(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*[A-Z])"
                  r"[A-Za-z0-9+/]{24,}={0,2}(?![\w+/])")
_REG = re.compile(r"\b(HKEY_[A-Z_]+|HKLM|HKCU|HKCR|HKU|HKCC|HKA)\\"
                  r"((?:[^\\\s'\"()=<>]+(?: [^\\\s'\"()=<>]+)*\\)*[^\\\s'\"()=<>]*)")
_REGVAL = re.compile(r"((?:\(default\)|<[a-z0-9-]+>|'[^']*') = [a-z]+ ).+$")
_WP = r"(?:<[a-z][a-z0-9-]*>|[^\\\s'\"<>|*?;,()])"  # a character of a part, or a placeholder
_WINPATH = re.compile(r"((?<!\w)[A-Za-z]:\\|\{[a-z0-9]+\}\\)"
                      rf"((?:{_WP}+(?: {_WP}+)*\\)*{_WP}*)")
_DESKTOP = re.compile(r"(gitfurl-(?:<prefix>|[\w\-]+)-)([^\s/'\"]+?)\.desktop\b")
_ABS = re.compile(r"(?<![\w.~<>\-])/[^\s'\"<>(),;]+")
_UNDER = re.compile(r"(<(?:prefix-path|prefixes|logs|applications|staging|installer-folder)>"
                    r"|(?<![\w<>])~)(/[^\s'\"(),;]*)")
_RELPATH = re.compile(r"(?<![\w./\\<>~\-])(?:[\w.\-+~]+/)+(?:[\w.\-+~]*\.[A-Za-z]\w{0,5}"
                      r"|<[a-z][a-z0-9-]*>(?:\.[A-Za-z]\w{0,5})?)(?![\w/])")
_FILENAME = re.compile(r"(?<![\w.<>\-+~])\w[\w\-+~]*(?:\.[\w\-+~]+)*"
                       r"\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{2,5}\b(?![\w\-(])")
_QUOTED = re.compile(r"(?<!\w)([\"'])((?:(?!\1).){1,300}?)\1(?!\w)")
_CMDLIKE = re.compile(r"(?:gitfurl|wine|wineboot|wineserver)(?: [\w\-=<>.,+]+)*")
_SIZE = re.compile(r"(?<![\w.])(\d[\d,]*(?:\.\d+)?)\s?(bytes|byte|B|KB|kB|MB|GB|TB|KiB|MiB|GiB|"
                   r"TiB)(?![A-Za-z])")
_OFFSET = re.compile(r"\bbyte offset (\d+)\b")
_MIXED = re.compile(r"(?:gitfurl-)?(?:<[a-z][a-z0-9-]*>-?)+(?:\.[A-Za-z]{2,7})?")
_HOME_KEEP = frozenset(["/.wine", "/.local/share/applications", "/.config/gitfurl",
                        "/.local/share/gitfurl", "/.local/state/gitfurl"])
_SLASH_WORDS = frozenset(["wine/proton", "wine/proton/umu", "and/or"])
_DOTTED_WORDS = frozenset(["e.g", "i.e"])
_SYSTEM_PATHS = ("/proc/", "/sys/fs/cgroup", "/etc/os-release", "/tmp/.X11-unix",
                 "/tmp/.gitfurl-xauthority")


def _kind_of(token: str) -> str:
    m = re.match(r"<([a-z][a-z-]*?)(?:-\d+)?>", token)
    return m.group(1) if m else "name"


class Scrubber:
    """Replaces names by placeholders that stay the same in one report, and counts what it
    removed by category (never the values)."""

    def __init__(self, exact_sizes: bool = False):
        self.exact = exact_sizes
        self.tokens: dict[tuple[str, str], str] = {}
        self.numbers: Counter = Counter()
        self.removed: Counter = Counter()
        self.phrases: dict[str, str] = {}  # casefold name -> placeholder (matched anywhere)
        self.words: dict[str, str] = {}  # casefold token -> placeholder (many file names)
        self.check: dict[str, str] = {}  # casefold name -> its category (for the check)
        self.roots: list[tuple[str, str]] = []  # (absolute path, placeholder), longest first
        self.origin: datetime | None = None
        self._phrase_re = None

    # ---- placeholders ----
    def token(self, kind: str, key: str, name: str = "") -> str:
        k = (kind, key.casefold())
        if k not in self.tokens:
            self.numbers[kind] += 1
            n = self.numbers[kind]
            self.tokens[k] = f"<{kind}>" if kind in SINGLE and n == 1 else f"<{kind}-{n}>"
        return self.tokens[k] + _ext(name)

    def file_token(self, kind: str, name: str) -> str:
        """A file's placeholder, keyed by its name without a kept extension, which is added
        back (`<file-3>.dll`): `Game` and `Game.exe` share one number."""
        ext = _ext(name)
        return self.token(kind, name[:-len(ext)] if ext else name, name)

    def count(self, kind: str, n: int = 1) -> None:
        self.removed[CATEGORY.get(kind, kind)] += n

    # ---- known names ----
    def known(self, name: str, kind: str, many: bool = False) -> None:
        """A name from the run's own data. `many`: one of possibly thousands of file names;
        without spaces it is matched as a whole token only."""
        name = (name or "").strip()
        if not name or len(name) > 400:
            return
        if kind not in ("prefix", "user", "host", "machine-id", "installer") \
                and _is_generic(name):
            return
        if kind in ("file", "folder", "archive", "installer", "menu-entry"):
            ext = _ext(name)
            stem = name[:-len(ext)] if ext else name
            token = self.file_token(kind, name)
            if ext and not _is_generic(stem):
                self._add(stem, self.token(kind, stem), many)
        else:
            token = self.token(kind, name)
        spaced = bool(re.search(r"[^\w.\-+~]", name))
        if many and spaced and len(self.phrases) > PHRASES_MAX:
            return
        self._add(name, token, many and not spaced)
        if not many:
            self.check.setdefault(name.casefold(), CATEGORY.get(kind, kind))

    def _add(self, name: str, token: str, many: bool) -> None:
        key = name.casefold()
        if many:
            self.words.setdefault(key, token)
        elif key not in self.phrases:
            self.phrases[key] = token
            self._phrase_re = None

    def known_path(self, path: str, kind: str = "file") -> None:
        """A relative or absolute path from the run: every part is a known name."""
        parts = [p for p in re.split(r"[\\/]", path) if p]
        for i, part in enumerate(parts):
            self.known(part, kind if i == len(parts) - 1 else "folder", many=True)

    def root(self, path, placeholder: str) -> None:
        """An absolute folder shown as a placeholder (gitfurl's own folders, the prefix)."""
        p = str(path).rstrip("/")
        if p and p.startswith("/") and p != "/" and all(r != p for r, _ in self.roots):
            self.roots.append((p, placeholder))
            self.roots.sort(key=lambda r: -len(r[0]))

    # ---- the passes ----
    def scrub(self, text: str) -> str:
        return self.generic_pass(self.known_pass(text))

    def known_pass(self, text: str) -> str:
        text = self._roots(text)
        if self.phrases:
            if self._phrase_re is None:
                alts = sorted(self.phrases, key=len, reverse=True)
                self._phrase_re = re.compile(
                    r"(?<![A-Za-z0-9])(?:" + "|".join(re.escape(a) for a in alts)
                    + r")(?![A-Za-z0-9])", re.IGNORECASE)

            def phrase(m):
                tok = self.phrases[m.group(0).casefold()]
                self.count(_kind_of(tok))
                return tok
            text = self._outside(self._phrase_re, phrase, text)
        if self.words:
            def word(m):
                w, tail = m.group(0), ""
                if w.casefold() not in self.words:
                    bare = w.rstrip(".")
                    w, tail = bare, w[len(bare):]
                tok = self.words.get(w.casefold())
                if tok is None:
                    return m.group(0)
                self.count(_kind_of(tok))
                return tok + tail
            text = self._outside(_WORD, word, text)
        return text

    def _roots(self, text: str) -> str:
        for path, tok in self.roots:
            if path in text:
                text, n = re.subn(re.escape(path) + r"(?![^\s/'\"),;:.])", tok, text)
                self.count("home" if tok == "~" else "path", n)
        return text

    @staticmethod
    def _outside(rx, repl, text: str) -> str:
        """rx.sub(repl) on the parts of `text` that are not placeholders."""
        out, pos = [], 0
        for m in PLACEHOLDER.finditer(text):
            out.append(rx.sub(repl, text[pos:m.start()]))
            out.append(m.group(0))
            pos = m.end()
        out.append(rx.sub(repl, text[pos:]))
        return "".join(out)

    def generic_pass(self, text: str) -> str:
        sub = self._outside
        text = sub(_URL, lambda m: self._put("url"), text)
        text = sub(_EMAIL, lambda m: self._put("email"), text)
        text = sub(_GUID, lambda m: self._put("guid"), text)
        text = sub(_MAC, lambda m: self._put("mac"), text)
        text = sub(_ISO, self._iso, text)
        text = sub(_DATE, lambda m: self._put("date"), text)
        text = sub(_STAMP, lambda m: self._put("date", "<stamp>"), text)
        text = sub(_TZ, lambda m: self._put("tz"), text)
        text = sub(_IPV4, lambda m: self._put("ip"), text)
        text = sub(_IPV6, lambda m: self._put("ip"), text)
        text = sub(_CLOCK, lambda m: self._put("time"), text)
        text = sub(_UID, lambda m: m.group(1) + self._put("uid"), text)
        text = sub(_CRC_CTX, lambda m: m.group(1) + self._put("crc"), text)
        text = sub(_SHA, lambda m: self._put("crc"), text)
        text = sub(_HEX, lambda m: self._put("hex"), text)
        text = sub(_CRC8, lambda m: self._put("crc"), text)
        text = sub(_B64, lambda m: self._put("base64"), text)
        text = _REG.sub(self._regkey, text)
        text = sub(_REGVAL, lambda m: m.group(1) + self._put("value", "<value>"), text)
        # These see the placeholders around them (their look-behinds skip them).
        text = _WINPATH.sub(self._winpath, text)
        text = _DESKTOP.sub(self._desktop, text)
        text = _ABS.sub(self._abspath, text)
        text = _UNDER.sub(self._under, text)
        text = _RELPATH.sub(self._relpath, text)
        text = _FILENAME.sub(self._filename, text)
        text = _QUOTED.sub(self._quoted, text)
        if not self.exact:
            text = sub(_SIZE, self._size, text)
            text = sub(_OFFSET, lambda m: "byte offset "
                       + rounded_size(int(m.group(1))).replace(" bytes", ""), text)
        return text

    def _put(self, kind: str, token: str | None = None) -> str:
        self.count(kind)
        return token or f"<{kind}>"

    def _iso(self, m) -> str:
        self.count("date")
        try:
            when = datetime.fromisoformat(m.group(1).replace("Z", "+00:00"))
        except ValueError:
            return "<date>"
        when = when.replace(tzinfo=None)
        return self.relative(when) if self.origin is not None else "<date>"

    def relative(self, when: datetime) -> str:
        delta = (when - self.origin).total_seconds()
        s = int(round(abs(delta)))
        return f"{'-' if delta < 0 else '+'}{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"

    def _quoted(self, m) -> str:
        q, inner = m.group(1), m.group(2)
        if not inner.strip() or any(rx.fullmatch(inner) for rx in KEEP_QUOTED) \
                or _CMDLIKE.fullmatch(inner):
            return m.group(0)
        if all(self._safe_part(p) for p in re.split(r"[\\/]", inner)):
            return m.group(0)  # a path whose every part is a placeholder or kept already
        self.count("text")
        return q + self.token("text", inner) + q

    @staticmethod
    def _safe_part(part: str) -> bool:
        if re.fullmatch(r"[A-Za-z]:|\.{1,2}", part):
            return True
        p = part.rstrip(".,;:")
        return (not p or p.casefold() in KEEP_DIRS or p.casefold() in KEEP_FILES
                or bool(_MIXED.fullmatch(p)))

    def _part(self, part: str, keep=KEEP_DIRS, kind: str = "folder") -> str:
        if re.fullmatch(r"[A-Za-z]:|\.{1,2}", part):
            return part
        bare = part.rstrip(".,;:")
        trail = part[len(bare):]
        if not bare or bare.casefold() in keep or bare.casefold() in KEEP_FILES \
                or _MIXED.fullmatch(bare):
            return part
        if kind == "key":
            self.count("key")
            return self.token("key", bare) + trail
        kind = "file" if "." in bare.strip(".") else "folder"
        self.count(kind)
        return self.file_token(kind, bare) + trail

    def _user_parts(self, parts: list[str]) -> list[str]:
        out = []
        for i, p in enumerate(parts):
            if i > 0 and parts[i - 1].casefold() == "users" and p \
                    and p.casefold() not in ("public", "default", "steamuser") \
                    and not PLACEHOLDER.fullmatch(p):
                self.count("user")
                out.append("<user>")
            else:
                out.append(self._part(p))
        return out

    def _regkey(self, m) -> str:
        parts = m.group(2).split("\\")
        return m.group(1) + "\\" + "\\".join(self._part(p, REG_KEEP, "key") for p in parts)

    def _winpath(self, m) -> str:
        return m.group(1) + "\\".join(self._user_parts(m.group(2).split("\\")))

    def _abspath(self, m) -> str:
        path, trail = m.group(0), ""
        while path and path[-1] in ".,;:)":
            path, trail = path[:-1], path[-1] + trail
        if path.startswith(_SYSTEM_PATHS) or path in ("/", "/tmp"):
            return path + trail
        switch = re.fullmatch(r"(/[A-Z][A-Z0-9]*)(=.*)?", path)  # an Inno Setup switch
        if switch:
            value = switch.group(2) or ""
            if value[1:].startswith("/"):
                self.count("path")
                value = "=<path>"
            return switch.group(1) + value + trail
        base = path.rsplit("/", 1)[-1]
        self.count("path")
        return (f"<path>/{base}" if base in TOOLS else "<path>") + trail

    def _under(self, m) -> str:
        """The rest of a path below a placeholder for one of gitfurl's folders or the prefix."""
        head, tail = m.group(1), m.group(2)
        if head == "~":
            bare = tail.rstrip(".,;:")
            if bare in _HOME_KEEP:
                return m.group(0)
            self.count("path")
            return "~/<path>" + tail[len(bare):]
        return head + "/".join(self._user_parts(tail.split("/")))

    def _relpath(self, m) -> str:
        path = m.group(0)
        if path.casefold() in _SLASH_WORDS or path.startswith("~/"):
            return path
        return "/".join(self._part(p) for p in path.split("/"))

    def _desktop(self, m) -> str:
        if PLACEHOLDER.fullmatch(m.group(2)):
            return m.group(0)
        self.count("menu-entry")
        return m.group(1) + self.token("menu-entry", m.group(2)) + ".desktop"

    def _filename(self, m) -> str:
        name = m.group(0)
        if name.casefold() in KEEP_FILES or name.casefold() in _DOTTED_WORDS:
            return name
        self.count("file")
        return self.file_token("file", name)

    def _size(self, m) -> str:
        num, unit = m.group(1), m.group(2)
        try:
            value = float(num.replace(",", ""))
        except ValueError:
            return m.group(0)
        self.count("size")
        if unit.casefold() in ("kib", "mib", "gib", "tib"):
            return f"about {_sig2(value)} {unit}"
        return rounded_size(value * _UNITS[unit.casefold()])

    def size(self, n) -> str:
        """A byte count from the detailed log."""
        if n is None:
            return "none"
        if self.exact:
            return f"{n} bytes"
        self.count("size")
        return rounded_size(float(n))

    # ---- the check ----
    def leaks(self, lines) -> list[str]:
        """The categories of known names still in the scrubbed lines (placeholders are left
        out of the search)."""
        bare = PLACEHOLDER.sub(" ", "\n".join(lines))
        found = set()
        for name, what in self.check.items():
            if re.search(r"(?<![A-Za-z0-9])" + re.escape(name) + r"(?![A-Za-z0-9])", bare,
                         re.IGNORECASE):
                found.add(what)
        return sorted(found)


# --- the user and the machine ------------------------------------------------------------------

def os_release(path: str = "/etc/os-release") -> dict[str, str]:
    """ID and VERSION_ID from os-release, nothing else."""
    out: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return out
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        value = value.strip().strip("\"'")
        if sep and key in ("ID", "VERSION_ID") and re.fullmatch(r"[\w.\-]{1,40}", value):
            out[key] = value
    return out


def machine_names(env: dict | None = None, os_release_path: str = "/etc/os-release"
                  ) -> dict[str, list[str]]:
    """Your user name, real name, home folder, host name and machine ID, to be removed. Read
    from this machine only (getpass, pwd, the host name, /etc/machine-id); nothing is sent."""
    import getpass
    import socket

    env = os.environ if env is None else env
    out: dict[str, list[str]] = {"user": [], "real-name": [], "home": [], "host": [],
                                 "machine-id": []}
    users = [env.get("USER"), env.get("LOGNAME")]
    try:
        users.append(getpass.getuser())
    except Exception:  # noqa: BLE001 - getpass raises when nothing names the user
        pass
    try:
        import pwd

        pw = pwd.getpwuid(os.getuid())
        users.append(pw.pw_name)
        gecos = pw.pw_gecos.split(",")[0].strip()
        if gecos:
            out["real-name"] += [gecos, *(w for w in gecos.split() if len(w) >= 3)]
        out["home"].append(pw.pw_dir)
    except (ImportError, KeyError, OSError):
        pass
    out["user"] = [u for u in users if u]
    out["home"] += [h for h in (env.get("HOME"), str(Path.home())) if h]
    hosts = set()
    for get in (socket.gethostname, platform.node):
        try:
            hosts.add(get())
        except OSError:
            pass
    try:
        hosts.add(Path("/etc/hostname").read_text(encoding="utf-8", errors="replace").strip())
    except OSError:
        pass
    distro = os_release(os_release_path).get("ID", "").casefold()
    for h in hosts:
        for part in {h, h.split(".")[0]}:
            # a host still called after the distribution (its default) names nothing more
            if part and part.casefold() not in ("localhost", "localhost.localdomain", distro):
                out["host"].append(part)
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            mid = Path(p).read_text(encoding="ascii", errors="replace").strip()
        except OSError:
            continue
        if mid:
            out["machine-id"].append(mid)
    return {k: list(dict.fromkeys(v)) for k, v in out.items()}


def environment(wine_version: str | None = None, umu: bool | None = None,
                os_release_path: str = "/etc/os-release") -> list[str]:
    """The environment lines (this machine, when the report is made)."""
    from gitfurl import farc_codecs

    rel = os_release(os_release_path)
    distro = " ".join(x for x in (rel.get("ID"), rel.get("VERSION_ID")) if x) or "not known"
    native = "not present"
    if farc_codecs.cm_available():
        native = "present (cm stage" + (", rolz" if farc_codecs.rolz_native() else "") + ")"
    m = re.match(r"wine-[\w.\-]+(?: \([\w .\-]+\))?", wine_version or "")
    lines = [f"gitfurl: {__version__}",
             f"python: {platform.python_version()}",
             f"os: {platform.system() or 'not known'}; distribution: {distro}",
             f"architecture: {platform.machine() or 'not known'}",
             f"native module: {native}",
             f"wine: {m.group(0) if m else 'not found or not checked'}"]
    if umu is not None:
        lines.append(f"umu-run: {'on PATH' if umu else 'not found'}")
    return lines


# --- what a run's files say --------------------------------------------------------------------

@dataclass
class _Facts:
    route: str | None = None
    reason: str | None = None
    chosen_by: str | None = None
    methods: list[str] = field(default_factory=list)
    inno: str | None = None
    runtime: str | None = None
    options: list[str] = field(default_factory=list)
    status: str | None = None
    crc: Counter = field(default_factory=Counter)
    counts: dict[str, str] = field(default_factory=dict)

    def option(self, name: str) -> None:
        if name not in self.options:
            self.options.append(name)


REASONS = (  # (words in the route's reasons, the category shown)
    ("you chose", "you chose the route"),
    ("options that only a Setup run uses", "options only a Setup run uses"),
    ("not recognised as either", "not an Inno Setup installer or a FreeArc archive"),
    ("no FreeArc archives beside", "no FreeArc archives beside the installer"),
    ("does not handle", "a method gitfurl does not handle"),
    ("CRC", "an archive whose checksums do not match"),
    ("cannot be unpacked by gitfurl", "archives gitfurl cannot unpack"),
    ("not linked", "archives not linked to the installer"),
    ("could not be read", "the file could not be read"),
    ("gitfurl can unpack them itself", "every archive uses methods gitfurl decodes"),
)


def _reason_category(text: str) -> str:
    return next((cat for words, cat in REASONS if words in text), "other")


def _collect_from_log(s: Scrubber, text: str, facts: _Facts) -> list[str]:
    """Known names from the log's own lines, and the facts they give. Returns the absolute
    paths of the installer and archives, so their headers can be read."""
    sources: list[str] = []
    in_entries = False
    for line in text.splitlines():
        body = line[4:] if line.startswith("--- ") else line
        key, sep, value = line.partition(": ")
        if sep and key in ("installer", "archive"):
            p = Path(value)
            s.root(p.parent, "<installer-folder>")
            s.known(p.name, key)
            s.known(p.parent.name, "installer-folder")
            sources.append(value)
            base = re.sub(r"[-_.]?\d+$", "", p.stem)
            if key == "archive" and base != p.stem:
                s.known(base, "name")  # the series' own name (NAME in NAME-01.bin)
        elif sep and key == "cwd":
            s.root(value, "<installer-folder>")
            s.known(Path(value).name, "installer-folder")
        elif sep and key == "prefix":
            s.root(value, "<prefix-path>")
        elif sep and key == "destination":
            m = re.match(r".*? \((/.*)\)$", value)
            if m:
                s.root(m.group(1), "<prefix-path>/drive_c/Games/<prefix>")
        elif sep and key == "command":
            try:
                argv = shlex.split(value)
            except ValueError:
                argv = value.split()
            for a in argv:
                if a.startswith("/") and Path(a).suffix.casefold() in (".exe", ".msi"):
                    s.root(Path(a).parent, "<installer-folder>")
                    s.known(Path(a).name, "installer")
                    s.known(Path(a).parent.name, "installer-folder")
                    sources.append(a)
            names = [Path(a).name for a in argv]
            facts.runtime = "proton (umu-run)" if "umu-run" in names else "wine"
            if "bwrap" in names:
                facts.option("--portable")
            if "systemd-run" in names:
                facts.option("memory limit")
            if any(a.upper().startswith("/VERYSILENT") for a in argv):
                facts.option("--unattended")
            if any(a.startswith("WINEDEBUG") for a in argv):
                facts.option("--wine-debug")
        m = re.match(r"CRC-32 mismatch: (.*): stored [0-9A-Fa-f]{8}, computed [0-9A-Fa-f]{8}; "
                     r"(.*)$", body)
        if m:
            s.known_path(m.group(1))
            facts.crc[m.group(2)] += 1
        m = re.match(r"\s+(\S.*?) \(\d[\d.,]* ?\w+\)$", body)  # a program candidate
        if m and ("/" in m.group(1) or "." in m.group(1)):
            s.known_path(m.group(1))
        m = re.match(r"\s+(\S.*\.(?:exe|msi))$", body, re.IGNORECASE)  # a redistributable
        if m:
            s.known_path(m.group(1))
        m = re.search(r"(?:\(removed again\)|\(damaged, still there\)|incomplete file "
                      r"\(kept\)): (.*)$", body)
        if m:
            for name in m.group(1).split(", "):
                if name != "...":
                    s.known_path(name)
        m = re.search(r"\(--keep-bad-file-crc\): (.*)$", body)
        if m:
            facts.option("--keep-bad-file-crc")
            for name in re.findall(r"(?:^|, )(.+?) \(stored", m.group(1)):
                s.known_path(name)
        m = re.match(r"note: every one of the .* in (.+) failed its CRC-32 check", body)
        if m:
            s.known(m.group(1), "archive")
        if body.startswith("setup entries"):
            in_entries = True
        m = re.search(r"menu entry: wrote (\S+) \((.*)\)$", body) if in_entries else None
        if m:
            s.known(m.group(2), "menu-entry")
        if line.startswith("scan: "):
            facts.option("--scan")
        if line.startswith("route: native"):
            facts.route = facts.route or "native"
        m = re.match(r"--- exit status (-?\d+)|--- cancelled by user \(process exit status "
                     r"(-?\d+)\)", line)
        if m:
            facts.status = m.group(1) or f"{m.group(2)} (stopped by you)"
    if in_entries:
        facts.option("--apply-setup-entries")
    return sources


def _collect_from_timeline(s: Scrubber, text: str, facts: _Facts) -> None:
    for line in text.splitlines():
        m = _TL_ROW.match(line)
        what, _sep, why = (m.group(4) if m else line).partition(" — ")
        r = re.match(r"Route: (\w+) \(.*?; (you|gitfurl) chose it\)", what)
        if r:
            facts.route, facts.chosen_by = r.group(1), r.group(2)
            facts.reason = _reason_category(why)
        if what == "Reason for the route" and facts.reason in (None, "other"):
            facts.reason = _reason_category(why)
        o = re.match(r"(?:Opened|Data blocks? [\d\-]+ of \d+ in) (.+?)(?:: |$)", what)
        if o:
            s.known(o.group(1), "archive")
            chain = what.rsplit(": ", 1)[-1] if what.startswith("Data") else ""
            if chain and all(p in STAGES for p in chain.split("+")) \
                    and chain not in facts.methods:
                facts.methods.append(chain)
        a = re.match(r"Archive warning: (.+)$", what)
        if a:
            s.known(a.group(1), "archive")
            if "index" in why:
                facts.option("--accept-bad-index-crc")
        e = re.match(r"Every file of (.+) failed its CRC-32 check", what)
        if e:
            s.known(e.group(1), "archive")
        st = re.match(r"Started Setup \((.+?)\) under (.+?),", what)
        if st:
            s.known(st.group(1), "installer")
            facts.runtime = facts.runtime or ("proton (umu-run)" if "Proton" in st.group(2)
                                              else "wine")
        b = re.search(r"\(large file beside the installer: (.+)\)$", why)
        if b:
            s.known(b.group(1), "archive")
        p = re.match(r"Prefix (\S+): (existing|new|missing)", what)
        if p:
            s.known(p.group(1), "prefix")
            pm = re.search(r"\((/.+)\)$", why)
            if pm:
                s.root(pm.group(1), "<prefix-path>")
        d = re.search(r"a new folder in the prefix \((/.+?)\);", why)
        if d:
            s.root(d.group(1), "<prefix-path>/drive_c/Games/<prefix>")
        if what.startswith("Detected Inno Setup") and facts.inno is None:
            i = re.search(r"Inno Setup Setup Data \((\d+(?:\.\d+)+[A-Za-z]?)\)((?: \([uU]\))?)",
                          why)
            j = re.search(r"setup data format (\d+(?:\.\d+)+[A-Za-z]?)", why)
            facts.inno = (j.group(1) if j else i.group(1) + i.group(2) if i else None)
        if what.startswith("Warning: files whose CRC-32 does not match the archive are KEPT"):
            facts.option("--keep-bad-file-crc")
        if what.startswith("Cleanup"):
            facts.option("--cleanup")
        if "inside bwrap" in why:
            facts.option("--portable")
        if what.startswith(("ClamAV scan", "The ClamAV scan")):
            facts.option("--scan")
        if what.startswith(("Setup entry not applied", "Registry values written",
                            "Menu entries written", "No registry value was written")):
            facts.option("--apply-setup-entries")
        w = re.match(r"(\d+) files written", what)
        if w:
            facts.counts["files written"] = w.group(1)
        if what.startswith("Setup exited with status") and facts.status is None:
            facts.status = what.rsplit(" ", 1)[-1]
        n = re.match(r"(\d+) archives?, (\d+) files", why) \
            if what.startswith("Started the unpack") else None
        if n:
            facts.counts["archives"], facts.counts["files planned"] = n.group(1), n.group(2)


def _collect_from_sources(s: Scrubber, sources: list[str]) -> None:
    """Names beside the installer, in its Inno Setup header and in the archives' own file
    lists, while they are still there. Read-only and bounded; never raises."""
    from gitfurl import farc, inno

    folders, total = set(), 0
    for src in dict.fromkeys(sources):
        p = Path(src)
        if p.parent not in folders:
            folders.add(p.parent)
            try:
                with os.scandir(p.parent) as it:
                    for k, e in enumerate(it):
                        if k >= LISTDIR_MAX:
                            break
                        s.known(e.name, "file", many=True)
            except OSError:
                pass
        try:
            if p.is_symlink() or not p.is_file():
                continue
            fd = os.open(p, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
            with os.fdopen(fd, "rb") as fh:
                head = fh.read(4)
        except OSError:
            continue
        try:
            if head[:2] == b"MZ":
                total += _collect_inno(s, inno.read_setup(p))
            elif head == b"ArC\x01" and total < FILE_NAMES_MAX:
                arc = farc.read_archive(p)
                for e in arc.entries[:FILE_NAMES_MAX - total]:
                    s.known_path(e.path, "folder" if e.is_dir else "file")
                total += len(arc.entries)
        except Exception:  # noqa: BLE001 - a damaged or hostile file must not stop the report
            continue


_INNO_FIELDS = {"app name": "app", "app version name": "app", "uninstall display name": "app",
                "app id": "app", "app mutex": "app", "setup mutex": "app",
                "output base file name": "app", "app version": "app-version",
                "default dir name": "app-folder", "default group name": "app-folder",
                "publisher": "publisher", "copyright": "publisher", "contact": "publisher",
                "default organisation": "publisher", "default user name": "publisher",
                "comments": "publisher", "support phone": "publisher",
                "publisher url": "url", "support url": "url", "updates url": "url",
                "uninstall display icon": "app-folder"}
_CONST = re.compile(r"\{[a-z0-9]+\}", re.IGNORECASE)


def _collect_inno(s: Scrubber, setup) -> int:
    """Known names from an Inno Setup header (gitfurl.inno.Setup)."""
    for key, kind in _INNO_FIELDS.items():
        value = str(setup.strings.get(key, "") or "")
        if not value or (kind == "app-version" and len(value) < 6):
            continue  # a short version ("1.0") names nothing and would hit gitfurl's numbers
        s.known(value, kind)
        if kind == "app-folder":
            for part in re.split(r"[\\/]", _CONST.sub("", value)):
                s.known(part, "app-folder")
    n = 0
    for fe in setup.files[:FILE_NAMES_MAX]:
        for v in (fe.source, fe.dest):
            if v:
                s.known_path(_CONST.sub("", v))
                n += 1
    for d in setup.dirs:
        s.known_path(_CONST.sub("", d.name), "folder")
    for ic in setup.icons:
        s.known(re.split(r"[\\/]", ic.name)[-1], "menu-entry")
        for v in (ic.target, ic.parameters, ic.working_dir, ic.icon_file, ic.comment):
            if v:
                s.known_path(_CONST.sub("", v))
    for r in setup.registry:
        for part in r.subkey.split("\\"):
            if part.casefold() not in REG_KEEP:
                s.known(part, "key")
        for v in (r.value_name, r.value_data):
            if v:
                s.known(v, "value")
    for r in [*setup.run, *setup.uninstall_run]:
        for v in (r.filename, r.parameters, r.working_dir, r.description):
            if v:
                s.known_path(_CONST.sub("", v))
    for c in [*setup.components, *setup.tasks, *setup.types]:
        if c.description:
            s.known(c.description, "text")
    return n


def plan_names(det=None, choice=None) -> list[tuple[str, str]]:
    """Known names from a plan in memory (the window's): the installer, the archives and
    every file they hold. For build_report(names=...)."""
    out: list[tuple[str, str]] = []
    if det is not None and getattr(det, "path", None) is not None:
        out.append((Path(det.path).name, "installer"))
    xplan = getattr(choice, "extract", None) if choice is not None else None
    if xplan is not None:
        from gitfurl.route import written_files

        out += [(ap.path.name, "archive") for ap in xplan.archives]
        for parts, name in written_files(xplan)[:FILE_NAMES_MAX]:
            out.append(("/".join([*parts, name]), "path"))
    return out


# --- the sections of one run -------------------------------------------------------------------

def _origin(run: RunFiles, timeline: str | None) -> datetime | None:
    """The run's start: its first timeline row, dated by the files' stamp."""
    try:
        stamp_dt = datetime.strptime((run.stamp or "")[:15], "%Y%m%d-%H%M%S")
    except ValueError:
        return None
    m = _TL_ROW.match((timeline or "").partition("\n")[0])
    if not m:
        return stamp_dt
    first = stamp_dt.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=int(m.group(3)))
    if first > stamp_dt + timedelta(minutes=1):
        first -= timedelta(days=1)
    return min(first, stamp_dt)


def _timeline_lines(s: Scrubber, text: str) -> list[str]:
    out, last = [], None
    for line in text.splitlines():
        m = _TL_ROW.match(line)
        if not m:
            out.append(s.scrub(line))
            continue
        s.count("time")
        if s.origin is None:
            out.append(f"--:--:--  {s.scrub(m.group(4))}")
            continue
        t = s.origin.replace(hour=int(m.group(1)), minute=int(m.group(2)),
                             second=int(m.group(3)))
        if t < s.origin - timedelta(minutes=1) or (last is not None and
                                                    t < last - timedelta(hours=1)):
            t += timedelta(days=1)
        last = t
        out.append(f"{s.relative(t)}  {s.scrub(m.group(4))}")
    return out


_WINE_MSG = re.compile(r"^(?:[0-9a-f]{4}:)?(?:[0-9a-f]{4}:)?(?:err|fixme|warn|trace):"
                       r"|^(?:wine|wineserver|winedbg|preloader)\b|^Unhandled exception"
                       r"|^Backtrace:", re.IGNORECASE)


def _env_value(s: Scrubber, key: str, value: str) -> str:
    if key in ENV_PATHS:
        s.count("path")
        return ENV_PATHS[key]
    if key in ENV_KEEP_VALUE:
        return s.scrub(value)
    s.count("env")
    m = re.fullmatch(r".*/((?:GE-)?Proton[\w.\-]*)", value) if key == "PROTONPATH" else None
    return f"<path>/{m.group(1)}" if m else "<value>"


def _command_line(s: Scrubber, value: str) -> str:
    """A logged command: each argument scrubbed; environment values only for gitfurl's own
    variables (WINEPREFIX as <prefix-path>, WINEDLLOVERRIDES and WINEDEBUG as they are)."""
    try:
        argv = shlex.split(value)
    except ValueError:
        return s.scrub(value)
    out, i = [], 0
    while i < len(argv):
        a = argv[i]
        if a == "--setenv" and i + 2 < len(argv):
            out += ["--setenv", argv[i + 1], _env_value(s, argv[i + 1], argv[i + 2])]
            i += 3
            continue
        m = re.fullmatch(r"([A-Z_][A-Z0-9_]*)=(.*)", a)
        out.append(f"{m.group(1)}={_env_value(s, m.group(1), m.group(2))}" if m else s.scrub(a))
        i += 1
    return " ".join(out)


def _log_lines(s: Scrubber, text: str) -> list[str]:
    """The log, scrubbed. Of Wine's own output only the last WINE_LINES message lines stay."""
    lines = text.splitlines()
    sep = lines.index("---") if "---" in lines else -1
    out: list[str] = []
    for line in lines[:sep + 1]:
        key, colon, value = line.partition(": ")
        out.append("command: " + _command_line(s, value) if colon and key == "command"
                   else s.scrub(line))
    wine: list[str] = []
    dropped = [0]

    def flush():
        if not wine and not dropped[0]:
            return
        kept = wine[-WINE_LINES:]
        dropped[0] += len(wine) - len(kept)
        out.append(f"(Wine's own output: {len(kept)} message lines kept, {dropped[0]} other "
                   "lines left out)")
        out.extend("  " + s.scrub(w[:500]) for w in kept)
        if dropped[0]:
            s.count("wine-output", dropped[0])
        wine.clear()
        dropped[0] = 0

    for line in lines[sep + 1:]:
        if line.startswith("--- "):
            flush()
            out.append(s.scrub(line))
        elif not line.strip():
            continue
        elif _WINE_MSG.search(line):
            wine.append(line)
        else:
            dropped[0] += 1
    flush()
    return out


# Detailed-log events: only these fields are shown; the rest are counted as left out.
_EVENT_KEEP = {"start": ("route", "files", "total_bytes", "portable", "unattended",
                         "env_overrides"),
               "exit": ("status", "ok", "error", "seconds", "files", "bytes_written",
                        "crc_errors", "crc_kept", "started", "extraction_observed",
                        "observation_measured", "exit_status", "crash_seen_s"),
               "cancelled": ("seconds", "files", "bytes_written", "crc_errors", "crc_kept",
                             "started", "exit_status", "notes"),
               "interrupted": ("stage", "ok", "files"),
               "not_started": ("reason",),
               "crash_handler": ("elapsed_s", "advisory"),
               "possibly_stalled": ("idle_s", "after_s", "newest_window_s", "advisory"),
               "memory_limit": ("memory_high", "memory_max", "reserve", "mem_total",
                                "mem_available"),
               "memory_limit_result": ("oom_kill", "oom_result", "high_events", "changes"),
               "unattended_result": ("log_found", "succeeded", "files", "cancelled"),
               "setup_entries": ()}
_EVENT_SIZES = ("total_bytes", "bytes_written", "memory_high", "memory_max", "reserve",
                "mem_total", "mem_available")


def _event_value(s: Scrubber, key: str, v) -> str:
    if key in _EVENT_SIZES and isinstance(v, (int, float)) and not isinstance(v, bool):
        return s.size(v)
    if key == "env_overrides" and isinstance(v, list):
        return ",".join(str(x) for x in v if re.fullmatch(r"[A-Z_0-9]{1,40}", str(x)))
    if isinstance(v, (bool, int, float)) or v is None:
        return str(v)
    if isinstance(v, list):
        return "[" + "; ".join(s.scrub(str(x)) for x in v) + "]"
    return s.scrub(str(v))


def _event_lines(s: Scrubber, text: str) -> list[str]:
    kinds: Counter = Counter()
    out: list[str] = []
    unread, last_pct = 0, None
    for raw in text.splitlines():
        try:
            ev = json.loads(raw)
        except ValueError:
            ev = None
        if not isinstance(ev, dict):
            unread += 1
            continue
        kind = str(ev.get("event", "?"))
        kinds[kind] += 1
        if kind == "progress":
            last_pct = ev.get("percent") if isinstance(ev.get("percent"), (int, float)) else None
            continue
        keep = _EVENT_KEEP.get(kind)
        if keep is None:
            continue
        parts = [f"{k}={_event_value(s, k, ev[k])}" for k in keep if k in ev]
        if isinstance(ev.get("archives"), list):
            parts.append(f"archives={len(ev['archives'])}")
        if isinstance(ev.get("lines"), list):
            parts.append(f"lines={len(ev['lines'])}")
        left = [k for k in ev if k not in ("t", "event", *keep)]
        if left:
            s.count("event", len(left))
        when = s.scrub(ev["t"]) if isinstance(ev.get("t"), str) else ""
        out.append((f"{when}  " if when else "") + f"{re.sub(r'[^a-z_]', '', kind)}: "
                   + ", ".join(parts))
    summary = "events: " + ", ".join(f"{re.sub(r'[^a-z_?]', '', k)} {n}"
                                     for k, n in sorted(kinds.items()))
    if last_pct is not None:
        summary += f"; last progress {last_pct} %"
    if unread:
        summary += f"; {unread} lines not read"
    return [summary, *out]


# --- the report --------------------------------------------------------------------------------

def _resolve(source, log_dir: Path | None, run: str | None, all_runs: bool) -> list[RunFiles]:
    if isinstance(source, str) and "/" not in source:
        from gitfurl.prefixes import validate_prefix_name
        from gitfurl.runner import logs_root

        validate_prefix_name(source)
        found = find_runs(log_dir or logs_root(), source)
        if run is not None:
            found = [r for r in found if r.stamp == run]
        if not found:
            raise ReportRefused(f"no {'such ' if run else ''}run of the prefix {source} was "
                                "found in gitfurl's log folder")
        return found if all_runs else found[-1:]
    paths = [Path(source)] if isinstance(source, (str, Path)) else [Path(p) for p in source]
    runs: list[RunFiles] = []
    for p in paths:
        if p.is_symlink() or not p.is_file():
            raise ReportRefused(f"{p.name} is not a regular file (links are not followed)")
        r = run_of(p)
        if all(x.key() != r.key() for x in runs):
            runs.append(r)
    return runs


def _read_run(r: RunFiles) -> dict[str, str]:
    files: dict[str, str] = {}
    for kind in ("log", "timeline", "events", "delete"):
        p = getattr(r, kind)
        if p is not None:
            try:
                files[kind] = _read(p)
            except OSError as exc:
                files[kind] = f"--- (this file could not be read: {exc.strerror or exc})"
    for p in r.other:
        try:
            text = _read(p)
        except OSError as exc:
            raise ReportRefused(f"{p.name} could not be read ({exc.strerror or exc})") from exc
        files.setdefault("timeline" if _TL_ROW.match(text.partition("\n")[0]) else "log", text)
    return files


def _facts_lines(facts: _Facts, files: dict, hives: int) -> list[str]:
    """Lines made from fixed words and numbers only (no text from the files)."""
    lines = []
    if facts.route:
        lines.append(f"route: {facts.route}"
                     + (f" ({facts.chosen_by} chose it)" if facts.chosen_by else "")
                     + (f"; reason: {facts.reason}" if facts.reason else ""))
    elif "delete" in files:
        lines.append("route: none (a prefix deletion)")
    if facts.runtime and facts.route != "native":
        lines.append(f"runtime: {facts.runtime}")
    if facts.inno:
        lines.append(f"inno setup data format: {facts.inno}")
    if facts.methods:
        lines.append("freearc stages: " + ", ".join(facts.methods))
    lines.append("options seen in the run: " + (", ".join(facts.options) or "none"))
    lines += [f"{k}: {n}" for k, n in facts.counts.items()]
    if facts.crc:
        lines.append("crc-32 mismatches logged: " + ", ".join(
            f"{n} {what if what in ('kept', 'removed again') else 'other'}"
            for what, n in sorted(facts.crc.items())))
    if facts.status is not None:
        lines.append(f"exit status: {facts.status}")
    read = [w for w, key in (("log", "log"), ("timeline", "timeline"),
                             ("detailed log", "events"), ("delete log", "delete"))
            if key in files]
    lines.append("files read: " + (", ".join(read) or "none")
                 + (f"; registry copies made before the setup entries: {hives} (not read)"
                    if hives else ""))
    return lines


def build_report(source, *, run: str | None = None, all_runs: bool = False,
                 exact_sizes: bool = False, log_dir: Path | None = None,
                 names=(), read_sources: bool = True, wine_version: str | None = None,
                 umu: bool | None = None, machine: dict | None = None, env: dict | None = None,
                 os_release_path: str = "/etc/os-release") -> Report:
    """A scrubbed report.

    `source` is a prefix name (its latest run; the run with stamp `run`; with all_runs every
    run) or a path or list of paths given directly (each brings the other files of its run).
    `names` adds known names as (name, kind) pairs (plan_names() gives them for a plan in
    memory). `machine` replaces machine_names() (tests). Raises ReportRefused when nothing can
    be read, or when a known name is still in the text after scrubbing.
    """
    from gitfurl.desktop import data_home
    from gitfurl.namelinks import staging_root
    from gitfurl.prefixes import prefixes_root
    from gitfurl.runner import logs_root

    env = os.environ if env is None else env
    runs = _resolve(source, Path(log_dir) if log_dir else None, run, all_runs)
    s = Scrubber(exact_sizes)
    me = machine_names(env, os_release_path) if machine is None else machine
    for kind in ("user", "real-name", "host", "machine-id"):
        for name in me.get(kind, []):
            s.known(name, kind)
    for root, tok in ((logs_root(env), "<logs>"), (prefixes_root(env), "<prefixes>"),
                      (Path(data_home(env)) / "applications", "<applications>"),
                      (staging_root(env), "<staging>")):
        s.root(root, tok)
    if log_dir:
        s.root(log_dir, "<logs>")
    for h in me.get("home", []):
        s.root(h, "~")
    texts = [(r, _read_run(r)) for r in runs]
    if not any(files for _r, files in texts):
        raise ReportRefused("the run has no files gitfurl can read")
    # Every name of every run first, so that one name has one placeholder throughout.
    all_facts = []
    for r, files in texts:
        facts = _Facts()
        if r.prefix:
            s.known(r.prefix, "prefix")
            s.root(prefixes_root(env) / r.prefix, "<prefix-path>")
        sources = _collect_from_log(s, files.get("log", ""), facts)
        _collect_from_timeline(s, files.get("timeline", ""), facts)
        dl = files.get("delete", "")
        m = re.match(r"gitfurl: delete the prefix (\S+)", dl)
        if m:
            s.known(m.group(1), "prefix")
        for path in re.findall(r"gitfurl-[^\s/]+?\.desktop", dl):
            s.known(path, "menu-entry")
        if read_sources:
            _collect_from_sources(s, sources)
        if "events" in files:
            facts.option("--detailed-log")
        all_facts.append(facts)
    # The caller's names last: a name the run's files already gave keeps its number, so the
    # window's report (with the plan's names) reads as the command line's for the same run.
    for name, kind in names:
        if kind == "path":
            s.known_path(name)
        else:
            s.known(name, kind)
    lines = [HEADER.format(sizes=SIZES_EXACT if exact_sizes else SIZES_ROUNDED), "",
             "== environment (this machine, when the report was made) =="]
    scrubbed: list[str] = [s.scrub(x) for x in environment(wine_version, umu, os_release_path)]
    lines += scrubbed
    for k, ((r, files), facts) in enumerate(zip(texts, all_facts), 1):
        s.origin = _origin(r, files.get("timeline"))
        lines += ["", f"== run {k} of {len(texts)} ==", *_facts_lines(facts, files, r.hives)]
        sections = []
        if "timeline" in files:
            sections.append(("-- timeline (times from the run's start) --",
                             _timeline_lines(s, files["timeline"])))
        if "log" in files:
            sections.append(("-- log --", _log_lines(s, files["log"])))
        if "events" in files:
            sections.append(("-- detailed log (summary) --", _event_lines(s, files["events"])))
        if "delete" in files:
            sections.append(("-- delete log --", [s.scrub(x) for x in
                                                  files["delete"].splitlines()]))
        for title, body in sections:
            lines += ["", title, *body]
            scrubbed += body
    leaks = s.leaks(scrubbed)
    if leaks:
        raise ReportRefused("no report was made: after scrubbing, the text still held "
                            + " and ".join(leaks) + " (a name gitfurl could not replace). "
                            "Nothing was written or copied")
    return Report("\n".join(lines) + "\n", dict(s.removed), len(texts))


def write_report(path, text: str) -> None:
    """Write `text` to a new file, mode 0600. Refuses a file that exists and a link there."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC,
                 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        os.fchmod(fh.fileno(), 0o600)
        fh.write(text)


def probe_wine() -> tuple[str | None, bool]:
    """The Wine version (`wine --version`, gitfurl.doctor's bounded probe; it creates and
    changes nothing) and whether umu-run is on PATH (not started)."""
    import shutil

    from gitfurl.doctor import probe_tool

    t = probe_tool("wine", ["--version"])
    return (t.version if t.problem is None else None), shutil.which("umu-run") is not None
