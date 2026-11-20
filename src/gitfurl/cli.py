from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gitfurl import __version__
from gitfurl.cleanup import MODES, perform_cleanup, plan_cleanup
from gitfurl.detect import describe as describe_detection
from gitfurl.detect import detect
from gitfurl.doctor import format_report, run_doctor
from gitfurl.memlimit import MemoryLimitError
from gitfurl.prefixes import (
    create_prefix,
    list_prefixes,
    plan_create,
    prefixes_root,
    validate_prefix_name,
)
from gitfurl.preflight import measure
from gitfurl.runner import (
    CANCEL_REMAINS,
    CANCELLED_STATUS,
    RUNTIMES,
    RunControl,
    RunOptions,
    build_plan,
    describe_result,
    logs_root,
    run_tracked,
    validate_installer,
    validate_wine_debug,
)
from gitfurl.safety import ROOT_MESSAGE, running_as_root
from gitfurl.scan import DEFAULT_TIMEOUT_S as SCAN_TIMEOUT_S

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
    inst.add_argument("--portable", action="store_true",
                      help="run the installer inside bwrap with only the prefix writable, no "
                           "network and no menu entries (reduces changes outside the prefix; "
                           "not a guarantee); default: off")
    inst.add_argument("--unattended", action="store_true",
                      help="Inno Setup installers only: run without the installer's wizard and "
                           "windows (Inno's /VERYSILENT switches), install to C:\\Games\\PREFIX "
                           "in the prefix, and read Inno's log afterwards. The installer still "
                           "does the unpacking. Default: off")
    inst.add_argument("--no-memory-limit", action="store_true",
                      help="run without the memory limit. By default the run is put in a "
                           "systemd user scope with a soft and a hard memory limit, which reduces "
                           "the chance that the run pushes the system out of memory")
    inst.add_argument("--find-exe", action="store_true",
                      help="after the run, guess which new program was installed (local only)")
    inst.add_argument("--progress", action="store_true",
                      help="print progress while the installer runs (payload read, rate, ETA)")
    inst.add_argument("--stall-after", type=float, default=None, metavar="SECONDS",
                      help="print one advisory note if nothing moves (no payload read, no CPU "
                           "use, no log growth) for this long. Off by default. It never stops "
                           "anything")
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
    inst.add_argument("--scan", action="store_true",
                      help="scan the installer and the files beside it with the clamscan on PATH "
                           "first; a match or a scan error stops the run (a scan error can "
                           "not be overridden). Off by default. "
                           "gitfurl never updates the ClamAV database")
    inst.add_argument("--allow-scan-hit", action="store_true",
                      help="with --scan: run even if ClamAV matched a signature (false positives "
                           "are common for game installers). Never overrides a scan error, "
                           "also not a match in a scan that had an error")
    inst.add_argument("--scan-timeout", type=float, default=SCAN_TIMEOUT_S, metavar="SECONDS",
                      help=f"with --scan: stop the scan after this long (default {SCAN_TIMEOUT_S:g})")
    inst.add_argument("--yes", action="store_true", help="approve the run without prompting")
    ext = sub.add_parser(
        "extract", help="extract FreeArc archives (storing, lzma, exe, delta and rep) into a "
                        "prefix without running the installer")
    ext.add_argument("installer", help="the installer; FreeArc archives beside it are read")
    ext.add_argument("--prefix", required=True, help="name of an existing dedicated prefix; "
                     "files go to C:\\Games\\PREFIX in it")
    ext.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    ext.add_argument("--yes", action="store_true", help="approve without prompting")
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
    if args.command == "extract":
        return _extract_command(args)
    if not args.dry_run:
        return _real_install(args)
    try:
        installer = validate_installer(args.installer)
        scan_plan = _scan_plan(args, installer)
        plan = build_plan(installer, args.prefix or DEFAULT_PREFIX, runtime=args.runtime,
                          proton_path=args.proton_path, portable=args.portable, scan=scan_plan,
                          memory_limit=not args.no_memory_limit,
                          unattended=_unattended_plan(args, args.prefix or DEFAULT_PREFIX,
                                                      installer))
        portable_lines = _portable_lines(plan, installer, shutil.which("wine") or "wine",
                                         full=True) if args.portable else []
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
    for line in portable_lines:
        print(line)
    for line in _unattended_lines(plan):
        print(line)
    if scan_plan is not None:
        from gitfurl.scan import describe_scan_plan

        for line in describe_scan_plan(scan_plan):
            print(line)
        print("  database date: not checked in a dry run (see 'gitfurl cli doctor')")
    for line in _memory_lines(plan)[0]:
        print(line)
    for line in describe_detection(detect(installer)).splitlines():
        print(line)
    print("preflight (read-only, advisory):")
    for line in measure(installer, plan.prefix).describe().splitlines():
        print(f"  {line}")
    print("Running an installer can expose files the current user can access.")
    return 0


