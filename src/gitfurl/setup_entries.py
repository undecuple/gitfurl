"""The setup's own registry entries and shortcuts on the native route, without running Wine.

Opt-in (`gitfurl cli install --apply-setup-entries`, or the window's switch). On the native
route Setup is not run, so nothing of its [Registry] and [Icons] sections happens. This module
reads those sections from the setup's header (gitfurl.inno, read-only), keeps only the entries
it can apply exactly, and, after the approval of a plan that lists every value and file:

  registry   writes the values into the prefix's own text registry files (system.reg for
             HKLM and HKCR, which is HKLM\\Software\\Classes; user.reg for HKCU). No Wine
             program runs. It refuses while a Wine server for the prefix runs (that server
             would write its own copy back), copies each file into the log folder first (mode
             0600), changes only the listed values (other lines stay byte for byte), and
             writes through a temporary file and a rename.
  shortcuts  writes one freedesktop menu entry per program under {app}:
             $XDG_DATA_HOME/applications/gitfurl-<prefix>-<name>.desktop (mode 0644), which
             starts it with `env WINEPREFIX=<prefix> wine "C:\\..."`. A file there that gitfurl
             did not write is never replaced.

Skipped, and listed with the reason: entries with a Check function, [Code] calls, a language
condition (when the setup has more than one language), components or tasks that are not the
defaults, a Windows version limit, constants outside the small table below ({code:...},
{reg:...}, {param:...} and the like), roots other than HKLM, HKCU, HKCR (and HKA when the
setup's privileges settle it). Deletion flags and uninstall flags are not applied, only listed.
[Run] entries are not touched here (they stay listed, never run).
"""

from __future__ import annotations

import os
import pwd
import re
import stat
import time
from dataclasses import dataclass, field
from pathlib import Path

from gitfurl.detect import clean

MAX_VALUES = 2000  # registry values one plan may write
MAX_SHORTCUTS = 50
MAX_DATA = 64 * 1024  # characters of one value's data
MAX_REG_FILE = 256 << 20  # bytes of one registry file that are read
SERVER_WAIT_S = 30.0  # how long a Wine server that is still running is waited for
SHOWN = 160  # characters of one value shown in the plan

HIVES = {"system": "system.reg", "user": "user.reg"}
HEADER = "WINE REGISTRY Version 2"

# Keys below Software\Classes that Windows keeps apart for 32-bit programs on 64-bit Windows.
_CLASSES_SPLIT = frozenset(["clsid", "interface", "typelib", "directshow", "media type",
                            "mediafoundation", "appid"])
_X64 = {"x64", "x64compatible", "x64os", "win64"}
_FUNCTIONS = {"code": "a [Code] function", "reg": "a registry lookup",
              "param": "a command-line parameter", "ini": "an INI lookup",
              "cm": "a custom message", "drive": "a drive lookup"}


class EntriesRefused(Exception):
    """Why one registry file was not changed (nothing was written to it)."""


# --- what gitfurl knows about the prefix --------------------------------------------------

@dataclass(frozen=True)
class Facts:
    arch: str | None  # "win64", "win32" or None (not known)
    user: str | None  # the profile folder's name under drive_c\users, or None
    assumed: bool = False  # the prefix does not exist yet: arch and user are assumed
    reg_files: bool = True  # system.reg and user.reg are there (or will be: assumed)

    def summary(self) -> str:
        arch = self.arch or "not known"
        user = self.user or "not known"
        how = " (assumed: the prefix is still to be created; checked again before writing)" \
            if self.assumed else ""
        return f"prefix architecture {arch}, user folder {user}{how}"


def _unix_user(env: dict[str, str]) -> str | None:
    name = env.get("USER") or env.get("LOGNAME")
    if not name:
        try:
            name = pwd.getpwuid(os.getuid()).pw_name
        except KeyError:
            return None
    return name if name and re.fullmatch(r"[A-Za-z0-9._-]{1,64}", name) else None


def _read_head(path: Path, n: int = 4096) -> str | None:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return None
    with os.fdopen(fd, "rb") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            return None
        return fh.read(n).decode("latin-1")


def prefix_facts(prefix: Path, env: dict[str, str] | None = None) -> Facts:
    """The prefix's architecture (system.reg's #arch line) and its user folder. Reads a few
    names and the first 4 KiB of system.reg only. For a prefix that does not exist yet,
    Wine's defaults are assumed: win64, and the Unix user name."""
    env = os.environ if env is None else env
    prefix = Path(prefix)
    if not prefix.is_dir():
        return Facts("win64", _unix_user(env), assumed=True)
    head = _read_head(prefix / HIVES["system"])
    reg_files = all(os.path.lexists(prefix / name) for name in HIVES.values())
    arch = None
    if head is not None:
        m = re.search(r"^#arch=(win32|win64)\s*$", head, re.M)
        arch = m.group(1) if m else None
    users = []
    try:
        for entry in os.scandir(prefix / "drive_c" / "users"):
            if entry.is_dir(follow_symlinks=False) and entry.name.casefold() not in (
                    "public", "default", "all users", "default user"):
                users.append(entry.name)
    except OSError:
        pass
    user = users[0] if len(users) == 1 else None
    return Facts(arch, user, False, reg_files)


