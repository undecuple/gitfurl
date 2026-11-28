from __future__ import annotations

import argparse
import math
import os
import shutil
import subprocess
import sys
from pathlib import Path

from gitfurl import __version__
from gitfurl.activity import CliProgress, wine_progress_text
from gitfurl.cleanup import MODES, perform_cleanup, plan_cleanup
from gitfurl.detect import describe as describe_detection
from gitfurl.detect import detect
from gitfurl.doctor import format_report, run_doctor
from gitfurl.memlimit import MemoryLimitError
from gitfurl.namelinks import plan_staging, propose_for, run_staged, staged_plan
from gitfurl.prefixes import (
    MENU_LINE,
    create_prefix,
    list_prefixes,
    plan_create,
    prefixes_root,
    validate_prefix_name,
    wineboot_env,
    wineboot_line,
)
from gitfurl.preflight import measure
from gitfurl.route import ROUTE_CHOICES, ROUTE_NATIVE, choose_route, setup_unpacked
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
    runtime_env,
    validate_installer,
    validate_wine_debug,
)
from gitfurl.safety import ROOT_MESSAGE, running_as_root
from gitfurl.scan import DEFAULT_TIMEOUT_S as SCAN_TIMEOUT_S
from gitfurl.timeline import approval_entry

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
    inst.add_argument("--wine-addons", action="store_true",
                      help="when gitfurl creates the prefix, let Wine show its Mono and Gecko install "
                           "dialogs (answering Install makes Wine download them); default: off, "
                           "wineboot runs with WINEDLLOVERRIDES=mscoree,mshtml=")
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
    inst.add_argument("--link-names", action="store_true",
                      help="when the setup script expects archives that are missing and the "
                           "folder holds one matching numbered series of FreeArc archives under "
                           "another name (fg-01.bin expected, other-01.bin there), use that "
                           "mapping: Setup runs from a staging folder of symbolic links that "
                           "gitfurl makes and deletes afterwards (your files are not renamed), "
                           "and the route check counts the archives as named. Default: off")
    inst.add_argument("--apply-setup-entries", action="store_true",
                      help="native route only: after a complete unpack, also add the registry "
                           "values and menu entries the setup itself declares that gitfurl can "
                           "apply exactly (written without Wine; the plan lists each one and "
                           "each skipped entry)")
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
                      help="print what the run is doing every interval: for Setup, the payload "
                           "read, rate, estimate and the processes; for gitfurl's own unpack, "
                           "the data block, each decoder's counters, the file, the rates and an "
                           "estimate. Several lines on a terminal, one line otherwise")
    inst.add_argument("--stall-after", type=float, default=None, metavar="SECONDS",
                      help="print an advisory note each time nothing moves (no payload read, "
                           "no CPU use, no log growth) for this long; after the run moves again, "
                           "a later quiet period gets a note too. Off by default. It never "
                           "stops anything")
    inst.add_argument("--progress-interval", type=float, default=10.0, metavar="SECONDS",
                      help="seconds between two --progress outputs (default 10)")
    inst.add_argument("--timeline", action="store_true",
                      help="also print the run's timeline rows as they come (what each step "
                           "did and why); the timeline is saved next to the log either way")
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
        "extract", help="files only: extract FreeArc archives (storing, lzma, exe, x86, delta, "
                        "rep, 4x4, srep, cm and rolz) into an existing prefix without running "
                        "the installer. "
                        "'install' picks this route itself when it can")
    ext.add_argument("installer", help="the installer; FreeArc archives beside it are read")
    ext.add_argument("--prefix", required=True, help="name of an existing dedicated prefix; "
                     "files go to C:\\Games\\PREFIX in it (or the folder --dest names)")
    ext.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    ext.add_argument("--yes", action="store_true", help="approve without prompting")
    for command in (inst, ext):
        command.add_argument("--accept-bad-index-crc", action="store_true",
                             help="read FreeArc indexes with a bad index or footer CRC, with "
                                  "a warning; each file's CRC is still checked. Default: off")
        command.add_argument("--keep-bad-file-crc", action="store_true",
                             help="keep (and list) a file whose decoded CRC-32 does not match "
                                  "the archive instead of removing it; its size must still "
                                  "match, and nothing is overwritten. Only for an installer "
                                  "you trust. This run only. Default: off")
        command.add_argument("--dest", metavar="WINPATH", default=None,
                             help="native route only: unpack into this new folder on the "
                                  "prefix's drive C:, for example 'C:\\Games\\My Game' (or "
                                  "C:/Games/My Game). It must not exist yet; folders above it "
                                  "are created. Not C:\\ itself, nothing in C:\\windows, "
                                  "C:\\users or C:\\ProgramData, and in C:\\Program Files (or "
                                  "its x86 twin) only a folder that is new directly below it. "
                                  "On the Wine/Proton route Setup picks its folder. Default: "
                                  "C:\\Games\\PREFIX")
    ixt = sub.add_parser(
        "extract-inno", help="extract the files an Inno Setup installer holds for its program "
                             "folder into a prefix, without running the installer")
    ixt.add_argument("installer", help="the Inno Setup installer (setup.exe)")
    ixt.add_argument("--prefix", required=True, help="name of an existing dedicated prefix; "
                     "files go to C:\\Games\\PREFIX in it")
    ixt.add_argument("--dry-run", action="store_true", help="show the plan and change nothing")
    ixt.add_argument("--yes", action="store_true", help="approve without prompting")
    for command in (inst, ixt):
        _choice_options(command)
    pre = sub.add_parser("prefix", help="manage dedicated prefixes")
    pre_sub = pre.add_subparsers(dest="prefix_command", required=True)
    create = pre_sub.add_parser("create", help="create a dedicated prefix")
    create.add_argument("name")
    create.add_argument("--yes", action="store_true", help="approve without prompting")
    create.add_argument("--wine-addons", action="store_true",
                        help="when gitfurl creates the prefix, let Wine show its Mono and Gecko install "
                           "dialogs (answering Install makes Wine download them); default: off, "
                           "wineboot runs with WINEDLLOVERRIDES=mscoree,mshtml=")
    create.add_argument("--runtime", choices=RUNTIMES, default="wine",
                        help="wine (default) or proton, through umu-run; use the runtime the "
                             "installs into this prefix will use")
    create.add_argument("--proton-path", metavar="DIR",
                        default=os.environ.get("PROTONPATH"),
                        help="a Proton folder you already have (or set PROTONPATH); never "
                             "downloaded")
    pre_sub.add_parser("list", help="list dedicated prefixes")
    delete = pre_sub.add_parser(
        "delete", help="delete a prefix gitfurl made, with the menu entries gitfurl wrote "
                       "for it; shows the plan first")
    delete.add_argument("name")
    delete.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    delete.add_argument("--yes", action="store_true", help="approve without prompting")
    desk = sub.add_parser(
        "desktop-entry",
        help="add (or with --remove, remove) an application-menu entry for the window, in "
             "your own data folder; optional, shows the plan first")
    desk.add_argument("--remove", action="store_true",
                      help="remove the entry and icon gitfurl wrote (files it did not write "
                           "are left)")
    desk.add_argument("--dry-run", action="store_true", help="show the plan; change nothing")
    desk.add_argument("--yes", action="store_true", help="approve without prompting")
    tlog = sub.add_parser("log", help="print the timeline of a prefix's last run (read-only)")
    tlog.add_argument("prefix", nargs="?", help="the prefix name")
    tlog.add_argument("--all", action="store_true",
                      help="list the timelines kept (for this prefix, or for every prefix)")
    rep = sub.add_parser(
        "report", help="a support report of a run, scrubbed of the software's names, file "
                       "names, checksums, your user, host and paths; printed (or written with "
                       "-o), never sent")
    rep.add_argument("prefix", nargs="?", help="the prefix name (its latest run)")
    rep.add_argument("--run", metavar="STAMP",
                     help="the run with this stamp (YYYYMMDD-HHMMSS, as in the log's name)")
    rep.add_argument("--all-runs", action="store_true", help="every run of the prefix kept")
    rep.add_argument("--log", metavar="FILE",
                     help="a log or timeline file given directly (with the files of its run)")
    rep.add_argument("--exact-sizes", action="store_true",
                     help="keep exact sizes and byte counts (default: rounded to two "
                          "significant figures; exact sizes can identify the software)")
    rep.add_argument("-o", "--output", metavar="FILE",
                     help="write the report to a new file (mode 0600); an existing file or a "
                          "link there is refused")
    rep.add_argument("--show-removed", action="store_true",
                     help="also list what was removed, by category and count (never the "
                          "values)")
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
    if args.command == "desktop-entry":
        return _desktop_command(args)
    if args.command == "log":
        return _log_command(args)
    if args.command == "report":
        return _report_command(args)
    try:
        if args.dest is not None:
            from gitfurl.extract import parse_destination

            parse_destination(args.dest)  # refused whatever the route, before anything is read
        installer = validate_installer(args.installer)
        det = detect(installer)  # once; passed on
        links = propose_for(installer) if args.link_names else None
        choice = choose_route(installer, args.prefix or DEFAULT_PREFIX, det, args.route,
                              setup_options=_native_unused(args), links=links,
                              accept_bad_index_crc=args.accept_bad_index_crc,
                              keep_bad_file_crc=args.keep_bad_file_crc, dest=args.dest)
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    picked = _setup_choices(args, installer, _choices_mode(args, choice))
    if isinstance(picked, int):
        return picked
    if choice.route == ROUTE_NATIVE:
        return _native_install(args, installer, det, choice, links, picked)
    if args.keep_bad_file_crc:
        print("--keep-bad-file-crc does not apply to the Wine/Proton route: Setup runs and "
              "checks its own files")
    if args.dest is not None:
        print("--dest does not apply to the Wine/Proton route: Setup runs and picks its own "
              "folder")
    if args.apply_setup_entries:
        print("--apply-setup-entries does not apply to the Wine/Proton route: Setup runs and "
              "writes its own registry entries and shortcuts")
    if not args.dry_run:
        return _real_install(args, det, choice, links, picked)
    try:
        scan_plan = _scan_plan(args, installer)
        plan = build_plan(installer, args.prefix or DEFAULT_PREFIX, runtime=args.runtime,
                          proton_path=args.proton_path, portable=args.portable, scan=scan_plan,
                          memory_limit=not args.no_memory_limit,
                          unattended=_unattended_plan(args, args.prefix or DEFAULT_PREFIX,
                                                      det, picked),
                          wine_addons=args.wine_addons)
        portable_lines = _portable_lines(plan, installer, shutil.which("wine") or "wine",
                                         full=True) if args.portable else []
        staging = _staging(args, installer, links, args.prefix or DEFAULT_PREFIX)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if staging is not None:
        plan = staged_plan(plan, staging)
    print("DRY RUN: nothing was executed, created or changed.")
    print(f"command:     {plan.preview()}")
    print(f"prefix:      {plan.prefix}")
    boot = wineboot_line(plan.runtime, plan.wine_addons)
    if boot and not plan.prefix.is_dir():
        print(f"if created:  {boot}")
    print(f"working dir: {plan.cwd}")
    for k, v in plan.env_overrides.items():
        print(f"env:         {k}={v}")
    print(f"desktop:     {MENU_LINE} (wineboot and Setup)")
    print("needed for a real run:")
    for item in plan.prerequisites:
        print(f"  - {item}")
    for line in portable_lines:
        print(line)
    for line in _link_lines(links, staging):
        print(line)
    for line in _unattended_lines(plan) + (picked.lines() if picked else []):
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
    for line in measure(installer, plan.prefix,
                        unpacked=setup_unpacked(installer, det, links)).describe().splitlines():
        print(f"  {line}")
    print("Running an installer can expose files the current user can access.")
    return 0


