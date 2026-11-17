"""Environment diagnostics. Read-only: runs only bounded version probes."""

from __future__ import annotations

import platform
import shutil
import subprocess
from dataclasses import dataclass

PROBE_TIMEOUT_S = 5


@dataclass
class ToolReport:
    name: str
    path: str | None
    version: str | None
    problem: str | None  # None when the probe worked


def probe_tool(name: str, version_args: list[str]) -> ToolReport:
    path = shutil.which(name)
    if path is None:
        return ToolReport(name, None, None, "not found on PATH")
    try:
        proc = subprocess.run(
            [path, *version_args],
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_S,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return ToolReport(name, path, None, f"version check timed out after {PROBE_TIMEOUT_S}s")
    except OSError as exc:
        return ToolReport(name, path, None, f"could not run: {exc.strerror or exc}")
    out = (proc.stdout or proc.stderr or "").strip().splitlines()
    if proc.returncode != 0 or not out:
        return ToolReport(name, path, None, f"unexpected result (exit status {proc.returncode})")
    return ToolReport(name, path, out[0][:200], None)


def probe_tkinter() -> ToolReport:
    import importlib.util

    if importlib.util.find_spec("tkinter") is None:
        return ToolReport("tkinter", None, None,
                          "not installed (the graphical app needs it; try python3-tk)")
    return ToolReport("tkinter", "importable", "available", None)


def run_doctor() -> dict:
    return {
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "wine": probe_tool("wine", ["--version"]),
        "bwrap": probe_tool("bwrap", ["--version"]),
        "umu": probe_tool("umu-run", ["--version"]),
        "tkinter": probe_tkinter(),
    }


def format_report(report: dict) -> str:
    lines = [f"platform: {report['platform']}"]
    for key in ("wine", "bwrap", "umu", "tkinter"):
        t: ToolReport = report[key]
        if t.problem:
            lines.append(f"{t.name}: unavailable ({t.problem})" + (" (optional, for the proton runtime)" if key == "umu" else ""))
        else:
            lines.append(f"{t.name}: {t.version} at {t.path}")
    lines.append("Nothing was installed or changed.")
    return "\n".join(lines)