# --- Inno Setup constants -------------------------------------------------------------------

@dataclass(frozen=True)
class Context:
    app: str  # the native destination, C:\Games\<prefix>
    arch: str | None
    mode64: bool | None  # Setup's 64-bit install mode
    admin: bool | None  # Setup's administrative install mode
    user: str | None


def _const(name: str, ctx: Context) -> tuple[str | None, str]:
    """The value of one constant (lower case, no braces), or (None, why)."""
    win64, win32 = ctx.arch == "win64", ctx.arch == "win32"
    pf32 = "C:\\Program Files (x86)" if win64 else "C:\\Program Files" if win32 else None
    pf64 = "C:\\Program Files" if win64 else None
    pf = None if ctx.mode64 is None else pf64 if ctx.mode64 else pf32
    home = f"C:\\users\\{ctx.user}" if ctx.user else None
    roaming = home and home + "\\AppData\\Roaming"
    local = home and home + "\\AppData\\Local"

    def common(path):
        return None if path is None else path + "\\Common Files"

    def auto(admin_value, user_value):
        return None if ctx.admin is None else admin_value if ctx.admin else user_value

    table = {
        "app": ctx.app, "win": "C:\\windows", "sys": "C:\\windows\\system32",
        "syswow64": "C:\\windows\\syswow64" if win64 else None, "sd": "C:",
        "cmd": "C:\\windows\\system32\\cmd.exe", "fonts": "C:\\windows\\Fonts",
        "pf": pf, "commonpf": pf, "pf32": pf32, "commonpf32": pf32, "pf64": pf64,
        "commonpf64": pf64, "cf": common(pf), "commoncf": common(pf), "cf32": common(pf32),
        "commoncf32": common(pf32), "cf64": common(pf64), "commoncf64": common(pf64),
        "commonappdata": "C:\\ProgramData", "commondocs": "C:\\users\\Public\\Documents",
        "userappdata": roaming, "localappdata": local, "userdocs": home and home + "\\Documents",
        "userpf": local and local + "\\Programs",
        "autopf": auto(pf, local and local + "\\Programs"),
        "autoappdata": auto("C:\\ProgramData", roaming),
    }
    if name not in table:
        return None, f"uses {{{clean(name, 40)}}}, which gitfurl does not expand"
    value = table[name]
    if value is None:
        why = ("the prefix's user folder is not known" if name in (
            "userappdata", "localappdata", "userdocs", "userpf") else
               "Setup's install mode (administrative or not) is not known" if name.startswith(
                   "auto") else
               "the prefix's architecture or Setup's 64-bit mode is not known")
        return None, f"uses {{{name}}} and {why}"
    return value, ""


def _close(text: str, i: int) -> int:
    """Index of the '}' that closes the '{' at i (constants can nest), or -1."""
    depth = 0
    for j in range(i, len(text)):
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return j
    return -1


def expand(text: str, ctx: Context, multi: bool = False) -> tuple[str | None, str]:
    """(expanded text, "") or (None, why it is skipped). `multi`: {break} separates the
    strings of a multisz value (a NUL character in the result)."""
    out: list[str] = []
    i = 0
    while i < len(text):
        c = text[i]
        if c != "{":
            out.append(c)
            i += 1
            continue
        if text.startswith("{{", i):
            out.append("{")
            i += 2
            continue
        j = _close(text, i)
        if j < 0:
            return None, "has a '{' without its '}'"
        name = text[i + 1:j]
        if ":" in name:
            kind = name.split(":", 1)[0].casefold()
            what = _FUNCTIONS.get(kind, "a lookup")
            return None, f"uses {{{clean(kind, 20)}:...}} ({what}), which gitfurl does not expand"
        if name.startswith("%"):
            return None, "uses {%...} (an environment variable), which gitfurl does not expand"
        low = name.casefold()
        if low == "break" and multi:
            out.append("\0")
        else:
            value, why = _const(low, ctx)
            if value is None:
                return None, why
            out.append(value)
        i = j + 1
    return "".join(out), ""


# --- Wine's text registry format ------------------------------------------------------------

_C_ESCAPES = {7: "a", 8: "b", 9: "t", 10: "n", 11: "v", 12: "f", 13: "r", 27: "e"}
_C_UNESCAPES = {"a": "\a", "b": "\b", "t": "\t", "n": "\n", "v": "\v", "f": "\f", "r": "\r",
                "e": "\x1b", "0": "\0"}