def _staging(args, installer, links, prefix_name):
    """The staging folder for a Setup run with --link-names and a mapping, else None.
    Raises ValueError when it cannot be used."""
    if links is None or not links.ok:
        return None
    if args.portable:
        raise ValueError("--link-names cannot be combined with --portable: the sandbox does "
                         "not see the staging folder. Use one of them")
    return plan_staging(installer, links, prefix_name)


def _link_lines(links, staging) -> list[str]:
    if staging is not None:
        return staging.describe()
    if links is None:
        return []
    return [*links.lines(), "  Setup runs from the installer's own folder"]


def _unattended_plan(args, prefix_name, det, picked=None):
    """The unattended plan when --unattended is given, else None. Uses the detection of the
    installer's headers; `picked` (setup_choices.Picked) adds Inno's switches for the answers.
    Raises ValueError (UnattendedRefused) when it is not Inno Setup."""
    if not args.unattended:
        return None
    from dataclasses import replace

    from gitfurl.unattended import plan_unattended

    plan = plan_unattended(prefix_name, det)
    if picked is not None and picked.switches():
        plan = replace(plan, choice_args=tuple(picked.switches()), choices_line=picked.record())
    return plan


# ---- Setup's own questions (gitfurl.setup_choices) ------------------------------------------

def _choice_options(command) -> None:
    g = command.add_argument_group(
        "Setup's choices (Inno Setup)", "Answers to the questions Setup's wizard would ask. "
        "Without them a terminal is asked each question; --yes or no terminal takes the "
        "script's defaults. Names not given keep the default; !name deselects")
    g.add_argument("--language", metavar="NAME", help="the setup's language (its [Languages] "
                   "name)")
    g.add_argument("--type", dest="setup_type", metavar="NAME", help="the setup type")
    g.add_argument("--components", metavar="A,!B", help="components to select or deselect")
    g.add_argument("--tasks", metavar="X,!Y", help="tasks to check or uncheck")