def _unattended_plan(args, prefix_name, installer):
    """The unattended plan when --unattended is given, else None. Reads the installer's
    headers only. Raises ValueError (UnattendedRefused) when it is not Inno Setup."""
    if not args.unattended:
        return None
    from gitfurl.unattended import plan_unattended

    return plan_unattended(prefix_name, detect(installer))


def _unattended_lines(plan) -> list[str]:
    return plan.unattended.describe(plan.prefix) if plan.unattended is not None else []


def _scan_plan(args, installer):
    """The ClamAV scan plan when --scan is given, else None. Runs nothing. Raises ValueError."""
    if not args.scan:
        if args.allow_scan_hit:
            raise ValueError("--allow-scan-hit only has a meaning together with --scan")
        return None
    from gitfurl.scan import build_scan_plan, find_clamscan

    if not math.isfinite(args.scan_timeout) or args.scan_timeout <= 0:
        raise ValueError("--scan-timeout must be a finite number above 0")
    return build_scan_plan(installer, find_clamscan(), args.scan_timeout)


def _portable_lines(plan, installer, wine: str, full: bool) -> list[str]:
    """Description of a portable run, with the real bwrap argument list. Raises ValueError."""
    import os
    from dataclasses import replace

    from gitfurl.portable import check_portable_supported, describe_portable, find_bwrap

    bwrap = find_bwrap()
    env = dict(os.environ)
    wrapped = replace(plan, argv=[wine, *plan.argv[1:]])
    check_portable_supported(wrapped, bwrap, env, installer)
    return describe_portable(wrapped, bwrap, installer, env, full_argv=full)


def _memory_lines(plan) -> tuple[list[str], str | None]:
    """The memory limit's plan lines and the problem that stops a run (None if none).

    Reads files only (PATH, the user manager's socket, cgroup files, /proc/meminfo)."""
    from gitfurl.memlimit import availability, describe

    return describe(plan.memory_limit, availability() if plan.memory_limit else None)


MEMORY_OFF_HINT = ("Nothing was run. To run without the memory limit, add --no-memory-limit "
                   "(the run can then use all free memory).")


def _prefix_interrupt_message(exc: BaseException) -> str:
    notes = getattr(exc, "notes", None)
    done = "; ".join(notes) if notes else "no cleanup details are available"
    return ("interrupted while creating the prefix. What gitfurl did: " + done
            + ". The prefix folder was kept and may be incomplete.")


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
    except KeyboardInterrupt as exc:
        print(_prefix_interrupt_message(exc), file=sys.stderr)
        return 130
    except (RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"error: {exc}" if isinstance(exc, RuntimeError) else
              "error: wine did not finish in time; the prefix directory may be incomplete.",
              file=sys.stderr)
        return 1
    print(f"wine exited with status {status}")
    return 0 if status == 0 else 1