def reg_escape(text: str, quote: str) -> str:
    """Text as Wine writes it in a registry file: backslash and `quote` characters escaped,
    control characters as C or octal escapes, anything above 127 as \\x and four hex digits
    (UTF-16 code units). The result is ASCII."""
    out = []
    raw = text.encode("utf-16-le", "surrogatepass")
    units = [raw[k] | raw[k + 1] << 8 for k in range(0, len(raw), 2)]
    for k, u in enumerate(units):
        ch = chr(u)
        nxt = chr(units[k + 1]) if k + 1 < len(units) else ""
        if u > 127:  # as Wine: four digits only when a hex digit follows
            out.append(f"\\x{u:04x}" if nxt and nxt in "0123456789abcdefABCDEF" else
                       f"\\x{u:x}")
        elif u < 32:  # as Wine: three digits only when an octal digit follows
            out.append("\\" + _C_ESCAPES[u] if u in _C_ESCAPES else
                       f"\\{u:03o}" if nxt and nxt in "01234567" else f"\\{u:o}")
        elif ch == "\\" or ch in quote:
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def reg_unescape(text: str, start: int, end_char: str) -> tuple[str, int]:
    """Read escaped text from `start` up to an unescaped `end_char`. Returns (text, index of
    end_char). Raises EntriesRefused when it is not there."""
    out = []
    i = start
    n = len(text)
    while i < n:
        c = text[i]
        if c == end_char:
            return "".join(out), i
        if c != "\\" or i + 1 >= n:
            out.append(c)
            i += 1
            continue
        nxt = text[i + 1]
        if nxt == "x":
            m = re.match(r"[0-9a-fA-F]{1,4}", text[i + 2:i + 6])
            if m:
                out.append(chr(int(m.group(0), 16)))
                i += 2 + len(m.group(0))
                continue
            out.append("x")
            i += 2
            continue
        m = re.match(r"[0-7]{1,3}", text[i + 1:i + 4])
        if m:
            out.append(chr(int(m.group(0), 8)))
            i += 1 + len(m.group(0))
            continue
        out.append(_C_UNESCAPES.get(nxt, nxt))
        i += 2
    raise EntriesRefused(f"a line of the registry file has no closing {end_char!r}")


def _key_of(line: str) -> str:
    name, _end = reg_unescape(line, 1, "]")
    return name


def _value_name(line: str) -> str | None:
    """The value name of a value line ("" for the default value @), or None for another
    line (#time, #class, a comment)."""
    if line.startswith("@="):
        return ""
    if not line.startswith('"'):
        return None
    name, end = reg_unescape(line, 1, '"')
    return name if line[end + 1:end + 2] == "=" else None


class _Section:
    def __init__(self, lines: list[str], new: bool = False):
        self.lines = lines
        self.new = new

    def logical(self) -> list[tuple[int, int]]:
        """(start, end) of each logical line after the key line (a line ending in a
        backslash goes on in the next one)."""
        out, i = [], 1
        while i < len(self.lines):
            j = i
            while j < len(self.lines) - 1 and self.lines[j].rstrip("\r\n").endswith("\\"):
                j += 1
            out.append((i, j + 1))
            i = j + 1
        return out

    def find(self, name: str) -> tuple[int, int] | None:
        want = name.casefold()
        for start, end in self.logical():
            got = _value_name(self.lines[start])
            if got is not None and got.casefold() == want:
                return start, end
        return None

    def insert_at(self) -> int:
        """Index after the last line that is not blank."""
        k = len(self.lines)
        while k > 1 and not self.lines[k - 1].strip():
            k -= 1
        return k


def value_line(name: str, text: str) -> str:
    left = "@" if name == "" else '"' + reg_escape(name, '"') + '"'
    return f"{left}={text}\n"


def edit_registry(data: bytes, values: list, now: float | None = None
                  ) -> tuple[bytes, list[str]]:
    """The registry file `data` with `values` (RegValue, all of this file) set, and one line
    per value saying what was done. Only the listed values' lines and new keys' sections are
    added or replaced; every other byte stays as it was. Raises EntriesRefused."""
    now = time.time() if now is None else now
    text = data.decode("latin-1")
    if not text.startswith(HEADER):
        raise EntriesRefused(f"it does not start with {HEADER!r}")
    lines = text.splitlines(keepends=True)
    header: list[str] = []
    sections: list[_Section] = []
    for ln in lines:
        if ln.startswith("["):
            sections.append(_Section([ln]))
        elif sections:
            sections[-1].lines.append(ln)
        else:
            header.append(ln)
    index: dict[str, _Section] = {}
    for sec in sections:
        index.setdefault(_key_of(sec.lines[0]).casefold(), sec)
    done: list[str] = []
    filetime = int((now + 11644473600) * 10_000_000)
    for v in values:
        sec = index.get(v.key.casefold())
        what = v.display()
        if sec is None:
            if v.key_must_exist:
                done.append(f"not written (the key does not exist; dontcreatekey): {what}")
                continue
            sec = _Section([f"[{reg_escape(v.key, '[]')}] {int(now)}\n",
                            f"#time={filetime:x}\n"], new=True)
            sections.append(sec)
            index[v.key.casefold()] = sec
            if v.kind == "none":
                done.append(f"created the key: {what}")
                continue
        elif v.kind == "none":
            done.append(f"the key was already there: {what}")
            continue
        at = sec.find(v.name)
        line = value_line(v.name, v.text)
        if at is not None and v.only_if_missing:
            done.append(f"kept the value that was there (createvalueifdoesntexist): {what}")
        elif at is not None:
            sec.lines[at[0]:at[1]] = [line]
            done.append(f"replaced: {what}")
        else:
            k = sec.insert_at()
            if not sec.lines[k - 1].endswith("\n"):
                sec.lines[k - 1] += "\n"
            sec.lines.insert(k, line)
            done.append(f"added: {what}")
    out = ["".join(header)]
    for sec in sections:
        if sec.new:
            last = next((x for x in reversed(out) if x), "")
            if last and not last.endswith("\n"):
                out.append("\n")
            out.append("\n")
        out.append("".join(sec.lines))
    return "".join(out).encode("latin-1"), done