def _choices_mode(args, choice) -> str:
    from gitfurl import setup_choices as sc

    if choice.route == ROUTE_NATIVE:
        return sc.MODE_ENTRIES if args.apply_setup_entries else sc.MODE_NATIVE
    return sc.MODE_SWITCHES if args.unattended else sc.MODE_WIZARD


def _setup_choices(args, installer, mode):
    """Setup's questions for this route: asked on the terminal, given as options, or the
    script's defaults; the dry run prints them. Returns a setup_choices.Picked, None (not an
    Inno Setup installer, or nothing to ask) or an exit status."""
    from gitfurl import setup_choices as sc

    if not args.dry_run and not args.prefix:
        return None  # the run stops for the missing --prefix before anything is asked
    try:
        picked = sc.pick(installer, mode, answers=sc.given(args.language, args.setup_type,
                                                           args.components, args.tasks),
                         yes=args.yes, tty=sys.stdin.isatty(), dry_run=args.dry_run)
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    except sc.ChoicesStopped as exc:
        print(f"error: {sc.HEADING}: {exc}. Nothing was run.", file=sys.stderr)
        return 2
    if picked is not None and (args.dry_run or picked.how != "asked"):
        mine = picked.how.startswith("given")
        print(f"{sc.HEADING}: the questions, with "
              + ("your answers:" if mine else "the script's defaults:"))
        for line in picked.choices.lines(picked.choice, "answer" if mine else "default"):
            print(line)
    return picked


