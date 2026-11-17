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


def _cleanup_group(proc, env: dict[str, str], wine: str, grace_s: float, guard) -> list[str]:
    """End the group `proc` leads, then ask this prefix's Wine server to exit. Returns notes.

    The leader is never reaped here until the end (liveness is read from /proc), so its
    group id stays ours. KILL goes to the group whenever any member is left after the grace
    period, even if the leader itself already exited on TERM. A Ctrl-C held by `guard` ends
    the grace wait early. Every step runs even if an earlier one fails.
    """
    import signal
    import subprocess
    import time

    from gitfurl.procgroup import group_alive, wineserver_for

    notes: list[str] = []
    pgid = proc.pid

    def wait_gone(seconds: float, interruptible: bool) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end and not (interruptible and guard.hits) and group_alive(pgid):
            time.sleep(0.05)
        return not group_alive(pgid)

    try:
        if group_alive(pgid):
            for sig, seconds in ((signal.SIGTERM, grace_s), (signal.SIGKILL, 3.0)):
                try:
                    os.killpg(pgid, sig)
                except (ProcessLookupError, PermissionError):
                    pass
                if wait_gone(seconds, sig == signal.SIGTERM):
                    break
                if sig == signal.SIGTERM:
                    notes.append("a second Ctrl-C ended the wait: remaining processes were "
                                 "force-killed" if guard.hits else
                                 f"some processes did not stop within {grace_s:g} s and were "
                                 "force-killed")
            if group_alive(pgid):
                notes.append("could not confirm that every process it started had ended")
            else:
                notes.append("the processes it started were stopped")
        else:
            notes.append("no process it started was still running")
    except Exception as exc:  # noqa: BLE001 - cleanup must go on to the next step
        notes.append(f"stopping the processes failed: {exc}")
    server = wineserver_for(wine)
    if not server:
        notes.append("wineserver was not found, so this prefix's Wine server was not asked to "
                     "exit")
    else:
        try:
            done = subprocess.run([server, "-k"], env=env, timeout=30, check=False,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            notes.append("asked this prefix's Wine server to exit (wineserver -k)"
                         if done.returncode == 0 else
                         f"wineserver -k for this prefix exited with status {done.returncode}")
        except (OSError, subprocess.TimeoutExpired) as exc:
            notes.append(f"could not ask this prefix's Wine server to exit: {exc}")
    try:
        proc.wait(timeout=10)  # reap last
    except subprocess.TimeoutExpired:
        notes.append("the first process did not end within 10 s of the stop (it may be stuck "
                     "in the kernel); it was not waited for further")
    return notes


def run_in_group(argv: list[str], env: dict[str, str], timeout: float, wine: str,
                 grace_s: float = 5.0):
    """Run argv in its own process group and wait up to `timeout` seconds.

    On a timeout or Ctrl-C only that group is signalled (TERM, then KILL), then this
    prefix's own Wine server is asked to exit. Nothing is found by name; a program that left
    the group is out of reach. A second Ctrl-C during the cleanup is held back and only
    shortens the grace wait. The exception that ends the wait (subprocess.TimeoutExpired or
    KeyboardInterrupt) is re-raised with a `notes` list saying what was really done.
    """
    import subprocess

    from gitfurl.procgroup import SigintGuard

    guard = SigintGuard()
    guard.__enter__()  # a Ctrl-C from here to the wait is held, then replayed
    try:
        proc = subprocess.Popen(argv, env=env, start_new_session=True)
        try:
            guard.release()
            if guard.hits:
                raise KeyboardInterrupt
            return subprocess.CompletedProcess(argv, proc.wait(timeout))
        except BaseException as exc:
            guard.hits = 0  # a Ctrl-C held before the start is this exception, not a second one
            guard.__enter__()
            exc.notes = _cleanup_group(proc, env, wine, grace_s, guard)
            raise
    finally:
        guard.release()


def create_prefix(name: str, wine: str, env: dict[str, str] | None = None, run=None,
                  extra_env: dict[str, str] | None = None, wrap=None) -> int:
    """Create a dedicated prefix. Refuses to touch an existing path. Returns wine's exit status.

    Without an injected `run` (tests), wineboot runs in its own process group so a timeout
    or Ctrl-C can end it and its helpers (see run_in_group). `wrap`, if given, turns the
    wineboot argv into the argv to start (the memory limit's systemd-run scope).
    """
    import subprocess

    plan = plan_create(name, wine, env, extra_env)
    path: Path = plan["path"]
    if path.exists():
        raise FileExistsError(f"prefix path already exists, not touching it: {path}")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        path.mkdir(mode=0o700)
    except KeyboardInterrupt as exc:
        exc.notes = ["wineboot had not been started yet; nothing was run"]
        raise
    child = child_env(os.environ if env is None else env, plan["env_overrides"])
    argv = wrap(plan["argv"]) if wrap else plan["argv"]
    try:
        if run is None:
            proc = run_in_group(argv, env=child, timeout=WINEBOOT_TIMEOUT_S, wine=wine)
        else:
            proc = run(argv, env=child, timeout=WINEBOOT_TIMEOUT_S, check=False)
    except subprocess.TimeoutExpired as exc:
        done = "; ".join(getattr(exc, "notes", [])) or "no cleanup details"
        raise RuntimeError(
            f"creating the prefix timed out after {WINEBOOT_TIMEOUT_S} s ({path}). What "
            f"gitfurl did: {done}. The prefix folder was kept; it may be incomplete.") from exc
    return proc.returncode
