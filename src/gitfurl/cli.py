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
from gitfurl.route import ROUTE_CHOICES, ROUTE_NATIVE, choose_route
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
    inst.add_argument("--route", choices=ROUTE_CHOICES, default="auto",
                      help="auto (default): gitfurl picks the route and says why. native: "
                           "gitfurl unpacks the FreeArc archives beside an Inno Setup installer "
                           "itself and does not run Setup (refused, with the reason, when that "
                           "is not possible). wine: Setup runs under Wine/Proton")
    inst.add_argument("--unattended", action="store_true",
                      help="Wine route only. Inno Setup installers only: run without the installer's wizard and "
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
        "extract", help="files only: extract FreeArc archives (storing, lzma, exe, delta and "
                        "rep) into an existing prefix without running the installer. 'install' "
                        "picks this route itself when it can")
    ext.add_argument("installer", help="the installer; FreeArc archives beside it are read")
    ext.add_argument("--prefix", required=True, help="name of an existing dedicated prefix; "
                     "files go to C:\\Games\\PREFIX in it")
    ext.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    ext.add_argument("--yes", action="store_true", help="approve without prompting")
    ixt = sub.add_parser(
        "extract-inno", help="extract the files an Inno Setup installer holds for its program "
                             "folder into a prefix, without running the installer")
    ixt.add_argument("installer", help="the Inno Setup installer (setup.exe)")
    ixt.add_argument("--prefix", required=True, help="name of an existing dedicated prefix; "
                     "files go to C:\\Games\\PREFIX in it")
    ixt.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    ixt.add_argument("--yes", action="store_true", help="approve without prompting")
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
    if args.command == "extract-inno":
        return _extract_inno_command(args)
    try:
        installer = validate_installer(args.installer)
        det = detect(installer)  # once; passed on
        choice = choose_route(installer, args.prefix or DEFAULT_PREFIX, det, args.route,
                              setup_options=_native_unused(args))
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    if choice.route == ROUTE_NATIVE:
        return _native_install(args, installer, det, choice)
    if not args.dry_run:
        return _real_install(args, det, choice)
    try:
        scan_plan = _scan_plan(args, installer)
        plan = build_plan(installer, args.prefix or DEFAULT_PREFIX, runtime=args.runtime,
                          proton_path=args.proton_path, portable=args.portable, scan=scan_plan,
                          memory_limit=not args.no_memory_limit,
                          unattended=_unattended_plan(args, args.prefix or DEFAULT_PREFIX,
                                                      det))
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
    for line in describe_detection(det, choice).splitlines():
        print(line)
    print("preflight (read-only, advisory):")
    for line in measure(installer, plan.prefix).describe().splitlines():
        print(f"  {line}")
    print("Running an installer can expose files the current user can access.")
    return 0


def _unattended_plan(args, prefix_name, det):
    """The unattended plan when --unattended is given, else None. Uses the detection of the
    installer's headers. Raises ValueError (UnattendedRefused) when it is not Inno Setup."""
    if not args.unattended:
        return None
    from gitfurl.unattended import plan_unattended

    return plan_unattended(prefix_name, det)


def _confirm(prompt: str, nothing: str) -> int | None:
    """None when approved (--yes is checked by the caller); otherwise the exit status 2."""
    if not sys.stdin.isatty():
        print("Not approved: no terminal to ask on. Re-run with --yes to approve.")
        return 2
    if input(prompt).strip() != "yes":
        print(f"Not approved. {nothing}")
        return 2
    return None


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
        refused = _confirm("Type 'yes' to extract the files: ", "Nothing was written.")
        if refused is not None:
            return refused
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


def _extract_inno_command(args) -> int:
    """Inno Setup's own files: plan, approval, then gitfurl's own reader writes them."""
    from gitfurl.extract import ExtractRefused
    from gitfurl.inno import extract_inno_files, plan_inno_extract

    if not args.dry_run and running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    try:
        installer = validate_installer(args.installer)
        plan = plan_inno_extract(installer, args.prefix)
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
    print("Ctrl-C stops the extraction; files already written and checked are kept.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not approved: no terminal to ask on. Re-run with --yes to approve.")
            return 2
        if input("Type 'yes' to extract the files: ").strip() != "yes":
            print("Not approved. Nothing was written.")
            return 2
    try:
        result = extract_inno_files(plan)
    except ExtractRefused as exc:
        print(f"error: {exc}. {exc.outcome()}", file=sys.stderr)
        return 1
    for line in result.lines():
        print(line)
    if result.stopped:
        return 130
    return 0 if result.ok else 1