def _desktop_command(args) -> int:
    from gitfurl.desktop import DesktopRefused, apply, check, plan_desktop

    if running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    try:
        plan = plan_desktop(remove=args.remove)
        lines = plan.describe()
        check(plan)
    except DesktopRefused as exc:
        print(f"error: {exc}. Nothing was changed.", file=sys.stderr)
        return 1
    print("Plan (dry run, nothing changed):" if args.dry_run else "Plan:")
    for line in lines:
        print(f"  {line}")
    if args.dry_run:
        return 0
    if not args.yes:
        refused = _confirm("Type 'yes' to " + ("remove them: " if args.remove
                                               else "write them: "), "Nothing was changed.")
        if refused is not None:
            return refused
    done: list[str] = []
    try:
        apply(plan, done)
    except (DesktopRefused, OSError) as exc:
        what = exc if isinstance(exc, DesktopRefused) else f"{exc.strerror or exc}: " \
            f"{getattr(exc, 'filename', '') or ''}"
        for line in done:
            print(f"already done: {line}")
        print(f"error: {what}" + (". 'gitfurl cli desktop-entry --remove' takes away what "
                                  "was written." if done and not args.remove else
                                  "" if done else ". Nothing was changed."), file=sys.stderr)
        return 1
    for line in done:
        print(line)
    return 0


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


def _prefix_delete_command(args) -> int:
    from gitfurl.prefix_delete import DeleteRefused, perform_delete_prefix, plan_delete_prefix

    if not args.dry_run and running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    try:
        plan = plan_delete_prefix(args.name)
    except DeleteRefused as exc:
        print(f"error: {exc}. Nothing was deleted.", file=sys.stderr)
        return 1
    print("Plan (dry run, nothing changed):" if args.dry_run else "Plan:")
    for line in plan.describe():
        print(f"  {line}")
    if args.dry_run:
        return 0
    if not args.yes:
        refused = _confirm(f"Type 'yes' to delete the prefix {args.name}: ",
                           "Nothing was deleted.")
        if refused is not None:
            return refused
    try:
        res = perform_delete_prefix(plan, how="yes" if args.yes else "typed")
    except DeleteRefused as exc:
        print(f"error: {exc}. Nothing was deleted.", file=sys.stderr)
        return 1
    for line in res.lines(plan):
        print(line)
    if res.log_path is not None:
        print(f"log: {res.log_path}")
    print(res.headline(plan))
    return 0 if res.folder_gone and not res.left else 1


