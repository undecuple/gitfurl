"""Execution planning. M1 only builds and previews a plan; it never runs anything."""

from __future__ import annotations

import shlex
from dataclasses import dataclass, field
from pathlib import Path

from gitfurl.prefixes import child_env as build_child_env
from gitfurl.prefixes import prefix_path


@dataclass(frozen=True)
class InstallPlan:
    argv: list[str]
    prefix: Path
    cwd: Path
    env_overrides: dict[str, str] = field(default_factory=dict)
    prerequisites: list[str] = field(default_factory=list)
    runtime: str = "wine"

    def preview(self) -> str:
        """Readable preview only. The real call would use argv, never a shell string."""
        return shlex.join(self.argv)


def validate_installer(path_str: str) -> Path:
    path = Path(path_str)
    if not path.exists():
        raise ValueError(f"installer not found: {path}")
    if not path.is_file():
        raise ValueError(f"installer is not a regular file: {path}")
    return path.resolve()


RUNTIMES = ("wine", "proton")


def validate_proton_path(value: str | None) -> Path:
    """A Proton folder you chose (it must contain the 'proton' script). Never downloaded."""
    if not value:
        raise ValueError("the proton runtime needs a Proton folder: pass --proton-path or set "
                         "PROTONPATH (gitfurl does not download one)")
    path = Path(value).expanduser()
    if not (path / "proton").is_file():
        raise ValueError(f"not a Proton folder (no 'proton' file inside): {path}")
    return path.resolve()


def runtime_env(runtime: str, proton_path: str | None) -> dict[str, str]:
    """Environment overrides a runtime needs besides WINEPREFIX."""
    if runtime == "wine":
        return {}
    if runtime != "proton":
        raise ValueError(f"unknown runtime: {runtime}")
    return {"PROTONPATH": str(validate_proton_path(proton_path)), "GAMEID": "umu-0"}


def build_plan(installer: Path, prefix_name: str, env: dict[str, str] | None = None,
               runtime: str = "wine", proton_path: str | None = None) -> InstallPlan:
    prefix = prefix_path(prefix_name, env)
    extra = runtime_env(runtime, proton_path)
    if runtime == "proton":
        needs = [
            "umu-run available on PATH",
            ("the Steam Linux Runtime, which umu-run downloads on its first run unless it is "
             "already present (a network download; gitfurl does not start it for you)"),
        ]
    else:
        needs = ["wine available on PATH (run 'gitfurl doctor')"]
    return InstallPlan(
        argv=["umu-run" if runtime == "proton" else "wine", str(installer)],
        prefix=prefix,
        cwd=installer.parent,
        env_overrides={"WINEPREFIX": str(prefix), **extra},
        prerequisites=[*needs, f"dedicated prefix at {prefix}, created with approval"],
        runtime=runtime,
    )


@dataclass
class RunOptions:
    """Optional extras for a run. Everything here is off by default."""

    on_progress: object = None  # callable(Sample), enables the progress monitor
    progress_interval: float = 10.0
    detailed_log: bool = False  # also write a JSON-lines event log
    wine_debug: str | None = None  # WINEDEBUG channels, e.g. "err+all"


_WINEDEBUG_OK = None


def validate_wine_debug(value: str) -> str:
    import re

    if not re.fullmatch(r"[A-Za-z0-9+,_\-]{1,200}", value):
        raise ValueError("invalid --wine-debug value: use letters, digits and + , - _ only")
    return value


def logs_root(env: dict[str, str] | None = None) -> Path:
    import os

    env = os.environ if env is None else env
    base = env.get("XDG_STATE_HOME") or str(Path(env.get("HOME", "~")).expanduser() / ".local/state")
    return Path(base) / "gitfurl" / "logs"