def _real_install(args, det, choice) -> int:
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
                          unattended=_unattended_plan(args, args.prefix, det))
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
    for line in describe_detection(det, choice).splitlines():
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
        refused = _confirm("Type 'yes' to run the installer: ", "Nothing was run.")
        if refused is not None:
            return refused
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
        sys.stdout.flush()
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
    sys.stdout.flush()
    return 0 if status == 0 else 1


def _native_unused(args) -> list[str]:
    """Options the user set that only a Setup run uses. With --route auto they choose the
    Wine/Proton route; with --route native the plan lists them as not applying."""
    out = []
    for flag, on in (("--portable", args.portable), ("--unattended", args.unattended),
                     ("--wine-debug", bool(args.wine_debug)),
                     ("--show-changes", args.show_changes),
                     ("--stall-after", args.stall_after is not None),
                     ("--runtime proton", args.runtime == "proton")):
        if on:
            out.append(flag)
    return out


def _native_install(args, installer, det, choice) -> int:
    """The native route: Setup is not run; gitfurl unpacks the archives itself after one
    approval of the whole plan (scan, prefix creation, unpack, listings, cleanup)."""
    from gitfurl.extract import ExtractRefused
    from gitfurl.route import (
        PREFIX_ALLOWANCE,
        NativeInstallPlan,
        memory_problem,
        native_prefix_create,
        run_native,
    )

    dry = args.dry_run
    if not dry and running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    if not dry and not args.prefix:
        print("error: a real run needs an explicit --prefix NAME (see 'gitfurl prefix list').",
              file=sys.stderr)
        return 1
    xplan = choice.extract
    prefix_name = xplan.prefix_name
    try:
        if args.progress_interval <= 0:
            raise ValueError("--progress-interval must be above 0")
        scan_plan = _scan_plan(args, installer)
        clean = plan_cleanup(installer, args.cleanup, [xplan.prefix, prefixes_root(), logs_root()])
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    missing = not xplan.prefix.is_dir()
    create = missing and args.create_prefix
    wine = shutil.which("wine") if create else None
    if not dry and missing and not args.create_prefix:
        print(f"error: prefix does not exist: {xplan.prefix}. Create it first with "
              f"'gitfurl prefix create {prefix_name}', or add --create-prefix. Nothing was run.",
              file=sys.stderr)
        return 1
    if not dry and create and wine is None:
        print("error: wine not found on PATH; it is needed to create the prefix. Run 'gitfurl "
              "cli doctor'. Nothing was run.", file=sys.stderr)
        return 1
    memory_lines: list[str] = []
    scope_problem = None
    if create:
        from gitfurl.memlimit import availability
        from gitfurl.memlimit import describe as describe_memory

        enabled = not args.no_memory_limit
        memory_lines, scope_problem = describe_memory(enabled,
                                                      availability() if enabled else None)
        memory_lines = [f"for the prefix creation: {ln}" if i == 0 else ln
                        for i, ln in enumerate(memory_lines)]
        if scope_problem and not dry:
            for line in memory_lines:
                print(line)
            print(f"error: {scope_problem}. {MEMORY_OFF_HINT}", file=sys.stderr)
            return 2
    scan_lines: list[str] = []
    if scan_plan is not None:
        from gitfurl.scan import database_info, describe_scan_plan

        scan_lines = describe_scan_plan(scan_plan) if dry else \
            describe_scan_plan(scan_plan, database_info(scan_plan.clamscan))
    plan = NativeInstallPlan(installer, prefix_name, xplan.prefix, choice, create, wine,
                             scan_lines,
                             clean.describe() if args.cleanup != "none" else None,
                             _native_unused(args), memory_check=not args.no_memory_limit)
    if dry:
        print("DRY RUN: nothing was executed, created or changed.")
    for line in plan.describe():
        print(line)
    for line in memory_lines:
        print(line)
    for line in describe_detection(det, choice).splitlines():
        print(line)
    print("preflight (read-only, advisory):")
    for line in measure(installer, xplan.prefix).describe().splitlines():
        print(f"  {line}")
    print("Only unpack installers you are authorised to use.")
    # For a prefix still to be created, the space is measured on the nearest existing folder
    # above it, with room for the prefix itself, so nothing is created when it cannot fit.
    problem = (xplan.space_problem_before_prefix(PREFIX_ALLOWANCE) if create
               else None if missing else xplan.problem())
    mem_problem = None if args.no_memory_limit else memory_problem(xplan.memory)
    if dry:
        stops = []
        if missing and not args.create_prefix:
            stops.append(f"the prefix does not exist: {xplan.prefix} (add --create-prefix, or "
                         "create it first)")
        if create and wine is None:
            stops.append("wine not found on PATH; it is needed to create the prefix")
        if scope_problem:
            stops.append(f"{scope_problem} (for the prefix creation; add --no-memory-limit to "
                         "create it without the memory limit)")
        if problem:
            stops.append(problem)
        if mem_problem:
            stops.append(f"{mem_problem} (add --no-memory-limit to unpack anyway)")
        for item in stops:
            print(f"a real run would stop now: {item}")
        return 0
    if problem:
        print(f"error: {problem}. " + ("Nothing was run or created." if create else
                                       "Nothing was written."), file=sys.stderr)
        return 1
    if mem_problem:
        print(f"error: {mem_problem}. Nothing was run or written. To unpack without this "
              "check, add --no-memory-limit (the unpack can then use all free memory).",
              file=sys.stderr)
        return 2
    print("Ctrl-C stops the unpack; files already written are kept and nothing is deleted.")
    if not args.yes:
        refused = _confirm("Type 'yes' to unpack the files: ", "Nothing was run or written.")
        if refused is not None:
            return refused
    scan_summary = None
    if scan_plan is not None:
        from gitfurl.scan import decide, run_scan

        try:
            proceed, scan_summary = decide(run_scan(scan_plan), args.allow_scan_hit)
        except KeyboardInterrupt:
            print("interrupted during the ClamAV scan. Nothing was run or created.",
                  file=sys.stderr)
            return 130
        if not proceed:
            print(f"error: {scan_summary}", file=sys.stderr)
            return 1
        print(f"[note] {scan_summary}", flush=True)
    if create:
        from gitfurl.memlimit import MemoryLimitError as _MemErr

        try:
            status = native_prefix_create(plan, not args.no_memory_limit)
        except _MemErr as exc:
            print(f"error: {exc}. {MEMORY_OFF_HINT}", file=sys.stderr)
            return 2
        except FileExistsError as exc:
            print(f"error: {exc}. Nothing was unpacked.", file=sys.stderr)
            return 1
        except KeyboardInterrupt as exc:
            print(_prefix_interrupt_message(exc), file=sys.stderr)
            print("Nothing was unpacked.", file=sys.stderr)
            return 130
        except (RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"error: {exc}" if isinstance(exc, RuntimeError) else
                  "error: wine did not finish in time; the prefix directory may be incomplete.",
                  file=sys.stderr)
            print("Nothing was unpacked.", file=sys.stderr)
            return 1
        if status != 0:
            print(f"error: creating the prefix failed (wine exited {status}). Nothing was "
                  "unpacked.", file=sys.stderr)
            return 1
        problem = xplan.problem()
        if problem:
            print(f"error: {problem}. The prefix was created; nothing was written in it.",
                  file=sys.stderr)
            return 1
    try:
        res = run_native(
            plan, on_line=(lambda text: print(f"[progress] {text}", flush=True))
            if args.progress else None,
            detailed_log=args.detailed_log, progress_interval=args.progress_interval,
            scan_summary=scan_summary)
    except ExtractRefused as exc:
        print(f"error: {exc}. {exc.outcome()}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        if getattr(exc, "unpack_started", False):
            print(f"interrupted during the unpack; files already written in {xplan.dest} are "
                  "kept and nothing was deleted. The unpack's result was not recorded.",
                  file=sys.stderr)
        else:
            print("interrupted before any file was written; nothing was deleted.",
                  file=sys.stderr)
        return 130
    for line in res.lines:
        print(line)
    if args.cleanup != "none":
        if res.status == 0:
            deleted, problems = perform_cleanup(clean, 0)
            print(f"cleanup: deleted {deleted} file(s)")
            for item in problems:
                print(f"cleanup problem: {item}", file=sys.stderr)
        else:
            print("cleanup: skipped, the unpack did not complete")
    if res.events_path is not None:
        print(f"event log: {res.events_path}")
    print(f"local log: {res.log_path} (may contain your username and local paths)")
    sys.stdout.flush()
    return res.status