def _prefix_command(args) -> int:
    if args.prefix_command == "delete":
        return _prefix_delete_command(args)
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
    if args.wine_addons and args.runtime != "wine":
        print("error: --wine-addons applies only to the wine runtime (Proton sets up its own "
              "prefix). Nothing was changed.", file=sys.stderr)
        return 1
    try:
        runtime_extra = runtime_env(args.runtime, args.proton_path)
    except ValueError as exc:
        print(f"error: {exc}. Nothing was changed.", file=sys.stderr)
        return 1
    tool = "umu-run" if args.runtime == "proton" else "wine"
    wine = shutil.which(tool)
    if wine is None:
        print(f"error: {tool} not found on PATH; run 'gitfurl doctor'. Nothing was changed.",
              file=sys.stderr)
        return 1
    extra = {**runtime_extra, **wineboot_env(args.runtime, args.wine_addons)}
    plan = plan_create(args.name, wine, extra_env=extra)
    print(f"This will create a new dedicated prefix at {plan['path']}")
    shown = " ".join(f"{k}={v}" for k, v in runtime_extra.items())
    print(f"command: {' '.join(plan['argv'])}  (with WINEPREFIX{' ' + shown if shown else ''} "
          "set for that command only)")
    line = wineboot_line(args.runtime, args.wine_addons)
    if line:
        print(line)
    print(f"{MENU_LINE}.")
    if args.runtime == "proton":
        print("umu-run downloads the Steam Linux Runtime on its first run unless it is already "
              "present (a network download that gitfurl does not make). Use this prefix for "
              "Proton installs (--runtime proton).")
    print("Your existing ~/.wine is not used or changed.")
    if not args.yes:
        if not sys.stdin.isatty():
            print("Not approved: no terminal to ask on. Re-run with --yes to approve.")
            return 2
        if input("Type 'yes' to create it: ").strip() != "yes":
            print("Not approved. Nothing was changed.")
            return 2
    try:
        status = create_prefix(args.name, wine, extra_env=extra)
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
    print(f"{tool} exited with status {status}")
    return 0 if status == 0 else 1


def _extract_command(args) -> int:
    """Native extraction: plan, approval, then gitfurl's own FreeArc reader writes the files."""
    from gitfurl.extract import ExtractRefused, extract, plan_extract

    if not args.dry_run and running_as_root():
        print(f"error: {ROOT_MESSAGE}", file=sys.stderr)
        return 1
    try:
        installer = validate_installer(args.installer)
        plan = plan_extract(installer, args.prefix,
                            accept_bad_index_crc=args.accept_bad_index_crc,
                            keep_bad_file_crc=args.keep_bad_file_crc, dest=args.dest)
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
        if plan.warnings or plan.keep_bad_file_crc:  # logged, with a timeline
            from gitfurl.route import NativeInstallPlan, RouteChoice, run_native
            from gitfurl.timeline import Timeline, approval_entry, archive_warning_entries

            tl = Timeline()
            archive_warning_entries(tl, plan)
            approval_entry(tl, "yes" if args.yes else "typed")
            choice = RouteChoice("native", "you", (), plan)
            native = NativeInstallPlan(installer, args.prefix, plan.prefix, choice, False)
            try:
                run = run_native(native, timeline=tl)
                kept = len(run.result.crc_kept) if run.result is not None else 0
                tl.end("Extraction ended",
                       f"complete; {kept} files kept with a CRC-32 mismatch" if kept and
                       run.status == 0 else "complete" if run.status == 0 else
                       "stopped" if run.status == 130 else "see the log for errors")
            finally:
                tl.close()
            print(f"log: {run.log_path}")
            for line in run.lines:
                print(line)
            return run.status
        else:
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
    from gitfurl.setup_choices import MODE_FILES

    try:
        installer = validate_installer(args.installer)
        picked = _setup_choices(args, installer, MODE_FILES)
        if isinstance(picked, int):
            return picked
        plan = plan_inno_extract(installer, args.prefix,
                                 choice=picked.entry_choice() if picked else None)
    except ValueError as exc:  # ExtractRefused is a ValueError
        print(f"error: {exc}. Nothing was written.", file=sys.stderr)
        return 1
    problem = plan.problem()
    if args.dry_run:
        print("DRY RUN: nothing was extracted, created or changed.")
    for line in plan.describe() + (picked.lines() if picked else []):
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


