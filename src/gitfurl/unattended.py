"""Unattended runs of Inno Setup installers: no wizard, no installer windows.

Off by default. Only offered when the read-only detection found an Inno Setup ID string
("likely"). The installer still does all the work (its own code unpacks its own files, in the
prefix, under Wine); gitfurl only passes Inno Setup's documented command-line switches, picks
the folder inside the prefix, and reads the log Inno Setup writes there afterwards.

Switches used (Inno Setup's "Setup Command Line Parameters"):
  /SP-               no "This will install..." question at the start
  /VERYSILENT        no wizard and no progress window
  /SUPPRESSMSGBOXES  message boxes take their default answer
  /NORESTART         never restart, even if the installer asks for it
  /DIR="x:\\dir"     the install folder
  /LOG="file"        a log of what Setup did

A setup script can still show its own windows or ignore these switches; that is not known
until it runs. Nothing here runs anything: it builds arguments and reads one file afterwards.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from gitfurl.detect import CONF_LIKELY, Detection, clean
from gitfurl.prefixes import validate_prefix_name

INNO_SWITCHES = ("/SP-", "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART")
LOG_READ_LIMIT = 4 * 1024 * 1024  # only the end of a very long Inno log is read
MAX_ERROR_LINES = 5

# Inno Setup's documented exit codes ("Setup Exit Codes").
EXIT_MEANINGS = {
    0: "Setup finished",
    1: "Setup failed to initialize",
    2: "Setup was cancelled before the installation started",
    3: "a fatal error happened while Setup prepared the next step",
    4: "a fatal error happened during the installation",
    5: "the installation was cancelled while it ran (or an error was answered with Abort)",
    6: "Setup was forced to end",
    7: "Setup's preparing step decided it cannot go on",
    8: "Setup's preparing step decided it cannot go on until the computer restarts",
}

_ERROR_RE = re.compile(r"(?i)(exception|error|failed|abort)")
_SUCCESS = "Installation process succeeded."
_DEST_RE = re.compile(r"Dest filename: ")


class UnattendedRefused(ValueError):
    """The unattended route was asked for but cannot be used for this installer."""


@dataclass(frozen=True)
class UnattendedPlan:
    target: str  # a Windows path inside the prefix, e.g. C:\Games\my-game
    log_name: str  # file name of Inno Setup's log, directly in the prefix's drive_c

    def args(self) -> list[str]:
        return [*INNO_SWITCHES, f"/DIR={self.target}", f"/LOG=C:\\{self.log_name}"]

    def log_path(self, prefix: Path) -> Path:
        return prefix / "drive_c" / self.log_name

    def folder(self, prefix: Path) -> Path:
        """The Linux path of the install folder (C: is the prefix's drive_c)."""
        return prefix / "drive_c" / Path(*self.target[3:].split("\\"))

    def describe(self, prefix: Path) -> list[str]:
        return [
            "unattended install (Inno Setup): on",
            "  the installer runs without its wizard or windows; it does the unpacking itself",
            f"  switches: {' '.join(self.args())}",
            f"  install folder: {self.target} (in the prefix: {self.folder(prefix)})",
            f"  Inno Setup's log: {self.log_path(prefix)} (read after the run)",
            ("  the setup script picks every other choice (components, tasks) with its "
             "defaults; a script can still show its own windows or ignore these switches"),
        ]


def plan_unattended(prefix_name: str, detection: Detection,
                    now: datetime | None = None) -> UnattendedPlan:
    """The unattended plan, or UnattendedRefused when the installer is not Inno Setup."""
    validate_prefix_name(prefix_name)
    inno = [ev for ev in detection.evidence
            if ev.format == "Inno Setup" and ev.confidence == CONF_LIKELY]
    if not inno:
        raise UnattendedRefused(
            "unattended install only works with Inno Setup installers, and no Inno Setup ID "
            "was found in this file (run without the unattended option to use the "
            "installer's own windows)")
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return UnattendedPlan(target=f"C:\\Games\\{prefix_name}",
                          log_name=f"gitfurl-setup-{stamp}.log")


@dataclass
class UnattendedReport:
    exit_status: int | None
    log_found: bool = False
    succeeded: bool = False  # Inno's log says "Installation process succeeded."
    files: int = 0  # "Dest filename:" lines in the log
    errors: list[str] = field(default_factory=list)
    log_path: Path | None = None
    folder: Path | None = None
    folder_exists: bool = False

    def lines(self) -> list[str]:
        out = []
        if self.exit_status is not None:
            meaning = EXIT_MEANINGS.get(self.exit_status)
            if meaning:
                out.append(f"Inno Setup exit status {self.exit_status}: {meaning}.")
        if not self.log_found:
            out.append(f"Inno Setup's log was not found ({self.log_path}); Setup may not have "
                       "started, or the script did not honour /LOG.")
            return out
        state = "says the installation succeeded" if self.succeeded else \
            "does not say the installation succeeded"
        out.append(f"Inno Setup's log {state}; it lists {self.files} file(s) written. "
                   f"Log: {self.log_path}")
        out.extend(f"  from the log: {e}" for e in self.errors)
        if self.folder is not None:
            where = "exists" if self.folder_exists else "does not exist"
            out.append(f"Install folder {self.folder} {where}.")
        return out


def read_report(plan: UnattendedPlan, prefix: Path, exit_status: int | None) -> UnattendedReport:
    """Read Inno Setup's log after the run. Reads one file, bounded; never raises on its text."""
    path = plan.log_path(prefix)
    folder = plan.folder(prefix)
    rep = UnattendedReport(exit_status, log_path=path, folder=folder,
                           folder_exists=folder.is_dir())
    try:
        with open(path, "rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - LOG_READ_LIMIT))
            raw = fh.read(LOG_READ_LIMIT)
    except OSError:
        return rep
    rep.log_found = True
    text = raw.decode("utf-8", errors="replace")
    for line in text.splitlines():
        if _SUCCESS in line:
            rep.succeeded = True
        if _DEST_RE.search(line):
            rep.files += 1
        elif _ERROR_RE.search(line) and len(rep.errors) < MAX_ERROR_LINES:
            rep.errors.append(clean(line.strip(), 200))
    return rep
