"""Environment diagnostics. Read-only: runs only bounded version probes."""

from __future__ import annotations

import os
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
            errors="replace",  # odd version output must not raise
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
    return ToolReport("tkinter", None, "available (importable)", None)


def machine_resources(meminfo: str = "/proc/meminfo") -> str:
    """Available memory and CPU threads: heavy decompression installers can need both."""
    cpus = os.cpu_count() or 0
    try:
        with open(meminfo, encoding="ascii") as fh:
            kb = next(int(line.split()[1]) for line in fh if line.startswith("MemAvailable:"))
        mem = f"{kb / 1048576:.1f} GB memory available"
    except (OSError, StopIteration, ValueError):
        mem = "memory unknown"
    return f"{cpus} CPU threads, {mem}"


def memory_limit_report(env=None) -> str:
    """Whether the memory limit can be used (file checks, then systemd itself, read-only)."""
    from gitfurl.memlimit import MemoryLimitError, availability, verify

    avail = availability(env)
    if not avail.ok:
        return f"unavailable ({avail.reason}); runs need --no-memory-limit"
    try:
        checked = verify(avail, env)
    except MemoryLimitError as exc:
        return f"unavailable ({str(exc).removeprefix('memory limit: NOT available: ')}); runs " \
               "need --no-memory-limit"
    return (f"available (systemd {checked.version}, memory controller delegated to your user "
            f"manager at {checked.manager_cgroup})")


def native_decoders() -> str:
    """Which stages gitfurl's optional native module (gitfurl._codecs) decodes, as imported."""
    from gitfurl import farc_codecs

    stages = [name for name, there in (("cm", farc_codecs.cm_available()),
                                       ("rolz", farc_codecs.rolz_native())) if there]
    if not stages:
        return "not installed (the cm stage is refused; rolz is decoded in Python)"
    return ", ".join(stages)


def run_doctor() -> dict:
    return {
        "resources": machine_resources(),
        "platform": f"{platform.system()} {platform.release()} ({platform.machine()})",
        "wine": probe_tool("wine", ["--version"]),
        "bwrap": probe_tool("bwrap", ["--version"]),
        "umu": probe_tool("umu-run", ["--version"]),
        "innoextract": probe_tool("innoextract", ["--version"]),
        "clamscan": probe_tool("clamscan", ["--version"]),
        "tkinter": probe_tkinter(),
        "memory_limit": memory_limit_report(),
        "native": native_decoders(),
    }


def format_report(report: dict) -> str:
    lines = [f"platform: {report['platform']}"]
    if report.get("resources"):
        lines.append(f"machine: {report['resources']}")
    notes = {"bwrap": " (optional, for --portable)",
             "umu": " (optional, for the proton runtime)",
             "innoextract": " (optional; only noted: gitfurl never runs it to extract)"}
    for key in ("wine", "bwrap", "umu", "innoextract", "tkinter"):
        t: ToolReport = report[key]
        if t.problem:
            text = f"unavailable ({t.problem})"
        else:
            text = t.version + (f" at {t.path}" if t.path else "")
        lines.append(f"{t.name}: {text}{notes.get(key, '')}")
    scan = report.get("clamscan")
    if scan is not None:
        lines.extend(_clamscan_lines(scan))
    if report.get("native"):
        lines.append(f"native decoders: {report['native']}")
    if report.get("memory_limit"):
        lines.append(f"memory limit: {report['memory_limit']}")
    lines.append("Nothing was installed or changed.")
    return "\n".join(lines)


def _clamscan_lines(t: ToolReport) -> list[str]:
    """clamscan is optional (for --scan). The version probe holds the database date."""
    note = " (optional, for --scan; gitfurl never updates its database)"
    if t.problem:
        return [f"clamscan: unavailable ({t.problem}){note}"]
    from gitfurl.scan import NO_DATABASE, database_info, describe_database, parse_version

    info = parse_version(t.version or "")
    if info.problem == NO_DATABASE and t.path:  # ClamAV 1.5: --version alone reads no database
        found = database_info(t.path)
        if found.db_version is not None:
            info = found
    engine = f"ClamAV {info.engine}" if info.engine else "version not recognised"
    return [f"clamscan: {engine} at {t.path}{note}",
            *(f"  {ln}" for ln in describe_database(info))]