def _real_install(args, det, choice, links=None, picked=None) -> int:
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
                          unattended=_unattended_plan(args, args.prefix, det, picked),
                          wine_addons=args.wine_addons)
        if args.portable and args.runtime != "wine":
            raise ValueError("portable install is only supported with the wine runtime")
        staging = _staging(args, installer, links, args.prefix)
        if staging is not None:
            plan = staged_plan(plan, staging)
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
    memory_lines, limit_problem = _memory_lines(plan)
    if limit_problem:
        for line in memory_lines:
            print(line)
        print(f"error: {limit_problem}. {MEMORY_OFF_HINT}", file=sys.stderr)
        return 2
    print(f"About to run: {plan.preview()}")
    print(f"prefix: {plan.prefix}" + (" (will be created first)" if missing else ""))
    boot = wineboot_line(plan.runtime, plan.wine_addons) if missing else None
    if boot:
        print(boot)
    print(f"{MENU_LINE}.")
    if args.portable:
        for line in portable_lines:
            print(line)
    else:
        print("Wine prefixes are not sandboxes: the installer can reach files you can reach.")
    for line in _link_lines(links, staging):
        print(line)
    for line in _unattended_lines(plan) + (picked.lines() if picked else []):
        print(line)
    for line in memory_lines:
        print(line)
    print("Only run installers you are authorised to use and have chosen to trust.")
    for line in describe_detection(det, choice).splitlines():
        print(line)
    print("preflight (read-only, advisory):")
    for line in measure(installer, plan.prefix,
                        unpacked=setup_unpacked(installer, det, links)).describe().splitlines():
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
    tl = _new_timeline(args, det, choice, plan.prefix, not missing, args.create_prefix,
                       picked=picked)
    if not args.yes:
        refused = _confirm("Type 'yes' to run the installer: ", "Nothing was run.")
        if refused is not None:
            return refused
    approval_entry(tl, "yes" if args.yes else "typed")
    control = RunControl()
    try:
        options = RunOptions(timeline=tl,
            on_progress=(lambda smp: print(wine_progress_text(smp, sys.stdout.isatty()),
                                           flush=True)) if args.progress else None,
            progress_interval=args.progress_interval,
            detailed_log=args.detailed_log, wine_debug=args.wine_debug, control=control,
            stall_after_s=args.stall_after,
            on_note=(lambda note: print(f"[note] {note}", flush=True)),
            allow_scan_hit=args.allow_scan_hit,
            choices_line=picked.record() if picked is not None and picked.applied else None)
        result = run_staged(staging, lambda: run_tracked(
            plan, wine, args.create_prefix, args.show_changes or args.find_exe, options=options),
            on_line=lambda line: print(line, flush=True))
        status, log_path, changes = result
    except MemoryLimitError as exc:
        _end_timeline(tl, args.prefix, "Not run: the memory limit could not be used", str(exc))
        print(f"error: {exc}. {MEMORY_OFF_HINT}", file=sys.stderr)
        return 2
    except (RuntimeError, ValueError) as exc:
        _end_timeline(tl, args.prefix, "Failed", str(exc))
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt as exc:
        _end_timeline(tl, args.prefix, "Interrupted (Ctrl-C)",
                      "the run was not completed; see the log")
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
            tl.add("cleanup", "Cleanup skipped", "the run was cancelled")
        _end_timeline(tl, args.prefix, "Cancelled.", "the run was stopped, not completed")
        if args.detailed_log:
            print(f"event log: {Path(log_path).with_suffix('.events.jsonl')}")
        print(f"local log: {log_path} (may contain your username and local paths)")
        if tl.path is not None:
            print(f"timeline: {tl.path}")
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
            _cleanup_entry(tl, deleted, problems)
        else:
            print("cleanup: skipped, the installer did not exit with status 0")
            tl.add("cleanup", "Cleanup skipped", "the installer did not exit with status 0")
    _end_timeline(tl, args.prefix, f"Installer exited with status {status}",
                  "0 usually means it finished" if status == 0 else
                  "a non-zero status does not say why; read the log")
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
    if tl.path is not None:
        print(f"timeline: {tl.path}")
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
                     ("--runtime proton", args.runtime == "proton")):
        if on:
            out.append(flag)
    return out