# --- the plan --------------------------------------------------------------------------------

@dataclass(frozen=True)
class RegValue:
    hive: str  # "system" or "user"
    root: str  # the root as Windows names it here: HKLM or HKCU
    key: str  # relative to the hive file's root
    name: str  # "" for the default value; ignored for kind "none"
    kind: str  # string, expandsz, multisz, dword, qword, binary, none
    text: str  # what follows '=' in the registry file
    shown: str  # the data, for people
    only_if_missing: bool = False  # createvalueifdoesntexist
    key_must_exist: bool = False  # dontcreatekey
    notes: tuple[str, ...] = ()

    def display(self) -> str:
        where = clean(f"{self.root}\\{self.key}", SHOWN)
        if self.kind == "none":
            return f"{where} (key only)"
        name = "(default)" if self.name == "" else repr(clean(self.name, 80))
        return f"{where} {name} = {self.kind} {self.shown}"


@dataclass(frozen=True)
class Shortcut:
    path: Path  # the .desktop file
    name: str
    target: str  # Windows path of the program
    text: str  # the file's content
    replaces: bool = False  # a file gitfurl wrote earlier is there


@dataclass
class SetupEntriesPlan:
    prefix: Path
    prefix_name: str
    facts: Facts
    values: list[RegValue] = field(default_factory=list)
    shortcuts: list[Shortcut] = field(default_factory=list)
    new_dirs: list[Path] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)  # "what: why"
    notes: list[str] = field(default_factory=list)
    env: dict | None = None

    @property
    def empty(self) -> bool:
        return not self.values and not self.shortcuts

    def describe(self) -> list[str]:
        out: list[str] = []
        if self.values:
            out.append(f"registry ({self.facts.summary()}): written into the prefix's own "
                       "files without Wine, only while no Wine server for this prefix runs "
                       f"(waits up to {SERVER_WAIT_S:g} s for one to end); each file is first "
                       "copied into the log folder (mode 0600), then only these values are "
                       "added or replaced, through a temporary file and a rename:")
            for v in self.values:
                out.append(f"  {HIVES[v.hive]}: {v.display()}")
                out.extend(f"    note: {n}" for n in v.notes)
        else:
            out.append("registry: no value to write")
        if self.shortcuts:
            out.append("menu entries (outside the prefix, in your own data folder; mode 0644; "
                       "a file there that gitfurl did not write is left as it is):")
            out.extend(f"  create the folder {d}" for d in self.new_dirs)
            for sc in self.shortcuts:
                verb = "replace gitfurl's entry" if sc.replaces else "write"
                out.append(f"  {verb} {sc.path}: {clean(sc.name, 80)} starts "
                           f"{clean(sc.target, SHOWN)}")
        else:
            out.append("menu entries: none to write")
        if self.skipped:
            out.append("skipped (not applied):")
            out.extend(f"  {s}" for s in self.skipped)
        out.extend(f"note: {n}" for n in self.notes)
        return out


def _ver_major(nt: int) -> int:
    return nt >> 24 & 0xFF


def _mode64(setup, arch: str | None) -> bool | None:
    from gitfurl.inno import evaluate

    if arch == "win32":
        return False
    expr = (setup.arch_64bit or "").strip()
    if not expr:
        return False
    if arch is None:
        return None
    return evaluate(expr, _X64)


def _admin(setup) -> bool | None:
    return {2: True, 3: False}.get(setup.privileges)


def _number(text: str, bits: int) -> int | None:
    t = text.strip()
    try:
        if t.startswith("$"):
            v = int(t[1:], 16)
        elif t.lower().startswith("0x"):
            v = int(t[2:], 16)
        else:
            v = int(t, 10)
    except ValueError:
        return None
    if v < 0:
        v += 1 << bits
    return v if 0 <= v < 1 << bits else None


def _hexbytes(raw: bytes) -> str:
    return ",".join(f"{b:02x}" for b in raw)


def _format(kind: str, data: str) -> tuple[str, str] | str:
    """(registry file text, shown data) for one value, or why it is skipped."""
    if len(data) > MAX_DATA:
        return f"its data is longer than {MAX_DATA:,} characters"
    if kind == "string":
        return '"' + reg_escape(data, '"') + '"', repr(clean(data, SHOWN))
    if kind == "expandsz":
        return 'str(2):"' + reg_escape(data, '"') + '"', repr(clean(data, SHOWN))
    if kind == "multisz":
        parts = data.split("\0")
        return ('str(7):"' + reg_escape("\0".join(parts) + "\0", '"') + '"',
                "[" + ", ".join(repr(clean(p, 60)) for p in parts[:8])
                + (", ..." if len(parts) > 8 else "") + "]")
    if kind in ("dword", "qword"):
        bits = 32 if kind == "dword" else 64
        v = _number(data, bits)
        if v is None:
            return f"its {kind} data is not a number gitfurl can read ({clean(data, 40)!r})"
        if kind == "dword":
            return f"dword:{v:08x}", str(v)
        return "hex(b):" + _hexbytes(v.to_bytes(8, "little")), str(v)
    if kind == "binary":
        try:
            raw = bytes.fromhex(data.replace(",", " "))
        except ValueError:
            return f"its binary data is not hex bytes ({clean(data, 40)!r})"
        return "hex:" + _hexbytes(raw), (_hexbytes(raw[:32]) + (",..." if len(raw) > 32
                                                                 else "")) or "(empty)"
    return "", ""  # none: the key only


