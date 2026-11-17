"""Prefix naming and location. Nothing here creates or modifies a prefix."""

from __future__ import annotations

import os
import re
from pathlib import Path

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


def validate_prefix_name(name: str) -> str:
    if not _NAME_RE.fullmatch(name):
        raise ValueError(
            "invalid prefix name: use 1-64 letters, digits, '-' or '_', starting with a "
            "letter or digit"
        )
    return name


def prefixes_root(env: dict[str, str] | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_DATA_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/share")
    return Path(base) / "gitfurl" / "prefixes"


def prefix_path(name: str, env: dict[str, str] | None = None) -> Path:
    return prefixes_root(env) / validate_prefix_name(name)


WINEBOOT_TIMEOUT_S = 300


def child_env(base: dict[str, str], overrides: dict[str, str]) -> dict[str, str]:
    """The environment for a Wine child: yours, minus inherited WINE* settings, plus overrides.

    Variables such as WINEARCH or WINEDLLOVERRIDES from your shell could change how the
    installer runs without the plan showing it, so they are not passed on. Set them through
    gitfurl's options (for example --wine-debug), which the plan and the event log show.
    """
    kept = {k: v for k, v in base.items() if not k.startswith("WINE")}
    return {**kept, **overrides}


def list_prefixes(env: dict[str, str] | None = None) -> list[str]:
    root = prefixes_root(env)
    if not root.is_dir():
        return []
    return sorted(p.name for p in root.iterdir() if p.is_dir())


def plan_create(name: str, wine: str, env: dict[str, str] | None = None,
                extra_env: dict[str, str] | None = None) -> dict:
    """Describe what creating a prefix would do. Has no side effects."""
    path = prefix_path(name, env)
    return {
        "path": path,
        "argv": [wine, "wineboot", "--init"],
        "env_overrides": {**(extra_env or {}), "WINEPREFIX": str(path)},
    }


def _wineserver_for(wine: str) -> str | None:
    """wineserver beside an absolute wine path, else on PATH. Never relative to the cwd."""
    import shutil

    if os.path.isabs(wine):
        beside = Path(wine).with_name("wineserver")
        if beside.is_file():
            return str(beside)
    return shutil.which("wineserver")


def run_in_group(argv: list[str], env: dict[str, str], timeout: float, wine: str,
                 grace_s: float = 5.0):
    """Run argv in its own process group and wait up to `timeout` seconds.

    On a timeout or Ctrl-C only that group is signalled (TERM, then KILL after `grace_s`),
    and then this prefix's own Wine server is asked to exit (`wineserver -k`, WINEPREFIX
    from `env`). Nothing is found by name. A program that left the group is out of reach.
    Raises subprocess.TimeoutExpired after the cleanup, or re-raises KeyboardInterrupt.
    """
    import signal
    import subprocess
    import time

    proc = subprocess.Popen(argv, env=env, start_new_session=True)
    try:
        return subprocess.CompletedProcess(argv, proc.wait(timeout))
    except BaseException:
        # The leader has not been reaped (wait() raised), so its group id is still ours.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(proc.pid, sig)
            except (ProcessLookupError, PermissionError):
                pass
            end = time.monotonic() + grace_s
            while time.monotonic() < end and proc.poll() is None:
                time.sleep(0.05)
            if proc.poll() is not None:
                break
        server = _wineserver_for(wine)
        if server:
            try:
                subprocess.run([server, "-k"], env=env, timeout=30, check=False,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except (OSError, subprocess.TimeoutExpired):
                pass
        if proc.poll() is None:
            proc.wait()
        raise


def create_prefix(name: str, wine: str, env: dict[str, str] | None = None, run=None,
                  extra_env: dict[str, str] | None = None) -> int:
    """Create a dedicated prefix. Refuses to touch an existing path. Returns wine's exit status.

    Without an injected `run` (tests), wineboot runs in its own process group so a timeout
    or Ctrl-C can end it and its helpers (see run_in_group).
    """
    import subprocess

    plan = plan_create(name, wine, env, extra_env)
    path: Path = plan["path"]
    if path.exists():
        raise FileExistsError(f"prefix path already exists, not touching it: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    child = child_env(os.environ if env is None else env, plan["env_overrides"])
    try:
        if run is None:
            proc = run_in_group(plan["argv"], env=child, timeout=WINEBOOT_TIMEOUT_S, wine=wine)
        else:
            proc = run(plan["argv"], env=child, timeout=WINEBOOT_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"creating the prefix timed out after {WINEBOOT_TIMEOUT_S} s ({path}). gitfurl "
            "stopped the processes it started for it and asked this prefix's Wine server to "
            "exit. The prefix folder was kept; it may be incomplete.") from exc
    return proc.returncode