def _native_install(args, installer, det, choice, links=None, picked=None) -> int:
    """The native route: Setup is not run; gitfurl unpacks the archives itself after one
    approval of the whole plan (scan, prefix creation, unpack, listings, cleanup)."""
    from gitfurl.route import NativeInstallPlan, native_stops

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
        clean = plan_cleanup(installer, args.cleanup,
                             [xplan.prefix, xplan.dest, prefixes_root(), logs_root()])
    except ValueError as exc:
        print(f"error: {exc}. Nothing was run.", file=sys.stderr)
        return 1
    missing = not xplan.prefix.is_dir()
    create = missing and args.create_prefix
    wine = shutil.which("wine") if create else None
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
    stops = native_stops(xplan, missing=missing, create_ok=args.create_prefix, wine=wine,
                         scope_problem=scope_problem, memory_check=not args.no_memory_limit)
    stop = {s.kind: s.detail for s in stops}
    if not dry and "no_prefix" in stop:
        print(f"error: prefix does not exist: {xplan.prefix}. Create it first with "
              f"'gitfurl prefix create {prefix_name}', or add --create-prefix. Nothing was run.",
              file=sys.stderr)
        return 1
    if not dry and "no_wine" in stop:
        print("error: wine not found on PATH; it is needed to create the prefix. Run 'gitfurl "
              "cli doctor'. Nothing was run.", file=sys.stderr)
        return 1
    if not dry and "scope" in stop:
        for line in memory_lines:
            print(line)
        print(f"error: {scope_problem}. {MEMORY_OFF_HINT}", file=sys.stderr)
        return 2
    scan_lines: list[str] = []
    if scan_plan is not None:
        from gitfurl.scan import database_info, describe_scan_plan

        scan_lines = describe_scan_plan(scan_plan) if dry else \
            describe_scan_plan(scan_plan, database_info(scan_plan.clamscan))
    entries = None
    if args.apply_setup_entries:
        from gitfurl.setup_entries import plan_setup_entries

        entries = plan_setup_entries(installer, xplan,
                                     choice=picked.entry_choice() if picked else None)
    plan = NativeInstallPlan(installer, prefix_name, xplan.prefix, choice, create, wine,
                             scan_lines,
                             clean.describe() if args.cleanup != "none" else None,
                             _native_unused(args), memory_check=not args.no_memory_limit,
                             setup_entries=entries, choices=picked)
    if dry:
        print("DRY RUN: nothing was executed, created or changed.")
    for line in plan.describe():
        print(line)
    if args.wine_addons:
        print("--wine-addons does not apply to the native route: its wineboot always runs "
              "without Wine's Mono and Gecko dialogs (the unpack needs neither)")
    if args.stall_after is not None:
        print("--stall-after does not apply to the native route: Setup is not run, and the "
              "unpack prints its own progress (Ctrl-C stops it)")
    if links is not None and not links.ok:
        for line in links.lines():
            print(line)
    elif links is not None:
        print("--link-names: no staging folder is needed on the native route; gitfurl reads "
              "the archives under their own names")
    for line in memory_lines:
        print(line)
    for line in describe_detection(det, choice).splitlines():
        print(line)
    print("preflight (read-only, advisory):")
    for line in measure(installer, xplan.prefix).describe().splitlines():
        print(f"  {line}")
    print("Only unpack installers you are authorised to use.")
    problem, mem_problem = stop.get("space"), stop.get("memory")
    if dry:
        dry_text = {
            "no_prefix": lambda d: (f"the prefix does not exist: {d} (add --create-prefix, or "
                                    "create it first)"),
            "no_wine": lambda d: "wine not found on PATH; it is needed to create the prefix",
            "scope": lambda d: (f"{d} (for the prefix creation; add --no-memory-limit to "
                                "create it without the memory limit)"),
            "space": lambda d: d,
            "memory": lambda d: f"{d} (add --no-memory-limit to unpack anyway)",
        }
        for s in stops:
            print(f"a real run would stop now: {dry_text[s.kind](s.detail)}")
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
    tl = _new_timeline(args, det, choice, xplan.prefix, not missing, args.create_prefix,
                       xplan.target, picked)
    if not args.yes:
        refused = _confirm("Type 'yes' to unpack the files" + (
            " and add the setup entries listed above" if entries is not None else "") + ": ",
            "Nothing was run or written.")
        if refused is not None:
            return refused
    approval_entry(tl, "yes" if args.yes else "typed")
    status = _native_steps(args, plan, xplan, scan_plan, clean, create, tl)
    _end_timeline(tl, prefix_name, NATIVE_HEADLINES.get(status, "Not unpacked."))
    return status


def _native_steps(args, plan, xplan, scan_plan, clean, create, tl) -> int:
    """The approved native run: scan, prefix creation, unpack, cleanup."""
    from gitfurl.extract import ExtractRefused
    from gitfurl.route import native_prefix_create, run_native

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
            tl.add("note", "The ClamAV scan stopped the run", scan_summary)
            print(f"error: {scan_summary}", file=sys.stderr)
            return 1
        tl.add("note", "ClamAV scan passed", scan_summary)
        print(f"[note] {scan_summary}", flush=True)
    if create:
        tl.add("prefix", "Creating the prefix with wineboot",
               "it does not exist yet; Wine's Mono and Gecko dialogs are off for this command")
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
            tl.add("prefix", f"Creating the prefix failed (wine exited {status})",
                   "so nothing was unpacked")
            print(f"error: creating the prefix failed (wine exited {status}). Nothing was "
                  "unpacked.", file=sys.stderr)
            return 1
        tl.add("prefix", "Prefix created", "wineboot exited with status 0")
        problem = xplan.problem()
        if problem:
            print(f"error: {problem}. The prefix was created; nothing was written in it.",
                  file=sys.stderr)
            return 1
    try:
        with CliProgress(args.progress, args.progress_interval) as live:
            res = run_native(plan, detailed_log=args.detailed_log,
                             progress_interval=args.progress_interval,
                             scan_summary=scan_summary, timeline=tl, **live.kwargs())
    except ExtractRefused as exc:
        tl.add("note", "The unpack did not start", f"{exc}. {exc.outcome()}")
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
        if res.status == 0 and res.result.crc_kept:
            print("cleanup: skipped, files whose CRC-32 does not match were kept")
            tl.add("cleanup", "Cleanup skipped", "files whose CRC-32 does not match were kept "
                   "(--keep-bad-file-crc), so the installer stays")
        elif res.status == 0:
            deleted, problems = perform_cleanup(clean, 0)
            print(f"cleanup: deleted {deleted} file(s)")
            for item in problems:
                print(f"cleanup problem: {item}", file=sys.stderr)
            _cleanup_entry(tl, deleted, problems)
        else:
            print("cleanup: skipped, the unpack did not complete")
            tl.add("cleanup", "Cleanup skipped", "the unpack did not complete")
    if res.events_path is not None:
        print(f"event log: {res.events_path}")
    print(f"local log: {res.log_path} (may contain your username and local paths)")
    if tl.path is not None:
        print(f"timeline: {tl.path}")
    sys.stdout.flush()
    return res.status