def _place(root: str, subkey: str, view64: bool | None, ctx: Context, setup
           ) -> tuple[str, str, str] | str:
    """(hive, root shown, key in that hive) or why it is skipped."""
    parts = subkey.strip("\\").split("\\")
    if not subkey.strip("\\") or any(not p or len(p) > 255 for p in parts):
        return "the key path is empty, has an empty part or a part longer than 255 characters"
    if root == "HKA":
        admin = _admin(setup)
        if admin is None:
            return "HKA depends on Setup's install mode, which is not known"
        root = "HKLM" if admin else "HKCU"
    if root == "HKCR":
        if _admin(setup) is not True:
            return ("HKCR outside an administrative install may go to HKCU instead; gitfurl "
                    "does not guess")
        parts = ["Software", "Classes", *parts]
        root = "HKLM"
    if root not in ("HKLM", "HKCU"):
        return f"{root} is a root gitfurl does not write"
    hive = "system" if root == "HKLM" else "user"
    low = [p.casefold() for p in parts]
    placed = hive, root, "\\".join(parts)
    if ctx.arch == "win32":
        if view64:
            return "it asks for the 64-bit registry view, which a 32-bit prefix does not have"
        return placed
    if low[0] != "software" or (len(low) > 1 and low[1] == "wow6432node"):
        return placed
    classes = len(low) > 1 and low[1] == "classes"
    # Which keys Windows keeps apart for 32-bit programs: HKLM\Software (not its Classes),
    # and a few class keys (CLSID and the like) in both roots.
    if not (len(low) > 2 and low[2] in _CLASSES_SPLIT if classes else root == "HKLM"):
        return placed
    if view64 is None:
        return "its registry view (32-bit or 64-bit) is not known, so where the key goes is not"
    if ctx.arch is None:
        return "the prefix's architecture is not known, so where the key goes is not known"
    if view64:
        return placed
    if classes:
        return "a 32-bit program's class key is kept apart by Windows; gitfurl does not place it"
    return hive, root, "\\".join([parts[0], "Wow6432Node", *parts[1:]])


def _reg_value(e, ctx: Context, choice, languages: int, setup) -> RegValue | str:
    """One [Registry] entry as a RegValue, or why it is skipped."""
    from gitfurl.inno import REG_TYPES

    flags = e.flags
    if e.hooks:
        return "calls [Code] (BeforeInstall or AfterInstall)"
    if e.cond.check:
        return f"has a Check function ({clean(e.cond.check, 40)})"
    if e.cond.languages and languages > 1:
        return "depends on the language Setup picks"
    notes: list[str] = []
    if e.cond.components or e.cond.tasks or e.cond.languages:
        selected, why = choice.decide(e.cond)
        if selected is None:
            return why
        if not selected:
            return why
        notes.append("part of the default components or tasks (as gitfurl reads them)")
    if e.below_nt:
        return "limited to Windows versions below one it names"
    if _ver_major(e.min_nt) > 10:
        return "needs a Windows version newer than 10"
    kind = REG_TYPES.get(e.value_type)
    if kind is None:
        return "has a value type gitfurl does not know"
    unknown = sorted(f for f in flags if f.startswith("bit "))
    if unknown:
        return "has option bits gitfurl does not know"
    if kind == "none" and flags & {"deletekey", "deletevalue"}:
        return "only deletes (deletion flags are not applied)"
    for flag in ("deletekey", "deletevalue"):
        if flag in flags:
            notes.append(f"{flag} is not applied: nothing is deleted first")
    unins = sorted(flags & {"uninsdeletevalue", "uninsclearvalue", "uninsdeletekey",
                            "uninsdeletekeyifempty"})
    if unins:
        notes.append(", ".join(unins) + " not applied (there is no uninstaller)")
    if "preservestringtype" in flags:
        notes.append("preservestringtype is not applied: the type above is written")
    if e.bitness:
        view64 = None
    elif "64bit" in flags:
        view64 = True
    elif "32bit" in flags:
        view64 = False
    else:
        view64 = ctx.mode64
    root = e.root_name
    subkey, why = expand(e.subkey, ctx)
    if subkey is None:
        return f"its key {why}"
    name, why = expand(e.value_name, ctx)
    if name is None:
        return f"its value name {why}"
    data, why = expand(e.value_data, ctx, multi=(kind == "multisz")) if kind != "none" \
        else ("", "")
    if data is None:
        return f"its data {why}"
    place = _place(root, subkey, view64, ctx, setup)
    if isinstance(place, str):
        return place
    hive, shown_root, key = place
    fmt = _format(kind, data)
    if isinstance(fmt, str):
        return fmt
    text, shown = fmt
    return RegValue(hive, shown_root, key, name, kind, text, shown,
                    "createvalueifdoesntexist" in flags, "dontcreatekey" in flags,
                    tuple(notes))