def execute(plan: InstallPlan, wine: str, env: dict[str, str] | None = None, run=None,
            options: RunOptions | None = None):
    """Run the installer from an approved plan. Returns (exit status, log path).

    Output goes to a local log file only. The environment values are never written to the log.
    With options, it can also report progress and write a JSON-lines event log next to it.
    """
    import json
    import os
    import subprocess
    import time
    from datetime import datetime

    options = options or RunOptions()
    run = run or subprocess.run
    if not plan.prefix.is_dir():
        raise FileNotFoundError(f"prefix does not exist: {plan.prefix} (run 'gitfurl prefix create')")
    log_dir = logs_root(env)
    log_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_path = log_dir / f"{plan.prefix.name}-{stamp}.log"
    argv = [wine, *plan.argv[1:]]
    overrides = dict(plan.env_overrides)
    if options.wine_debug:
        overrides["WINEDEBUG"] = validate_wine_debug(options.wine_debug)
    child_env = build_child_env(os.environ if env is None else env, overrides)
    events = None
    if options.detailed_log:
        efd = os.open(log_dir / f"{plan.prefix.name}-{stamp}.events.jsonl",
                      os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        events = os.fdopen(efd, "w")

    def event(kind, **data):
        if events:
            events.write(json.dumps({"t": datetime.now().isoformat(timespec="seconds"),
                                     "event": kind, **data}) + "\n")
            events.flush()

    monitor = None
    if options.on_progress is not None:
        from gitfurl.progress import ProgressMonitor

        def handle(sample):
            event("progress", elapsed_s=round(sample.elapsed_s), read_bytes=sample.read_bytes,
                  total_bytes=sample.total_bytes, written_bytes=sample.written_bytes,
                  percent=None if sample.percent is None else round(sample.percent, 2))
            options.on_progress(sample)

        monitor = ProgressMonitor(plan.argv and Path(plan.argv[-1]), plan.prefix, handle,
                                  options.progress_interval)
    t0 = time.monotonic()
    # Variable names only, never values: the environment can hold secrets.
    event("start", command=shlex.join(argv), cwd=str(plan.cwd), prefix=str(plan.prefix),
          env_overrides=sorted(overrides), wine=wine)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as log:
            log.write(f"command: {shlex.join(argv)}\ncwd: {plan.cwd}\nprefix: {plan.prefix}\n---\n")
            log.flush()
            if monitor:
                monitor.start()
            try:
                proc = run(argv, cwd=plan.cwd, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                           check=False)
            finally:
                final = monitor.stop() if monitor else None
            log.write(f"\n--- exit status {proc.returncode}\n")
        if final is not None:
            event("progress_final", line=final.line())
        event("exit", status=proc.returncode, seconds=round(time.monotonic() - t0, 1))
    finally:
        if events:
            events.close()
    return proc.returncode, log_path


def run_with_prefix(plan: InstallPlan, wine: str, create_if_missing: bool,
                    env: dict[str, str] | None = None, run=None, on_ready=None,
                    options: RunOptions | None = None):
    """Create the plan's prefix first when asked and needed, then run the installer.

    Returns (exit status, log path). If creating the prefix fails, the installer is not run.
    """
    from gitfurl.prefixes import create_prefix

    if not plan.prefix.is_dir():
        if not create_if_missing:
            raise FileNotFoundError(
                f"prefix does not exist: {plan.prefix} (run 'gitfurl cli prefix create')"
            )
        extra = {k: v for k, v in plan.env_overrides.items() if k != "WINEPREFIX"}
        status = create_prefix(plan.prefix.name, wine, env, run, extra_env=extra)
        if status != 0:
            raise RuntimeError(f"creating the prefix failed (wine exited {status}); "
                               "the installer was not run")
    if on_ready is not None:
        on_ready()  # the prefix exists; e.g. take a "before" snapshot
    return execute(plan, wine, env, run, options)


def run_tracked(plan: InstallPlan, wine: str, create_if_missing: bool, track: bool,
                env: dict[str, str] | None = None, run=None, options: RunOptions | None = None):
    """Like run_with_prefix, and when track is true also report what the installer changed.

    Returns (exit status, log path, ChangeReport or None).
    """
    if not track:
        status, log = run_with_prefix(plan, wine, create_if_missing, env, run, options=options)
        return status, log, None
    from gitfurl.changes import compare, take_snapshot

    skip = [logs_root(env), plan.prefix.parent.parent]  # gitfurl's own data and state
    box = {}

    def before():
        box["b"] = take_snapshot(plan.prefix, skip=[logs_root(env)])

    status, log = run_with_prefix(plan, wine, create_if_missing, env, run, on_ready=before,
                                  options=options)
    after = take_snapshot(plan.prefix, skip=skip[:1])
    return status, log, compare(box["b"], after, plan.prefix)
