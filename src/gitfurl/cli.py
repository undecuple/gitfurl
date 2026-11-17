from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gitfurl import __version__
from gitfurl.cleanup import MODES, perform_cleanup, plan_cleanup
from gitfurl.doctor import format_report, run_doctor
from gitfurl.prefixes import (
    create_prefix,
    list_prefixes,
    plan_create,
    prefixes_root,
    validate_prefix_name,
)
from gitfurl.runner import (
    RUNTIMES,
    RunOptions,
    build_plan,
    logs_root,
    run_tracked,
    validate_installer,
    validate_wine_debug,
)
from gitfurl.safety import ROOT_MESSAGE, running_as_root

DEFAULT_PREFIX = "default"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="gitfurl cli",
        description="Helper for running user-supplied Windows installers "
        "through Wine. Wine prefixes are not security sandboxes.",
    )
    p.add_argument("--version", action="version", version=f"gitfurl {__version__}")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="report the local environment (read-only)")
    inst = sub.add_parser("install", help="preview or run an installer")
    inst.add_argument("installer", help="path to a local installer file")
    inst.add_argument("--prefix", help="name of a dedicated prefix")
    inst.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    inst.add_argument("--create-prefix", action="store_true",
                      help="create the prefix first if it does not exist")
    inst.add_argument("--runtime", choices=RUNTIMES, default="wine",
                      help="wine (default) or proton, through umu-run")
    inst.add_argument("--proton-path", metavar="DIR",
                      default=os.environ.get("PROTONPATH"),
                      help="a Proton folder you already have (or set PROTONPATH); never downloaded")
    inst.add_argument("--find-exe", action="store_true",
                      help="after the run, guess which new program was installed (local only)")
    inst.add_argument("--progress", action="store_true",
                      help="print progress while the installer runs (payload read, rate, ETA)")
    inst.add_argument("--progress-interval", type=float, default=10.0, metavar="SECONDS")
    inst.add_argument("--detailed-log", action="store_true",
                      help="also write a JSON-lines event log next to the log")
    inst.add_argument("--wine-debug", metavar="CHANNELS",
                      help="set WINEDEBUG for the run (more output in the log), e.g. err+all")
    inst.add_argument("--show-changes", action="store_true",
                      help="after the run, list what the installer changed (files, registry keys)")
    inst.add_argument("--cleanup", choices=MODES, default="none",
                      help="after a successful install (exit status 0), delete the installer "
                           "file or its whole folder; default: none")
    inst.add_argument("--yes", action="store_true", help="approve the run without prompting")
    pre = sub.add_parser("prefix", help="manage dedicated prefixes")
    pre_sub = pre.add_subparsers(dest="prefix_command", required=True)
    create = pre_sub.add_parser("create", help="create a dedicated prefix")
    create.add_argument("name")
    create.add_argument("--yes", action="store_true", help="approve without prompting")
    pre_sub.add_parser("list", help="list dedicated prefixes")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "doctor":
        print(format_report(run_doctor()))
        return 0
    if args.command == "prefix":
        return _prefix_command(args)
    if not args.dry_run:
        return _real_install(args)
    try:
        installer = validate_installer(args.installer)
        plan = build_plan(installer, args.prefix or DEFAULT_PREFIX, runtime=args.runtime,
                          proton_path=args.proton_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print("DRY RUN: nothing was executed, created or changed.")
    print(f"command:     {plan.preview()}")
    print(f"prefix:      {plan.prefix}")
    print(f"working dir: {plan.cwd}")
    for k, v in plan.env_overrides.items():
        print(f"env:         {k}={v}")
    print("needed for a real run:")
    for item in plan.prerequisites:
        print(f"  - {item}")
    print("Running an installer can expose files the current user can access.")
    return 0


def _prefix_command(args) -> int:
    if args.prefix_command == "create" and running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    if args.prefix_command == "list":
        names = list_prefixes()
        print(f"prefix root: {prefixes_root()}")
        print("\n".join(names) if names else "(no prefixes)")
        return 0
    try:
        validate_prefix_name(args.name)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    wine = shutil.which("wine")
    if wine is None:
        print("error: wine not found on PATH; run 'gitfurl doctor'. Nothing was changed.",
              file=sys.stderr)
        return 1
    plan = plan_create(args.name, wine)
    print(f"This will create a new dedicated prefix at {plan['path']}")
    print(f"command: {' '.join(plan['argv'])}  (with WINEPREFIX set for that command only)")
    print("Your existing ~/.wine is not used or changed.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not approved: no terminal to ask on. Re-run with --yes to approve.")
            return 2
        if input("Type 'yes' to create it: ").strip() != "yes":
            print("Not approved. Nothing was changed.")
            return 2
    try:
        status = create_prefix(args.name, wine)
    except FileExistsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except subprocess.TimeoutExpired:
        print("error: wine did not finish in time; the prefix directory may be incomplete.",
              file=sys.stderr)
        return 1
    print(f"wine exited with status {status}")
    return 0 if status == 0 else 1


def _real_install(args) -> int:
    if running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    if not args.prefix:
        print("error: a real run needs an explicit --prefix NAME (see 'gitfurl prefix list').",
              file=sys.stderr)
        return 1
    try:
        installer = validate_installer(args.installer)
        plan = build_plan(installer, args.prefix, runtime=args.runtime,
                          proton_path=args.proton_path)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    tool = "umu-run" if args.runtime == "proton" else "wine"
    wine = shutil.which(tool)
    if wine is None:
        print(f"error: {tool} not found on PATH; run 'gitfurl cli doctor'. Nothing was run.",
              file=sys.stderr)
        return 1
    missing = not plan.prefix.is_dir()
    if missing and not args.create_prefix:
        print(f"error: prefix does not exist: {plan.prefix}. Create it first with "
              f"'gitfurl prefix create {args.prefix}'. Nothing was run. Or add --create-prefix.", file=sys.stderr)
        return 1
    try:
        if args.wine_debug:
            validate_wine_debug(args.wine_debug)
        if args.progress_interval <= 0:
            raise ValueError("--progress-interval must be above 0")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        clean = plan_cleanup(installer, args.cleanup, [plan.prefix, prefixes_root(), logs_root()])
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    print(f"About to run: {plan.preview()}")
    print(f"prefix: {plan.prefix}" + (" (will be created first)" if missing else ""))
    print("Wine prefixes are not sandboxes: the installer can reach files you can reach.")
    print("Only run installers you are authorised to use and have chosen to trust.")
    if args.cleanup != "none":
        print("After the installer exits with status 0, " + clean.describe())
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not approved: no terminal to ask on. Re-run with --yes to approve.")
            return 2
        if input("Type 'yes' to run the installer: ").strip() != "yes":
            print("Not approved. Nothing was run.")
            return 2
    try:
        options = RunOptions(
            on_progress=(lambda smp: print(f"[progress] {smp.line()}", flush=True))
            if args.progress else None,
            progress_interval=args.progress_interval,
            detailed_log=args.detailed_log, wine_debug=args.wine_debug)
        status, log_path, changes = run_tracked(
            plan, wine, args.create_prefix, args.show_changes or args.find_exe, options=options)
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted; the installer may still be running or half-finished.",
              file=sys.stderr)
        return 130
    print(f"installer exited with status {status}")
    if args.cleanup != "none":
        if status == 0:
            deleted, problems = perform_cleanup(clean, status)
            print(f"cleanup: deleted {deleted} file(s)")
            for item in problems:
                print(f"cleanup problem: {item}", file=sys.stderr)
        else:
            print("cleanup: skipped, the installer did not exit with status 0")
    if changes is not None:
        if args.show_changes:
            print(changes.describe())
        if args.find_exe:
            print(changes.describe_executables(plan.prefix))
    if args.detailed_log:
        print(f"event log: {Path(log_path).with_suffix('.events.jsonl')}")
    print(f"local log: {log_path} (may contain your username and local paths)")
    if status != 0:
        print("A non-zero status does not by itself say why it failed; read the log.")
    return 0 if status == 0 else 1