def _reg_label(e) -> str:
    name = "(default)" if not e.value_name else repr(clean(e.value_name, 60))
    return f"[Registry] {e.root_name}\\{clean(e.subkey, 100)} {name}"


# --- shortcuts -------------------------------------------------------------------------------

def _slug(name: str) -> str:
    s = "-".join(re.findall(r"[a-z0-9]+", name.casefold()))[:40].strip("-")
    return s or "program"


def _split_params(text: str) -> list[str] | None:
    """Simple Windows parameters as a list: spaces separate, double quotes group. None when
    they are not that simple (an odd number of quotes, or a backslash before a quote)."""
    if text.count('"') % 2 or '\\"' in text:
        return None
    out, cur, quoted, has = [], [], False, False
    for c in text:
        if c == '"':
            quoted, has = not quoted, True
        elif c.isspace() and not quoted:
            if cur or has:
                out.append("".join(cur))
            cur, has = [], False
        else:
            cur.append(c)
    if cur or has:
        out.append("".join(cur))
    return out


def _desktop_value(text: str) -> str:
    return text.replace("\\", "\\\\")


def shortcut_text(name: str, prefix: Path, prefix_name: str, target: str,
                  params: list[str], workdir: Path | None) -> str:
    from gitfurl.desktop import MARKER, _exec_quote

    argv = ["env", f"WINEPREFIX={prefix}", "wine", target, *params]
    lines = [
        "# Written by gitfurl from the setup's [Icons] entries (native route); gitfurl",
        "# replaces or leaves only files that carry its marker line below.",
        "[Desktop Entry]",
        "Type=Application",
        f"Name={_desktop_value(name)}",
        f"Comment={_desktop_value(f'Runs in the Wine prefix {prefix_name}')}",
        "Exec=" + " ".join(_exec_quote(a) for a in argv),
    ]
    if workdir is not None:
        lines.append("Path=" + _desktop_value(str(workdir)))
    lines += ["Terminal=false", MARKER, ""]
    return "\n".join(lines)


def _plan_shortcuts(setup, xplan, ctx: Context, env, plan: SetupEntriesPlan) -> None:
    from gitfurl.desktop import DesktopRefused, _missing_dirs, _ours_entry, _read_nofollow
    from gitfurl.desktop import data_home
    from gitfurl.route import written_files

    if not setup.icons:
        return
    try:
        apps = data_home(env) / "applications"
    except DesktopRefused as exc:
        plan.skipped.append(f"[Icons]: every entry ({len(setup.icons)}): {exc}")
        return
    files: dict[str, tuple[list[str], str]] = {}
    folders: dict[str, list[str]] = {"": []}
    for parts, name in written_files(xplan):
        files["\\".join([*parts, name]).casefold()] = (parts, name)
        for k in range(1, len(parts) + 1):
            folders.setdefault("\\".join(parts[:k]).casefold(), parts[:k])
    app = ctx.app.casefold()
    seen: dict[str, str] = {}
    used: set[str] = set()
    for ic in setup.icons:
        label = f"[Icons] {clean(ic.name, 80)} -> {clean(ic.target, 100)}"

        def skip(why: str, label=label) -> None:
            plan.skipped.append(f"{label}: {why}")

        if len(plan.shortcuts) >= MAX_SHORTCUTS:
            skip(f"more than {MAX_SHORTCUTS} menu entries")
            continue
        if ic.hooks:
            skip("calls [Code] (BeforeInstall or AfterInstall)")
            continue
        if ic.cond.conditional:
            what = ", ".join(f"{k}: {clean(v, 40)}" for k, v in (
                ("Components", ic.cond.components), ("Tasks", ic.cond.tasks),
                ("Languages", ic.cond.languages), ("Check", ic.cond.check)) if v)
            skip(f"has a condition ({what})")
            continue
        target, why = expand(ic.target, ctx)
        if target is None:
            skip(f"its program {why}")
            continue
        if not target.casefold().startswith(app + "\\"):
            skip("its program is not under {app}")
            continue
        rel = target[len(ctx.app) + 1:]
        found = files.get(rel.casefold())
        if found is None or any(p in (".", "..") for p in rel.split("\\")):
            skip("its program is not among the files the unpack writes")
            continue
        key = rel.casefold()
        if key in seen:
            skip(f"same program as {seen[key]}")
            continue
        params, why = expand(ic.parameters, ctx)
        if params is None:
            skip(f"its parameters {why}")
            continue
        argv = _split_params(params)
        if argv is None:
            skip("its parameters are not simple enough to pass on exactly")
            continue
        workdir_text, why = expand(ic.working_dir, ctx)
        if workdir_text is None:
            skip(f"its working folder {why}")
            continue
        parts, fname = found
        workdir: Path | None = xplan.dest.joinpath(*parts)
        if workdir_text:
            wd = workdir_text.rstrip("\\").casefold()
            if wd == app:
                workdir = xplan.dest
            elif wd.startswith(app + "\\") and wd[len(app) + 1:] in folders:
                workdir = xplan.dest.joinpath(*folders[wd[len(app) + 1:]])
            else:
                workdir = None
                plan.notes.append(f"{label}: its working folder {clean(workdir_text, 80)!r} is "
                                  "not a folder the unpack writes, so the entry sets none")
        display = ic.name.rsplit("\\", 1)[-1]
        name, why = expand(display, ctx)
        if not name or not name.strip() or any(ord(c) < 32 for c in name):
            skip(f"its name {why}" if name is None else "its name is empty or not printable")
            continue
        slug = _slug(name)
        base = f"gitfurl-{xplan.prefix_name}-{slug}"
        fn, n = f"{base}.desktop", 2
        while fn.casefold() in used:
            fn, n = f"{base}-{n}.desktop", n + 1
        path = apps / fn
        try:
            there = _read_nofollow(path)
        except DesktopRefused as exc:
            skip(str(exc))
            continue
        if there is not None and not _ours_entry(there):
            skip(f"{path} exists and was not written by gitfurl; it is left as it is")
            continue
        win = ctx.app + "\\" + "\\".join([*parts, fname])
        try:
            text = shortcut_text(name, xplan.prefix, xplan.prefix_name, win, argv, workdir)
        except DesktopRefused as exc:
            skip(str(exc))
            continue
        used.add(fn.casefold())
        seen[key] = fn
        plan.shortcuts.append(Shortcut(path, name, win, text, there is not None))
    if plan.shortcuts:
        plan.new_dirs = _missing_dirs(apps / "x")