# ---- the timeline (gitfurl.timeline) --------------------------------------------------------

NATIVE_HEADLINES = {0: "Unpacked (Setup was not run).",
                    130: "Stopped. Files already written are kept; nothing was deleted.",
                    1: "The unpack ended with an error, or did not start."}


def _new_timeline(args, det, choice, prefix: Path, exists: bool, create: bool, target=None,
                  picked=None):
    from gitfurl.timeline import Timeline, choices_entry, plan_entries

    tl = Timeline(on_line=(lambda line: print(f"[timeline] {line}", flush=True))
                  if getattr(args, "timeline", False) else None)
    plan_entries(tl, det, choice, prefix, exists, create, target,
                 bool(getattr(args, "apply_setup_entries", False)))
    choices_entry(tl, picked)
    return tl


def _end_timeline(tl, prefix_name: str, what: str, why: str = "") -> None:
    """The outcome row; the file is made now when the run made no log of its own."""
    tl.save(logs_root(), prefix_name)
    tl.end(what, why)


def _cleanup_entry(tl, deleted: int, problems) -> None:
    tl.add("cleanup", f"Cleanup: deleted {deleted} file(s)"
           + (f", {len(problems)} problem(s)" if problems else ""),
           "the run completed and you chose to delete the installer afterwards")


def _log_command(args) -> int:
    """Print a prefix's latest timeline, or list the timelines kept. Reads files only."""
    from gitfurl.timeline import kept, latest, read_text

    if args.prefix is not None:
        try:
            validate_prefix_name(args.prefix)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
    root = logs_root()
    if args.all:
        found = kept(root, args.prefix)
        if not found:
            print(f"no timelines kept in {root}" + (f" for {args.prefix}" if args.prefix else ""))
            return 0
        for path in found:
            try:
                size = path.stat().st_size
            except OSError:
                continue
            print(f"{path}  ({size} bytes)")
        return 0
    if args.prefix is None:
        print("error: name a prefix (or use --all to list every timeline kept)", file=sys.stderr)
        return 1
    path = latest(root, args.prefix)
    if path is None:
        print(f"no timeline for {args.prefix} in {root} (a run writes one next to its log)")
        return 1
    try:
        text = read_text(path)
    except OSError as exc:
        print(f"error: {path} could not be read ({exc.strerror or exc})", file=sys.stderr)
        return 1
    print(f"timeline: {path}")
    print(text)
    return 0


def _report_command(args) -> int:
    """A scrubbed support report, printed or written to a new file. Sends nothing."""
    from gitfurl import report

    if (args.prefix is None) == (args.log is None):
        print("error: name a prefix, or give a file with --log (not both)", file=sys.stderr)
        return 1
    try:
        wine, umu = report.probe_wine()
        rep = report.build_report(args.log if args.log else args.prefix,
                                  run=args.run, all_runs=args.all_runs,
                                  exact_sizes=args.exact_sizes, wine_version=wine, umu=umu)
    except (report.ReportRefused, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.output:
        try:
            report.write_report(Path(args.output), rep.text)
        except FileExistsError:
            print(f"error: {args.output} exists (or is a link); nothing was written. Name a "
                  "new file", file=sys.stderr)
            return 1
        except OSError as exc:
            print(f"error: {args.output} could not be written ({exc.strerror or exc})",
                  file=sys.stderr)
            return 1
        print(f"support report written to {args.output} (mode 0600). Check it before you "
              "share it; nothing was sent anywhere.")
        if args.show_removed:
            print("\n".join(rep.removed_lines()))
        return 0
    sys.stdout.write(rep.text)
    if args.show_removed:
        print("\n".join(rep.removed_lines()), file=sys.stderr)
    return 0