def _extract_command(args) -> int:
    """Native extraction: plan, approval, then gitfurl's own FreeArc reader writes the files."""
    from gitfurl.extract import ExtractRefused, extract, plan_extract

    if not args.dry_run and running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    try:
        installer = validate_installer(args.installer)
        plan = plan_extract(installer, args.prefix)
    except ValueError as exc:  # ExtractRefused is a ValueError
        print(f"error: {exc}. Nothing was written.", file=sys.stderr)
        return 1
    problem = plan.problem()
    if args.dry_run:
        print("DRY RUN: nothing was extracted, created or changed.")
    for line in plan.describe():
        print(line)
    print("Only extract installers you are authorised to use.")
    if args.dry_run:
        if problem:
            print(f"a real extraction would stop now: {problem}")
        return 0
    if problem:
        print(f"error: {problem}. Nothing was written.", file=sys.stderr)
        return 1
    print("Ctrl-C stops the extraction; files already written are kept.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not approved: no terminal to ask on. Re-run with --yes to approve.")
            return 2
        if input("Type 'yes' to extract the files: ").strip() != "yes":
            print("Not approved. Nothing was written.")
            return 2
    try:
        result = extract(plan)
    except ExtractRefused as exc:
        print(f"error: {exc}. {exc.outcome()}", file=sys.stderr)
        return 1
    for line in result.lines():
        print(line)
    if result.stopped:
        return 130
    return 0 if result.ok else 1


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
        scan_plan = _scan_plan(args, installer)
        plan = build_plan(installer, args.prefix, runtime=args.runtime,
                          proton_path=args.proton_path, portable=args.portable, scan=scan_plan,
                          memory_limit=not args.no_memory_limit,
                          unattended=_unattended_plan(args, args.prefix, installer))
        if args.portable and args.runtime != "wine":
            raise ValueError("portable install is only supported with the wine runtime")
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    tool = "umu-run" if args.runtime == "proton" else "wine"
    wine = shutil.which(tool)
    if wine is None:
        print(f"error: {tool} not found on PATH; run 'gitfurl cli doctor'. Nothing was run.",
              file=sys.stderr)
        return 1
    portable_lines: list[str] = []
    if args.portable:
        try:
            portable_lines = _portable_lines(plan, installer, wine, full=True)
        except ValueError as exc:
            print(f"error: {exc}. Nothing was run.", file=sys.stderr)
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
        if args.stall_after is not None and args.stall_after <= 0:
            raise ValueError("--stall-after must be above 0")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    try:
        clean = plan_cleanup(installer, args.cleanup, [plan.prefix, prefixes_root(), logs_root()])
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    memory_lines, memory_problem = _memory_lines(plan)
    if memory_problem:
        for line in memory_lines:
            print(line)
        print(f"error: {memory_problem}. {MEMORY_OFF_HINT}", file=sys.stderr)
        return 2
    print(f"About to run: {plan.preview()}")
    print(f"prefix: {plan.prefix}" + (" (will be created first)" if missing else ""))
    if args.portable:
        for line in portable_lines:
            print(line)
    else:
        print("Wine prefixes are not sandboxes: the installer can reach files you can reach.")
    for line in _unattended_lines(plan):
        print(line)
    for line in memory_lines:
        print(line)
    print("Only run installers you are authorised to use and have chosen to trust.")
    for line in describe_detection(detect(installer)).splitlines():
        print(line)
    print("preflight (read-only, advisory):")
    for line in measure(installer, plan.prefix).describe().splitlines():
        print(f"  {line}")
    if scan_plan is not None:
        from gitfurl.scan import database_info, describe_scan_plan

        # A bounded version probe (as doctor runs), for the database date. The scan itself
        # runs only after approval.
        for line in describe_scan_plan(scan_plan, database_info(scan_plan.clamscan)):
            print(line)
        print("The scan runs first, after you approve. A match stops the run"
              + (" unless --allow-scan-hit is given" if args.allow_scan_hit else "")
              + "; a scan error always stops it and is never overridden (also when a "
              "signature matched in a scan that had an error).")
    print("Ctrl-C stops this run gracefully (see docs/user-guide.md for what it leaves behind).")
    if args.cleanup != "none":
        print("After the installer exits with status 0, " + clean.describe())
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not approved: no terminal to ask on. Re-run with --yes to approve.")
            return 2
        if input("Type 'yes' to run the installer: ").strip() != "yes":
            print("Not approved. Nothing was run.")
            return 2
    control = RunControl()
    try:
        options = RunOptions(
            on_progress=(lambda smp: print(f"[progress] {smp.line()}", flush=True))
            if args.progress else None,
            progress_interval=args.progress_interval,
            detailed_log=args.detailed_log, wine_debug=args.wine_debug, control=control,
            stall_after_s=args.stall_after,
            on_note=(lambda note: print(f"[note] {note}", flush=True)),
            allow_scan_hit=args.allow_scan_hit)
        result = run_tracked(
            plan, wine, args.create_prefix, args.show_changes or args.find_exe, options=options)
        status, log_path, changes = result
    except MemoryLimitError as exc:
        print(f"error: {exc}. {MEMORY_OFF_HINT}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        if getattr(exc, "scan_interrupted", False):
            print("interrupted during the ClamAV scan. " + "; ".join(exc.notes) + ". Nothing "
                  "was run or created.", file=sys.stderr)
        elif hasattr(exc, "notes"):  # interrupted while the prefix was being created
            print(_prefix_interrupt_message(exc), file=sys.stderr)
            print("The installer was not started.", file=sys.stderr)
        else:
            print("interrupted; the installer may still be running or half-finished.",
                  file=sys.stderr)
        return 130
    if control.cancelled:
        print("The run was cancelled, not completed.")
        if describe_result(result):
            print(describe_result(result))
        print(CANCEL_REMAINS)
        for note in control.notes:
            print(f"note: {note}")
        if args.cleanup != "none":
            print("cleanup: skipped, the run was cancelled")
        if args.detailed_log:
            print(f"event log: {Path(log_path).with_suffix('.events.jsonl')}")
        print(f"local log: {log_path} (may contain your username and local paths)")
        return CANCELLED_STATUS
    print(f"installer exited with status {status}")
    if describe_result(result):
        print(describe_result(result))
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
            if args.portable:
                print("Portable run: this report covers the checked places only; it says "
                      "nothing about places gitfurl did not check.")
        if args.find_exe:
            print(changes.describe_executables(plan.prefix))
    if args.detailed_log:
        print(f"event log: {Path(log_path).with_suffix('.events.jsonl')}")
    print(f"local log: {log_path} (may contain your username and local paths)")
    if status != 0:
        print("A non-zero status does not by itself say why it failed; read the log.")
    return 0 if status == 0 else 1