def plan_setup_entries(installer: Path, xplan, env: dict[str, str] | None = None
                       ) -> SetupEntriesPlan:
    """What --apply-setup-entries would write for this native plan (gitfurl.extract
    ExtractPlan). Reads the setup's header, a few names in the prefix and the first bytes of
    its system.reg only; writes nothing. Never raises for an installer it cannot read (the
    plan then says so and holds nothing to write)."""
    from gitfurl import inno

    facts = prefix_facts(xplan.prefix, env)
    plan = SetupEntriesPlan(xplan.prefix, xplan.prefix_name, facts, env=env)
    try:
        setup = inno.read_setup(Path(installer))
    except Exception as exc:  # noqa: BLE001 - a hostile file must not stop the plan
        plan.notes.append("the installer's setup data could not be read "
                          f"({clean(str(exc), 120)}); there is nothing to add")
        return plan
    choice = inno.default_choice(setup)
    ctx = Context(xplan.target, facts.arch, _mode64(setup, facts.arch), _admin(setup),
                  facts.user)
    if setup.registry and not facts.reg_files:
        plan.skipped.append(f"[Registry]: every entry ({len(setup.registry)}): the prefix has "
                            "no system.reg and user.reg yet (a prefix gitfurl creates with "
                            "wineboot has them)")
    else:
        for e in setup.registry:
            label = _reg_label(e)
            if len(plan.values) >= MAX_VALUES:
                plan.skipped.append(f"{label}: more than {MAX_VALUES} values")
                continue
            got = _reg_value(e, ctx, choice, len(setup.languages), setup)
            if isinstance(got, str):
                plan.skipped.append(f"{label}: {got}")
            else:
                plan.values.append(got)
    _plan_shortcuts(setup, xplan, ctx, env, plan)
    if (setup.registry or setup.icons) and choice.notes and plan.values:
        plan.notes.extend(choice.notes)
    return plan


# --- writing ---------------------------------------------------------------------------------

def _read_reg(path: Path) -> tuple[bytes, os.stat_result]:
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise EntriesRefused(f"it could not be opened safely ({exc.strerror}); it may be a "
                             "link") from exc
    with os.fdopen(fd, "rb") as fh:
        st = os.fstat(fh.fileno())
        if not stat.S_ISREG(st.st_mode):
            raise EntriesRefused("it is not a regular file")
        if st.st_size > MAX_REG_FILE:
            raise EntriesRefused(f"it is larger than {MAX_REG_FILE >> 20} MiB")
        data = fh.read(MAX_REG_FILE + 1)
    return data, st


