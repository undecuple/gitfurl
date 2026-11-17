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


def create_prefix(name: str, wine: str, env: dict[str, str] | None = None, run=None,
                  extra_env: dict[str, str] | None = None) -> int:
    """Create a dedicated prefix. Refuses to touch an existing path. Returns wine's exit status."""
    import subprocess

    run = run or subprocess.run
    plan = plan_create(name, wine, env, extra_env)
    path: Path = plan["path"]
    if path.exists():
        raise FileExistsError(f"prefix path already exists, not touching it: {path}")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.mkdir(mode=0o700)
    try:
        proc = run(plan["argv"], env=child_env(os.environ if env is None else env, plan["env_overrides"]),
                   timeout=WINEBOOT_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"creating the prefix timed out after {WINEBOOT_TIMEOUT_S} s ({path}). Wine's own "
            "processes for it (wineboot and helpers) may still be running; gitfurl did not stop "
            "them. The prefix folder was kept.") from exc
    return proc.returncode