def _write_new(path: Path, data: bytes, mode: int) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    with os.fdopen(fd, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(path, mode)


def _same(a: os.stat_result, b: os.stat_result) -> bool:
    return (a.st_ino, a.st_dev, a.st_size, a.st_mtime_ns) == \
        (b.st_ino, b.st_dev, b.st_size, b.st_mtime_ns)


def _wait_for_server(prefix: Path, running, wait_s: float, sleep, clock) -> str | None:
    end = clock() + wait_s
    while True:
        why = running(prefix)
        if why is None or clock() >= end:
            return why
        sleep(0.5)


@dataclass
class Applied:
    """What apply_entries() did, counted (the timeline's rows); its lines say the same."""
    values: dict[str, int] = field(default_factory=dict)  # registry file name -> values set
    menu_entries: int = 0  # .desktop files written
    not_done: list[str] = field(default_factory=list)  # "what: why", one per item left out


_DONE = ("added: ", "replaced: ", "created the key: ")


def _write_hive(plan: SetupEntriesPlan, hive: str, values: list[RegValue], log_dir: Path,
                stamp: str, running, applied: Applied) -> list[str]:
    path = plan.prefix / HIVES[hive]
    data, st = _read_reg(path)
    new, done = edit_registry(data, values)
    kept = [d for d in done if not d.startswith(_DONE)]
    backup = log_dir / f"{plan.prefix_name}-{stamp}-{HIVES[hive]}"
    _write_new(backup, data, 0o600)
    out = [f"copied {path} to {backup} (mode 0600) first"]
    if new == data:
        applied.not_done.extend(f"{HIVES[hive]}: {d}" for d in kept)
        return [f"registry: {HIVES[hive]} not changed (every value was kept as it was)",
                *(f"  {d}" for d in done), f"  {out[0]}"]
    why = running(plan.prefix)
    if why is not None:
        raise EntriesRefused(f"a Wine server for this prefix started meanwhile ({why})")
    tmp = path.with_name(f".{path.name}.gitfurl-{os.getpid()}")
    try:
        _write_new(tmp, new, stat.S_IMODE(st.st_mode))
        try:
            now = os.lstat(path)
        except OSError as exc:
            raise EntriesRefused(f"it went away meanwhile ({exc.strerror})") from exc
        if not _same(now, st):
            raise EntriesRefused("it changed while gitfurl edited it")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise
    applied.values[HIVES[hive]] = len(done) - len(kept)
    applied.not_done.extend(f"{HIVES[hive]}: {d}" for d in kept)
    return [f"registry: wrote into {path} ({len(values)} listed):", *(f"  {d}" for d in done),
            f"  {out[0]}"]


def apply_entries(plan: SetupEntriesPlan, log_dir: Path, stamp: str, *, running=None,
                  wait_s: float = SERVER_WAIT_S, sleep=time.sleep, clock=time.monotonic,
                  applied: Applied | None = None) -> list[str]:
    """Write an approved plan's registry values and menu entries. Returns what was done, one
    line each (for the end text and the log). Every problem is a line too; nothing raises
    except KeyboardInterrupt. `running(prefix)` says why a Wine server for the prefix runs
    (default: procgroup.prefix_server_running). `applied` (an Applied), when given, gets the
    same counted."""
    applied = Applied() if applied is None else applied
    from gitfurl.desktop import DesktopRefused, _ours_entry, _read_nofollow, _write

    if running is None:
        from gitfurl.procgroup import prefix_server_running as running
    out: list[str] = []
    if plan.values:
        now = prefix_facts(plan.prefix, plan.env)
        if not now.reg_files:
            out.append("registry: nothing written: the prefix has no system.reg and user.reg")
            applied.not_done.append(f"registry ({len(plan.values)} values): the prefix has no "
                                    "system.reg and user.reg")
        elif (now.arch, now.user) != (plan.facts.arch, plan.facts.user):
            out.append("registry: nothing written: the prefix is not what the plan assumed "
                       f"(the plan: {plan.facts.arch or 'architecture not known'}, user folder "
                       f"{plan.facts.user or 'not known'}; the prefix: "
                       f"{now.arch or 'architecture not known'}, user folder "
                       f"{now.user or 'not known'})")
            applied.not_done.append(f"registry ({len(plan.values)} values): the prefix is not "
                                    "what the plan assumed")
        else:
            why = _wait_for_server(plan.prefix, running, wait_s, sleep, clock)
            if why is not None:
                out.append(f"registry: nothing written: a Wine server for this prefix is "
                           f"running ({why}); it would write its own copy back. Run again "
                           "into a new prefix once it has ended")
                applied.not_done.append(f"registry ({len(plan.values)} values): a Wine server "
                                        "for this prefix is running")
            else:
                for hive in HIVES:
                    values = [v for v in plan.values if v.hive == hive]
                    if not values:
                        continue
                    try:
                        out.extend(_write_hive(plan, hive, values, log_dir, stamp, running,
                                               applied))
                    except (EntriesRefused, OSError) as exc:
                        why = exc.strerror if isinstance(exc, OSError) and exc.strerror \
                            else exc
                        out.append(f"registry: {HIVES[hive]} not changed: {why}")
                        applied.not_done.append(f"{HIVES[hive]} ({len(values)} values): "
                                                f"not changed: {why}")
    for d in plan.new_dirs:
        try:
            os.mkdir(d, 0o755)
            out.append(f"menu entries: created the folder {d}")
        except FileExistsError:
            pass
        except OSError as exc:
            out.append(f"menu entries: the folder {d} could not be created ({exc.strerror})")
    for sc in plan.shortcuts:
        try:
            there = _read_nofollow(sc.path)
            if there is not None and not _ours_entry(there):
                out.append(f"menu entry not written: {sc.path} exists and was not written by "
                           "gitfurl; it is left as it is")
                applied.not_done.append(f"menu entry {sc.path.name}: a file there was not "
                                        "written by gitfurl")
                continue
            if not sc.path.parent.is_dir():
                out.append(f"menu entry not written: {sc.path.parent} is not a folder")
                applied.not_done.append(f"menu entry {sc.path.name}: {sc.path.parent} is not "
                                        "a folder")
                continue
            _write(sc.path, sc.text.encode("utf-8"))
            out.append(f"menu entry: wrote {sc.path} ({clean(sc.name, 80)})")
            applied.menu_entries += 1
        except (DesktopRefused, OSError) as exc:
            why = exc.strerror if isinstance(exc, OSError) and exc.strerror else exc
            out.append(f"menu entry not written: {sc.path}: {why}")
            applied.not_done.append(f"menu entry {sc.path.name}: {why}")
    if not out:
        out.append("setup entries: nothing to write")
    return out
