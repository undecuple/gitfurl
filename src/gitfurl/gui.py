"""Graphical interface (tkinter). A thin layer over the shared core.

The window never runs anything by itself: creating a prefix or running an installer
each needs a confirmation dialog that shows what will happen.
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
from dataclasses import replace
from pathlib import Path

from gitfurl import __version__
from gitfurl import scan as scanmod
from gitfurl.cleanup import MODES, perform_cleanup, plan_cleanup
from gitfurl.detect import describe as describe_detection
from gitfurl.detect import detect
from gitfurl.doctor import run_doctor
from gitfurl.memlimit import availability as memory_availability
from gitfurl.memlimit import describe as describe_memory
from gitfurl.prefixes import (
    PrefixStopped,
    create_prefix,
    incomplete_marker,
    list_prefixes,
    plan_create,
    prefixes_root,
    validate_prefix_name,
    wineboot_env,
    wineboot_line,
)
from gitfurl.preflight import measure
from gitfurl.safety import ROOT_MESSAGE, running_as_root
from gitfurl.runner import (
    CANCEL_REMAINS,
    RUNTIMES,
    RunControl,
    RunOptions,
    build_plan,
    describe_result,
    logs_root,
    run_tracked,
    runtime_env,
    validate_installer,
)
from gitfurl.theme import FG, MUTED, RADIUS, SIDE, SPACE, TYPE  # noqa: F401

WARNING = (
    "A Wine prefix is not a sandbox: the installer can reach files your user can reach.\n"
    "Only run installers you are authorised to use and have chosen to trust."
)


PORTABLE_NOTE = (
    "Runs the installer in bwrap with the network off, your environment cleared and Wine's "
    "menu entries disabled. Reduces changes outside the prefix; not a guarantee.")


def short_path(path: Path) -> str:
    """Show a path with ~ for the home folder."""
    home = str(Path.home())
    text = str(path)
    return "~" + text[len(home):] if text.startswith(home + "/") else text


def describe_environment(report: dict) -> str:
    lines = [f"platform: {report['platform']}"]
    for key in ("wine", "bwrap"):
        t = report[key]
        lines.append(f"{t.name}: " + (f"unavailable ({t.problem})" if t.problem else t.version))
    return "\n".join(lines)


def portable_option_state(runtime: str, bwrap_path: str | None) -> tuple[bool, str]:
    """(available, reason) for the portable toggle. The reason is shown next to it."""
    if runtime != "wine":
        return False, "Not available with the proton runtime (untested under bwrap)."
    if not bwrap_path:
        return False, "Not available: bwrap (bubblewrap) was not found on PATH."
    return True, PORTABLE_NOTE


SCAN_NOTE = ("Runs the clamscan on your PATH on the installer and the files beside it before "
             "anything else; gitfurl passes no option that changes files. A match or a scan "
             "error stops the run (a scan error can not be overridden). A clean result only "
             "means no known signature matched. gitfurl never updates the database.")


def scan_option_state(clamscan_path: str | None) -> tuple[bool, str]:
    """(available, reason) for the ClamAV scan toggle. The reason is shown next to it."""
    if not clamscan_path:
        return False, ("Not available: clamscan was not found on PATH. gitfurl does not "
                       "install ClamAV.")
    return True, SCAN_NOTE


MEMORY_TITLE = "Limit memory (reduces the chance of running out)"
MEMORY_NOTE = ("On by default. Runs the installer in a systemd user scope with a soft and a hard "
               "memory limit. This reduces the chance that the system runs out of memory; it does "
               "not guarantee that the installer succeeds. Above the soft limit the installer is "
               "slowed down; at the hard limit a process of this run is stopped and systemd then "
               "ends the whole run. Swap is not limited, and programs already running outside the "
               "scope are not limited.")


UNATTENDED_TITLE = "Install without the installer's windows (Inno Setup only)"
UNATTENDED_NOTE = ("Off by default. For Inno Setup installers, gitfurl starts Setup with its own "
                   "silent switches: no wizard, no installer windows, every choice left at the "
                   "script's default, and the files go to C:\\Games\\<prefix> inside the prefix. "
                   "The installer still unpacks its own files. Afterwards gitfurl reads the log "
                   "Setup wrote. A setup script can still show its own windows or ignore the "
                   "switches. Refused for installers that are not Inno Setup.")


def unattended_for(installer, prefix: str, enabled: bool, now=None):
    """The UnattendedPlan for the window, or None when off. Reads the installer's headers
    only. Raises ValueError when the installer is not Inno Setup."""
    if not enabled:
        return None
    from gitfurl.unattended import plan_unattended

    return plan_unattended(prefix, detect(installer), now)


def memory_limit_problem(enabled: bool, avail=None, meminfo: str = "/proc/meminfo") -> str | None:
    """Why Run must stay off for the memory limit, or None. Reads files only."""
    if not enabled:
        return None
    _lines, problem = describe_memory(True, avail if avail is not None else memory_availability(),
                                      meminfo)
    if problem is None:
        return None
    return f"{problem}. Turn off '{MEMORY_TITLE}' to run without it."


def describe_plan(installer: str, prefix: str, runtime: str = "wine",
                  proton_path: str | None = None, portable: bool = False,
                  scan: bool = False, memory_limit: bool = False, unattended: bool = False,
                  now=None, wine_addons: bool = False) -> tuple[str, str | None]:
    """Return (preview text, error). No side effects: the scan is described, never run; the
    memory limit lines read files only."""
    if not installer.strip():
        return "", "Choose an installer file."
    if not prefix.strip():
        return "", "Choose or create a prefix."
    try:
        scan_plan = scanmod.build_scan_plan(validate_installer(installer.strip()),
                                            scanmod.find_clamscan()) if scan else None
        plan = build_plan(validate_installer(installer.strip()), prefix.strip(),
                          runtime=runtime, proton_path=proton_path, portable=portable,
                          scan=scan_plan, memory_limit=memory_limit,
                          unattended=unattended_for(validate_installer(installer.strip()),
                                                    prefix.strip(), unattended, now),
                          wine_addons=wine_addons)
        portable_lines = []
        if portable:
            from gitfurl.portable import check_portable_supported, describe_portable, find_bwrap

            bwrap, penv = find_bwrap(), dict(os.environ)
            installer_path = validate_installer(installer.strip())
            wine = shutil.which("wine") or "wine"
            wrapped = replace(plan, argv=[wine, *plan.argv[1:]])
            check_portable_supported(wrapped, bwrap, penv, installer_path)
            portable_lines = describe_portable(wrapped, bwrap, installer_path, penv,
                                               full_argv=True)
    except ValueError as exc:
        return "", str(exc)
    text = (
        f"command:     {plan.preview()}\n"
        f"prefix:      {plan.prefix}\n"
        f"working dir: {plan.cwd}\n"
        + "".join(f"env:         {k}={v}\n" for k, v in plan.env_overrides.items())
    )
    boot = boot_line(plan.runtime, wine_addons) if not plan.prefix.is_dir() else None
    if boot:
        text += f"if created:  {boot}\n"
    if portable_lines:
        text += "\n".join(portable_lines) + "\n"
    if plan.unattended is not None:
        text += "\n".join(plan.unattended.describe(plan.prefix)) + "\n"
    if scan_plan is not None:
        text += "\n".join(scanmod.describe_scan_plan(scan_plan)) + "\n"
    text += "\n".join(describe_memory(memory_limit, memory_availability() if memory_limit
                                      else None)[0])
    return text.rstrip(), None


WINE_ADDONS_TITLE = "Let Wine offer Mono and Gecko when it creates a prefix"
WINE_ADDONS_NOTE = ("Off by default: a prefix gitfurl creates with Wine gets no Mono (.NET) or "
                    "Gecko (Internet Explorer parts), so creating it needs no answer and Wine "
                    "downloads nothing. Most installers need neither. Turn this on to have Wine "
                    "show its install dialogs (answering Install downloads them). Not used for "
                    "Proton or the native route.")


def boot_line(runtime: str, addons: bool) -> str | None:
    """The window's line on how a new prefix is booted (wineboot_line in its words)."""
    return wineboot_line(runtime, addons, option=f"'{WINE_ADDONS_TITLE}'")


SETUP_INSTEAD_TITLE = "Run Setup instead (Wine/Proton route)"
SETUP_INSTEAD_NOTE = ("Off by default: when the installer's payload is FreeArc archives gitfurl "
                      "can read, gitfurl unpacks them itself and Setup is not run (the plan says "
                      "what is skipped). Turn this on to have Setup run and do all of its own "
                      "steps.")

# The native plan's text names command-line options; the window names its own switches.
WINDOW_WORDS = (
    ("run it with: gitfurl cli install <file> --prefix ",
     "choose that file as the installer here, with the prefix "),
    ("to unpack them anyway, use --route native",
     "to unpack them anyway, run 'gitfurl cli install --route native' (the window does not "
     "offer this)"),
    ("--route wine", f"the '{SETUP_INSTEAD_TITLE}' switch"),
    ("--no-memory-limit", f"turning off '{MEMORY_TITLE}'"),
    ("Create it first with 'gitfurl cli prefix create ", "Create it first (Create now) for '"),
    ("to have gitfurl unpack the archives itself instead, use --route native (these options "
     "then do not apply)", "to have gitfurl unpack the archives itself instead, turn those "
     "options off"),
)


def window_words(text: str) -> str:
    for cli_text, window_text in WINDOW_WORDS:
        text = text.replace(cli_text, window_text)
    return text


def window_setup_options(portable: bool, unattended: bool, runtime: str,
                         show_changes: bool) -> list[str]:
    """The window's options that only a Setup run uses (as the command line's --portable,
    --unattended, --runtime proton and --show-changes): any of them makes the route Wine."""
    return [label for on, label in (
        (portable, "portable install"), (unattended, UNATTENDED_TITLE.lower()),
        (runtime == "proton", "the Proton runtime"),
        (show_changes, "showing what the installer changed")) if on]


def window_route(installer, prefix: str, *, setup_instead: bool = False,
                 setup_options=(), env=None):
    """(detection, RouteChoice or None, error text or None). Reads headers and archive
    indexes only. The route is chosen as the command line's automatic choice; the 'Run Setup
    instead' switch asks for the Wine/Proton route."""
    from gitfurl.route import choose_route

    det = detect(installer)
    try:
        choice = choose_route(installer, prefix, det, "wine" if setup_instead else "auto", env,
                              setup_options=list(setup_options))
    except ValueError as exc:
        return det, None, str(exc)
    return det, choice, None


def window_route_text(installer, prefix: str, env=None, *, setup_instead: bool = False,
                      setup_options=()) -> str:
    """The detection text with the route line the window will take."""
    det, choice, error = window_route(installer, prefix, setup_instead=setup_instead,
                                      setup_options=setup_options, env=env)
    if choice is None:
        return describe_detection(det) + f"\nroute: not chosen ({error})."
    return window_words(describe_detection(det, choice))


PREFIX_STOP_TEXT = ("Stop ends the prefix creation: its own process group (a polite stop, then a "
                    "forced one after 5 seconds), then gitfurl asks this prefix's own Wine "
                    "server to exit. The new "
                    "folder is kept and marked incomplete, so gitfurl will not use it; delete "
                    "it yourself before using that name again. Nothing else is deleted.")

PREFIX_STOP_TEXT_PROTON = ("Stop ends the prefix creation: its own process group (a polite stop, "
                           "then a forced one after 5 seconds). With Proton, Proton's own Wine "
                           "server and the runtime's container may keep running; this is not "
                           "guaranteed to end everything. The new folder is kept and marked "
                           "incomplete, so gitfurl will not use it; delete it yourself before "
                           "using that name again. Nothing else is deleted.")

NATIVE_STOP_LINE = ("Stop ends the ClamAV scan, the prefix creation (its own process group, then "
                    "this prefix's wineserver -k) or the unpack between files or 1 MiB chunks. "
                    "Files already written are kept and nothing is deleted.")


def native_stop_text(stop) -> str:
    """One route.NativeStop in the window's words."""
    return {
        "no_prefix": lambda d: (f"The prefix does not exist: {d}. Create it first, or turn on "
                                "'Create the prefix if it does not exist'."),
        "no_wine": lambda d: "Wine is not on PATH; it is needed to create the prefix.",
        "scope": lambda d: (f"The memory limit cannot be used to create the prefix: {d}. Turn "
                            f"off '{MEMORY_TITLE}' to create it without the limit."),
        "space": lambda d: window_words(d) + ".",
        "memory": lambda d: (f"{d}. Turn off '{MEMORY_TITLE}' to unpack without this check "
                             "(the unpack can then use all free memory)."),
    }[stop.kind](stop.detail)


NATIVE_ADDONS_LINE = ("The Mono and Gecko dialogs switch is not used here: the native route's "
                      "wineboot always runs without those dialogs (the unpack needs neither).")


def native_plan_text(plan, det, *, memory_lines=(), preflight: str = "",
                     scan_db_lines=(), wine_addons: bool = False) -> str:
    """The approval text for a native run: every step, what is skipped, then the checks.
    wine_addons: the window's Mono and Gecko switch is on; the plan says it is not used."""
    lines = [window_words(ln) for ln in plan.describe()]
    if wine_addons:
        lines.append(NATIVE_ADDONS_LINE)
    lines += list(memory_lines)
    lines += ["", window_words(describe_detection(det, plan.choice))]
    if preflight:
        lines += ["", "Checks before the run (read-only, advisory):", preflight]
    if scan_db_lines:
        lines += ["", *scan_db_lines]
    lines += ["", NATIVE_STOP_LINE, "", "Only unpack installers you are authorised to use."]
    return "\n".join(lines)


def native_plan_for(installer, choice, *, create_ok: bool, memory_on: bool, wine=None,
                    scan_plan=None, db_info=None, cleanup_text=None, avail=None,
                    meminfo: str = "/proc/meminfo"):
    """(route.NativeInstallPlan, its stops, memory lines) for the window, built the way the
    command line builds them. Reads files and free space only; starts nothing."""
    from gitfurl.route import NativeInstallPlan, native_stops

    xplan = choice.extract
    missing = not xplan.prefix.is_dir()
    create = missing and create_ok
    memory_lines, scope_problem = [], None
    if create:
        memory_lines, scope_problem = describe_memory(
            memory_on, (avail if avail is not None else memory_availability()) if memory_on
            else None, meminfo)
        memory_lines = [f"for the prefix creation: {ln}" if i == 0 else ln
                        for i, ln in enumerate(memory_lines)]
    stops = native_stops(xplan, missing=missing, create_ok=create_ok,
                         wine=wine if create else None, scope_problem=scope_problem,
                         memory_check=memory_on, meminfo=meminfo)
    scan_lines = scanmod.describe_scan_plan(scan_plan, db_info) if scan_plan is not None else []
    plan = NativeInstallPlan(installer, xplan.prefix_name, xplan.prefix, choice, create,
                             wine if create else None, scan_lines, cleanup_text, [],
                             memory_check=memory_on)
    return plan, stops, memory_lines


class NativeSample:
    """A native unpack's progress for the window's bar and line (route.run_native numbers)."""

    def __init__(self, done: int, total: int, elapsed: float):
        self.done, self.total, self.elapsed = done, total, elapsed

    @property
    def percent(self) -> float:
        return 100.0 * self.done / self.total if self.total else 100.0

    def line(self) -> str:
        from gitfurl.route import _rate_text

        return _rate_text(self.done, self.total, self.elapsed)


def native_cleanup_allowed(outcome, stopped: bool) -> bool:
    """Cleanup only after a complete unpack (status 0) that nobody asked to stop. A Stop that
    lands after the unpack has returned (during the listings) still skips the cleanup, since
    the Stop dialog says nothing is deleted."""
    return not stopped and outcome.result is not None and outcome.result.status == 0


def native_end_text(outcome, cleanup_line: str | None, notes) -> tuple[str, str]:
    """(end text, colour: "ok", "warn" or "error") for a route.NativeOutcome."""
    notes = list(notes)
    if len(notes) > 5:  # the first (the scan result, if any) and the latest four
        notes = [notes[0], "...", *notes[-4:]]
    tail = "".join(f"\n{n}" for n in notes)
    res = outcome.result
    if res is None:
        head = "Stopped. " if outcome.status == 130 else "Not unpacked. "
        return (head + window_words(outcome.message or "") + tail,
                "warn" if outcome.status == 130 else "error")
    if res.status == 0:
        head, colour = "Unpacked (Setup was not run).", "ok"
        clean = f"\n{cleanup_line}" if cleanup_line and cleanup_line != "skipped" else ""
    elif res.status == 130:
        head, colour = "Stopped. Files already written are kept; nothing was deleted.", "warn"
        clean = "\nCleanup skipped." if cleanup_line else ""
    else:
        head, colour = "The unpack ended with an error.", "warn"
        clean = "\nCleanup skipped: the unpack did not complete." if cleanup_line else ""
    body = "\n".join(window_words(ln) for ln in res.lines)
    logs = (f"\nEvent log: {res.events_path}" if res.events_path else "") + \
        f"\nLocal log: {res.log_path} (may contain your user name and paths)"
    return f"{head}\n{body}{clean}{tail}{logs}", colour


def installer_problem(installer: str) -> str | None:
    """Why this installer text cannot be used, or None. Reads nothing but file metadata."""
    if not installer.strip():
        return "Choose an installer file."
    try:
        validate_installer(installer.strip())
    except ValueError as exc:
        return str(exc)
    return None


def plan_checklist(installer_error: str | None, prefix_error: str | None, prefix_exists: bool,
                   will_create: bool, runtime: str, runtime_found: bool,
                   plan_error: str | None = None, memory_error: str | None = None,
                   runtime_needed: bool = True, as_root: bool = False
                   ) -> list[tuple[str, bool, str]]:
    """What Run needs, as (key, ok, text) rows in screen order. No side effects.

    Run is allowed only when every row is ok. A row that is not ok says what is missing in
    words, so the state never depends on colour alone. `plan_error` is a problem the plan
    itself reports after the other rows pass (for example a missing Proton folder).
    `memory_error` is why the memory limit cannot be used (see memory_limit_problem); it is always
    shown, since only the user can decide to run without the limit.
    """
    tool = "umu-run" if runtime == "proton" else "Wine"
    rows = [("root", False, ROOT_MESSAGE)] if as_root else []
    rows += [
        ("installer", installer_error is None,
         "Installer file chosen" if installer_error is None else installer_error),
        ("runtime", runtime_found or not runtime_needed,
         f"{tool} not needed: gitfurl unpacks the archives itself" if not runtime_needed else
         f"{tool} found" if runtime_found else f"{tool} not found on PATH"),
    ]
    if prefix_error is not None:
        rows.append(("prefix", False, prefix_error))
    elif prefix_exists:
        rows.append(("prefix", True, "Prefix exists"))
    elif will_create:
        rows.append(("prefix", True, "Prefix does not exist yet and will be created first"))
    else:
        rows.append(("prefix", False, ("Prefix does not exist: press Create now, or turn on "
                                       "'Create the prefix if it does not exist'")))
    if plan_error is not None and all(ok for _k, ok, _t in rows):
        rows.append(("plan", False, plan_error))
    if memory_error is not None:
        rows.append(("memory", False, memory_error))
    return rows


def run_reason(rows: list[tuple[str, bool, str]], busy: bool) -> str:
    """Text for a disabled Run button, or "" when Run is allowed."""
    if busy:
        return "Run is off: something is already running."
    missing = [text for _k, ok, text in rows if not ok]
    if not missing:
        return ""
    more = f" (and {len(missing) - 1} more, see the checklist)" if len(missing) > 1 else ""
    return f"Run is off: {missing[0]}{more}"


def create_reason(prefix_error: str | None, prefix_exists: bool, busy: bool,
                  as_root: bool = False) -> str:
    """Text for a disabled Create now button, or "" when it is allowed."""
    if busy:
        return "Create is off: something is already running."
    if as_root:
        return f"Create is off: {ROOT_MESSAGE}"
    if prefix_error is not None:
        return f"Create is off: {prefix_error}"
    if prefix_exists:
        return "Create is off: the prefix already exists."
    return ""


def env_summary(wine_ok: bool, umu_ok: bool) -> str:
    return f"Wine: {'ok' if wine_ok else 'missing'}    umu-run: {'ok' if umu_ok else 'missing'}"


def key_hints(page: str) -> str:
    if page == "library":
        return "Tab: move    Up/Down: choose a prefix    Enter/Space: open"
    if page == "details":
        return "Tab: move    Enter/Space: activate    Esc: back to Library"
    return "Tab: move    Enter/Space: activate"


def run_state_text(kind: str | None, stopping: bool) -> str:
    """The footer's run state. kind is None, "prefix", "install" or "native"."""
    if kind is None:
        return "Idle"
    if stopping:
        return "Stopping..."
    return {"prefix": "Creating prefix", "native": "Unpacking"}.get(kind, "Installer running")


def run_in_thread(fn, results: queue.Queue) -> None:
    def work():
        try:
            results.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - shown to the user, not swallowed
            results.put(("error", exc))

    threading.Thread(target=work, daemon=True).start()


WINE_MISSING = ("Wine was not found. Install it with your package manager (for example the "
                "'wine' package), then press Check again.")

RUNTIME_DOWNLOAD_NOTE = ("\n\nProton runs through umu-run. If its runtime is not already on this "
                         "machine, umu-run downloads it on first use (a network download).")


# Setup runs in the window get stall advice: a note when nothing has moved for this long.
WINDOW_STALL_S = 120.0
STALL_PLAN_LINE = ("If nothing moves for 2 minutes (no installer data read, almost no CPU use, "
                   "no log growth), a note says so and, when the run's windows can be read "
                   "(not in a portable run), lists them and names the one that opened most "
                   "recently; it is advice only, nothing is stopped or answered.")


def keeps_note(note: str) -> bool:
    """Notes that stay on screen until the run ends (the progress line is overwritten every
    few seconds): a crash or a stall, which may need the user to look at a window."""
    return note.startswith(("crash:", "possibly stalled:"))


def stop_applies(pending: dict, control) -> bool:
    """True while `control` belongs to the run in progress and has not been stopped yet.

    The Stop dialog is modal and the run can end while it is open, so this is checked again
    after the answer, before anything on screen is changed.
    """
    return control is not None and pending.get("control") is control and not control.cancelled


def main(start: str = "library", prefix: str | None = None, driver=None) -> int:
    """Open the window. `driver` is for tests: called once as driver(root, ui) when the window
    is up (ui is a dict of the window's parts) and expected to end by calling root.destroy()."""
    try:
        import tkinter as tk
        from tkinter import filedialog, messagebox, simpledialog
    except ImportError as exc:
        print(
            f"The graphical interface could not load tkinter ({exc}).\n"
            "Install your distribution's tkinter package (python3-tk), or use the "
            "command line: gitfurl cli --help"
        )
        return 1
    from gitfurl import widgets as w

    try:
        root = tk.Tk()
        w.pick_font(root)  # also fails early when no usable font is installed
    except tk.TclError as exc:
        print(
            f"The graphical interface could not open a window ({exc}).\n"
            "It needs a running graphical session (a DISPLAY) and at least one installed font. "
            "You can still use the command line: gitfurl cli --help"
        )
        return 1
    root.title("gitfurl")
    root.configure(bg=w.BG)
    root.minsize(900, 620)
    root.geometry("1080x760")
    try:
        icon = Path(__file__).resolve().parent / "icon.png"
        if icon.is_file():
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
    except tk.TclError:
        pass
    def F(size=10, bold=False):
        return w.pick_font(root, size, bold)

    SP, T = SPACE, TYPE

    results: queue.Queue = queue.Queue()
    ui_updates: queue.Queue = queue.Queue()
    installer_var, status_var = tk.StringVar(), tk.StringVar()
    prefix_var = tk.StringVar(value=prefix or "default")
    create_var, changes_var, detail_var = (tk.BooleanVar(value=True), tk.BooleanVar(value=False),
                                           tk.BooleanVar(value=False))
    cleanup_var = tk.StringVar(value="none")
    runtime_var = tk.StringVar(value="wine")
    find_var = tk.BooleanVar(value=False)
    portable_var = tk.BooleanVar(value=False)
    scan_var = tk.BooleanVar(value=False)
    memory_var = tk.BooleanVar(value=True)  # the memory limit: on by default
    unattended_var = tk.BooleanVar(value=False)
    addons_var = tk.BooleanVar(value=False)  # Wine's Mono and Gecko dialogs: off
    setup_var = tk.BooleanVar(value=False)  # "Run Setup instead": off, so the route is automatic
    route_cache: dict = {}
    progress_var = tk.StringVar(value="Shown while an installer runs: payload read, rate, rough ETA.")
    pending = {"cleanup": None, "control": None, "proton": False}
    proton_var = tk.StringVar(value=os.environ.get("PROTONPATH", ""))
    busy = {"on": False}
    run = {"kind": None, "stopping": False}  # kind: None, "prefix" or "install"
    banners: list = []
    nav_state = {"page": start}
    wine_path = {"value": None, "umu": None}
    extra_names: list[str] = ["default"]
    report_cache = {"text": ""}
    pages: dict[str, tk.Frame] = {}
    nav_items: dict[str, tk.Canvas] = {}

    def label(parent, text="", size=10, bold=False, fg=FG, bg=w.BG, **kw):
        return tk.Label(parent, text=text, bg=bg, fg=fg, font=F(size, bold), **kw)

    def text_box(parent, height):
        return tk.Text(parent, height=height, bg=w.BG, fg=FG, relief="sunken", bd=2, wrap="word",
                       highlightthickness=1, highlightbackground=w.LINE, state="disabled",
                       font=("monospace", T["body"]), padx=10, pady=SP["sm"])

    def set_text(box, value):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", value)
        box.configure(state="disabled")

    def entry(parent, var):
        return tk.Entry(parent, textvariable=var, bg=w.BG, fg=FG, insertbackground=FG, relief="sunken", bd=2,
                        highlightthickness=1, highlightbackground=w.LINE, highlightcolor=w.ORANGE,
                        font=F(T["body"]))

    def heading(parent, text):
        label(parent, text.upper(), T["caption"], True, w.MUTED
              ).pack(anchor="w", pady=(SP["lg"], SP["sm"] - 2), padx=SP["xs"])

    def row(card, title, subtitle=None, widget=None, first=False):
        """One row of a boxed list: title and subtitle left, a control right."""
        if not first:
            tk.Frame(card.body, bg=w.LINE, height=1).pack(fill="x", pady=SP["sm"] - 2)
        r = tk.Frame(card.body, bg=w.PANEL)
        r.pack(fill="x")
        if widget:  # packed first so a narrow window clips the text, never the control
            widget(r).pack(side="right", padx=(SP["md"], 0))
        left = tk.Frame(r, bg=w.PANEL)
        left.pack(side="left", fill="x", expand=True)
        label(left, title, T["body"], False, FG, w.PANEL).pack(anchor="w")
        if subtitle:
            sub = label(left, subtitle, T["caption"], False, w.MUTED, w.PANEL, justify="left")
            sub.pack(anchor="w")
            autowrap(sub, left, minimum=160)
        return r

    # ---- footer: environment, key hints and run state, like the launcher's bottom bar ----
    footer = tk.Frame(root, bg=SIDE)
    footer.pack(side="bottom", fill="x")  # packed first so it always keeps its row
    tk.Frame(footer, bg=w.LINE, height=1).pack(fill="x")
    foot = tk.Frame(footer, bg=SIDE)
    foot.pack(fill="x", padx=SP["lg"], pady=SP["sm"])
    foot_env = label(foot, "", T["caption"], True, w.MUTED, SIDE)
    foot_env.pack(side="left")
    foot_state = label(foot, "", T["caption"], True, w.MUTED, SIDE)
    foot_state.pack(side="right")
    foot_hints = label(foot, "", T["caption"], False, w.MUTED, SIDE)
    foot_hints.pack(side="left", expand=True)

    def refresh_footer():
        found = bool(wine_path["value"])
        foot_env.configure(text=env_summary(found, bool(wine_path["umu"])),
                           fg=w.CYAN if found else w.RED)
        foot_hints.configure(text=key_hints(nav_state["page"]))
        colour = w.AMBER if run["stopping"] else (w.CYAN if run["kind"] else w.MUTED)
        foot_state.configure(text=run_state_text(run["kind"], run["stopping"]), fg=colour)

    def autowrap(lbl, parent, margin=0, minimum=200):
        """Re-wrap a label to the width of `parent`, so text follows the window size."""
        def fit(_e=None):
            width = parent.winfo_width() - margin
            if width > 1:
                lbl.configure(wraplength=max(minimum, width))
        parent.bind("<Configure>", fit, add="+")

    # ---- sidebar: pages, like rc's navigation split view ----
    side = tk.Frame(root, bg=SIDE, width=230)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    w.Wordmark(side, cell=5, outer=SIDE).pack(anchor="w", padx=18, pady=(20, 2))
    label(side, f"v{__version__}", T["caption"], False, w.MUTED, SIDE
          ).pack(anchor="w", padx=20)
    nav = tk.Frame(side, bg=SIDE)
    nav.pack(fill="x", pady=(SP["xl"], 0), padx=10)

    def nav_item(key, text):
        top = w.STUD_H  # room for the selected item's studs
        c = tk.Canvas(nav, height=38 + top, bg=SIDE, highlightthickness=0, bd=0, cursor="hand2")
        c.pack(fill="x", pady=2)

        def go():
            if not busy["on"]:
                show(key)

        def draw(_e=None):
            c.delete("all")
            width = c.winfo_width()
            on = nav_state["page"] == key or (key == "library" and nav_state["page"] == "details")
            if on:
                w.brick(c, 0, top, width, 38 + top, w.LINE)
            w.top_brick(c, 12, top + 12, 14, w.ORANGE if on else w.LINE)
            c.create_text(36, top + 18, text=text, anchor="w", fill=FG if on else w.MUTED,
                          font=F(T["body"], on))
            w.focus_ring(c, width, 38 + top, RADIUS, top=top)

        c.bind("<Configure>", draw)
        c.bind("<Button-1>", lambda _e: go())
        w.make_focusable(c, go, draw)
        c.redraw = draw
        nav_items[key] = c

    nav_item("library", "Library")
    nav_item("settings", "Settings")

    content = tk.Frame(root, bg=w.BG)
    content.pack(side="left", fill="both", expand=True)

    canvases: dict[str, tk.Canvas] = {}
    inners: dict[str, tk.Frame] = {}

    def wheel(direction):
        def handler(e):
            if not isinstance(e.widget, tk.Misc) or e.widget.winfo_toplevel() is not root:
                return
            if isinstance(e.widget, tk.Text) and e.widget.yview() != (0.0, 1.0):
                return  # a text box with its own scroll keeps the wheel
            cv = canvases.get(nav_state["page"])
            if cv is not None:
                cv.yview_scroll(direction * 3, "units")
        return handler

    root.bind_all("<Button-4>", wheel(-1))  # X11 wheel up
    root.bind_all("<Button-5>", wheel(1))
    root.bind_all("<MouseWheel>", lambda e: wheel(-1 if e.delta > 0 else 1)(e))

    def reveal(e):
        """Keyboard focus moved: scroll the page so the focused widget is on screen."""
        page = nav_state["page"]
        cv, inner = canvases.get(page), inners.get(page)
        wd = e.widget
        if cv is None or not isinstance(wd, tk.Misc) or not str(wd).startswith(str(cv) + "."):
            return
        margin = SP["lg"]
        top = wd.winfo_rooty() - inner.winfo_rooty()
        bottom = top + wd.winfo_height()
        view_top, view_h = cv.canvasy(0), cv.winfo_height()
        total = max(1, inner.winfo_height())
        if top < view_top + margin:
            cv.yview_moveto(max(0, top - margin) / total)
        elif bottom > view_top + view_h - margin:
            cv.yview_moveto((bottom + margin - view_h) / total)

    root.bind_all("<FocusIn>", reveal, add="+")

    def scroll_page(name):
        """A scrolling page. Row 1 of the page's frame is free for a fixed bar."""
        outer = tk.Frame(content, bg=w.BG)
        outer.grid_rowconfigure(0, weight=1)
        outer.grid_columnconfigure(0, weight=1)
        cv = tk.Canvas(outer, bg=w.BG, highlightthickness=0, bd=0)
        cv.grid(row=0, column=0, sticky="nsew")
        inner = tk.Frame(cv, bg=w.BG)
        win = cv.create_window(0, 0, window=inner, anchor="nw")
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        inner.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        canvases[name], inners[name] = cv, inner
        pad = tk.Frame(inner, bg=w.BG)
        pad.pack(fill="both", expand=True, padx=SP["xl"], pady=SP["xl"])
        pages[name] = outer
        return pad

    def show(name):
        nav_state["page"] = name
        for page in pages.values():
            page.pack_forget()
        pages[name].pack(fill="both", expand=True)
        for item in nav_items.values():
            item.redraw()
        if name == "library":
            refresh_list()
        elif name == "details":
            refresh_plan()
        refresh_footer()

    def go_back(_e=None):
        if busy["on"]:  # during a run the page with the Stop button stays put, like the sidebar
            return
        if nav_state["page"] == "details":
            show("library")

    root.bind("<Escape>", go_back)

    # ---- a banner for a missing tool: amber card, message, Check again ----
    def make_banner(parent, pady):
        slot = tk.Frame(parent, bg=w.BG, height=1)
        slot.pack(fill="x")
        card = w.RoundCard(slot, outline=w.AMBER, pad=SP["md"])
        card.pady = pady
        msg = tk.Label(card.body, text=WINE_MISSING, bg=w.PANEL, fg=w.AMBER, font=F(T["body"]),
                       justify="left", anchor="w")
        btn = w.PillButton(card.body, "Check again", lambda: refresh_env(), "plain",
                           F(T["caption"]), outer=w.PANEL, pad=(SP["md"], 6))
        btn.pack(side="right", padx=(SP["md"], 0))
        msg.pack(side="left", fill="x", expand=True)
        autowrap(msg, card.body, margin=130)
        banners.append((slot, card))

    # ---- Library page ----
    lib = scroll_page("library")
    top = tk.Frame(lib, bg=w.BG)
    top.pack(fill="x")
    label(top, "Library", T["title"], True).pack(side="left")
    new_btn = w.PillButton(top, "+  New prefix", lambda: new_prefix(), "accent", F(T["body"], True))
    new_btn.pack(side="right")
    lib_note = label(lib, "Each prefix is a separate, dedicated Wine environment. Your own ~/.wine "
                     "is never used.", T["body"], False, w.MUTED, justify="left")
    lib_note.pack(anchor="w", pady=(2, SP["sm"]))
    autowrap(lib_note, lib)
    make_banner(lib, (0, SP["sm"]))
    list_area = tk.Frame(lib, bg=w.BG)
    list_area.pack(fill="x")
    names: list[str] = []
    cards: list = []

    def open_prefix(name):
        if busy["on"]:
            return
        prefix_var.set(name)
        show("details")

    def move_card(delta):
        if nav_state["page"] != "library" or not cards:
            return
        cur = root.focus_get()
        idx = cards.index(cur) + delta if cur in cards else 0
        cards[max(0, min(len(cards) - 1, idx))].focus_set()

    root.bind("<Down>", lambda _e: move_card(1))
    root.bind("<Up>", lambda _e: move_card(-1))

    def refresh_list(_select=None):
        for child in list_area.winfo_children():
            child.destroy()
        cards.clear()
        existing = list_prefixes()
        names[:] = sorted(set(existing) | set(extra_names))
        for i, n in enumerate(names):
            c = w.RoundCard(list_area, pad=SP["md"], activate=lambda n=n: open_prefix(n))
            c.pack(fill="x", pady=SP["xs"] + 1)
            cards.append(c)
            r = tk.Frame(c.body, bg=w.PANEL)
            r.pack(fill="x")
            w.Tile(r, n[:1] or "g", w.ACCENTS[i % 4]).pack(side="left")
            ready = n in existing
            broken = ready and incomplete_marker(prefixes_root() / n) is not None
            label(r, "incomplete" if broken else "ready" if ready else "not created yet",
                  T["caption"], True, w.CYAN if ready and not broken else w.AMBER,
                  w.PANEL).pack(side="right", padx=6)
            mid = tk.Frame(r, bg=w.PANEL)
            mid.pack(side="left", padx=SP["md"], fill="x", expand=True)
            label(mid, n, T["lead"], True, FG, w.PANEL).pack(anchor="w")
            label(mid, short_path(prefixes_root() / n), T["caption"], False, w.MUTED, w.PANEL,
                  wraplength=400, justify="left").pack(anchor="w")
            for widget in [c, c.body, r, mid, *mid.winfo_children(), *r.winfo_children()]:
                widget.bind("<Button-1>", lambda _e, n=n: open_prefix(n), add="+")
                widget.configure(cursor="hand2")
        refresh_plan()

    # ---- Details page ----
    det = scroll_page("details")
    dtop = tk.Frame(det, bg=w.BG)
    dtop.pack(fill="x")
    w.PillButton(dtop, "<  Library", lambda: show("library"), "ghost", F(T["body"])).pack(anchor="w")
    dhead = tk.Frame(det, bg=w.BG)
    dhead.pack(fill="x", pady=(SP["sm"], 0))
    title_var, sub_var = tk.StringVar(), tk.StringVar()
    tk.Label(dhead, textvariable=title_var, bg=w.BG, fg=FG, font=F(T["title"], True)
             ).pack(side="left", anchor="n")
    sub_label = tk.Label(det, textvariable=sub_var, bg=w.BG, fg=w.MUTED, font=F(T["caption"]),
                         justify="left")
    sub_label.pack(anchor="w")
    autowrap(sub_label, det)
    make_banner(det, (SP["md"], 0))

    heading(det, "Checklist")
    c = w.RoundCard(det)
    c.pack(fill="x")
    check_rows: dict[str, tuple[tk.Frame, tk.Label, tk.Label]] = {}
    for key in ("root", "installer", "runtime", "prefix", "plan", "memory"):
        rr = tk.Frame(c.body, bg=w.PANEL)
        mark = tk.Label(rr, text="", bg=w.PANEL, fg=FG, font=F(T["body"], True), width=2)
        mark.pack(side="left", anchor="n")
        msg = tk.Label(rr, text="", bg=w.PANEL, fg=FG, font=F(T["body"]), justify="left",
                       anchor="w")
        msg.pack(side="left", fill="x", expand=True)
        autowrap(msg, rr, margin=30)
        check_rows[key] = (rr, mark, msg)
    status_slot = tk.Frame(det, bg=w.BG)
    status_slot.pack(fill="x")
    status_label = tk.Label(status_slot, textvariable=status_var, bg=w.BG, fg=w.CYAN,
                            font=F(T["body"]), justify="left")
    autowrap(status_label, status_slot)

    def show_status(*_):  # no empty gap while there is nothing to say
        if status_var.get():
            status_label.pack(anchor="w", pady=(SP["sm"], 0))
        else:
            status_label.pack_forget()

    status_var.trace_add("write", show_status)

    heading(det, "Installer")
    c = w.RoundCard(det)
    c.pack(fill="x")
    ir = tk.Frame(c.body, bg=w.PANEL)
    ir.pack(fill="x")

    def browse():
        path = filedialog.askopenfilename(title="Choose an installer",
                                          filetypes=[("Installers", "*.exe *.msi"), ("All", "*")])
        if path:
            installer_var.set(path)

    entry(ir, installer_var).pack(side="left", fill="x", expand=True, ipady=6)
    w.PillButton(ir, "Browse...", browse, "plain", F(T["body"]), outer=w.PANEL
                 ).pack(side="left", padx=(10, 0))

    def segmented_for(var, choices, after=None):
        def build(parent):
            f = tk.Frame(parent, bg=w.PANEL)
            buttons = {}

            def paint():
                for key, b in buttons.items():
                    b.style = "accent" if var.get() == key else "plain"
                    b._draw()

            var.trace_add("write", lambda *_: paint() if f.winfo_exists() else None)

            def pick(k):
                var.set(k)
                if after:
                    after()

            for key in choices:
                b = w.PillButton(f, key, lambda k=key: pick(k), "plain", F(T["caption"]),
                                 outer=w.PANEL, pad=(10, 5))
                b.pack(side="left", padx=2)
                buttons[key] = b
            paint()
            return f

        return build

    heading(det, "Options  (all off except creating the prefix and the memory limit)")
    c = w.RoundCard(det)
    c.pack(fill="x")
    row(c, "Create the prefix if it does not exist", "On by default. The prefix is created first, "
        "then the installer runs.", lambda p: w.Switch(p, create_var), first=True)
    memory_switch = {}

    def memory_widget(p):
        memory_switch["w"] = w.Switch(p, memory_var, command=lambda: refresh_plan())
        return memory_switch["w"]

    row(c, MEMORY_TITLE, MEMORY_NOTE, memory_widget)
    row(c, WINE_ADDONS_TITLE, WINE_ADDONS_NOTE,
        lambda p: w.Switch(p, addons_var, command=lambda: refresh_plan()))
    row(c, SETUP_INSTEAD_TITLE, SETUP_INSTEAD_NOTE,
        lambda p: w.Switch(p, setup_var, command=lambda: refresh_plan()))
    row(c, UNATTENDED_TITLE, UNATTENDED_NOTE,
        lambda p: w.Switch(p, unattended_var, command=lambda: refresh_plan()))
    row(c, "Show what the installer changed on my system",
        "Lists new files and registry keys in the prefix and in a few places outside it.",
        lambda p: w.Switch(p, changes_var))
    row(c, "Detailed log", "Also writes a JSON event log with progress samples next to the log.",
        lambda p: w.Switch(p, detail_var))
    row(c, "Find the installed program",
        "Guesses which new program the installer added, from names and sizes. Local only; "
        "nothing is run or added to any launcher.", lambda p: w.Switch(p, find_var))
    row(c, "Runtime", "wine (default), or proton through umu-run. gitfurl itself downloads nothing, "
        "but umu-run may download its runtime on first use.",
        segmented_for(runtime_var, RUNTIMES, lambda: refresh_plan()))

    def proton_row(p):
        f = tk.Frame(p, bg=w.PANEL)
        entry(f, proton_var).pack(side="left", ipady=4)
        w.PillButton(f, "Folder...", lambda: proton_var.set(
            filedialog.askdirectory(title="Choose the Proton folder") or proton_var.get()),
            "plain", F(T["caption"]), outer=w.PANEL, pad=(10, 5)).pack(side="left", padx=(8, 0))
        return f

    row(c, "Proton folder", "Only used with the proton runtime. Defaults to PROTONPATH.", proton_row)

    portable_switch = {}

    def portable_widget(p):
        portable_switch["w"] = w.Switch(p, portable_var, command=lambda: refresh_plan())
        return portable_switch["w"]

    portable_row = row(c, "Portable install (reduces changes outside this prefix)", PORTABLE_NOTE,
                       portable_widget)

    def refresh_portable():
        ok, reason = portable_option_state(runtime_var.get(), shutil.which("bwrap"))
        if not ok:
            portable_var.set(False)
        portable_switch["w"].set_enabled(ok)
        for child in portable_row.winfo_children()[-1:]:  # the title and note frame
            for lab in child.winfo_children()[-1:]:
                lab.configure(text=reason)

    runtime_var.trace_add("write", lambda *_: refresh_portable())
    refresh_portable()

    scan_switch = {}

    def scan_widget(p):
        scan_switch["w"] = w.Switch(p, scan_var, command=lambda: refresh_plan())
        return scan_switch["w"]

    scan_row = row(c, "Scan the installer with ClamAV first", SCAN_NOTE, scan_widget)
    scan_reason = {"text": SCAN_NOTE}

    def refresh_scan():
        ok, reason = scan_option_state(scanmod.find_clamscan())
        if not ok:
            scan_var.set(False)
        scan_switch["w"].set_enabled(ok)
        scan_reason["text"] = reason
        for child in scan_row.winfo_children()[-1:]:  # the title and note frame
            for lab in child.winfo_children()[-1:]:
                lab.configure(text=reason)

    refresh_scan()

    row(c, "Delete the original installer after a good run",
        "installer = the file, folder = everything beside it. Only when it exits with status 0.",
        segmented_for(cleanup_var, MODES))

    heading(det, "Plan  (nothing runs until you approve)")
    c = w.RoundCard(det)
    c.pack(fill="x")
    plan_box = text_box(c.body, 5)
    plan_box.pack(fill="x")

    heading(det, "Progress")
    c = w.RoundCard(det)
    c.pack(fill="x")
    bar = w.Bar(c.body)
    bar.pack(fill="x")
    progress_label = tk.Label(c.body, textvariable=progress_var, bg=w.PANEL, fg=w.MUTED,
                              font=F(T["caption"]), justify="left", anchor="w")
    progress_label.pack(fill="x", pady=(SP["sm"], 0))
    autowrap(progress_label, c.body)
    warn_label = tk.Label(det, text=WARNING, bg=w.BG, fg=w.YELLOW, font=F(T["caption"]),
                          justify="left")
    warn_label.pack(anchor="w", pady=(SP["lg"], 0))
    autowrap(warn_label, det)

    # The action bar stays on screen whatever the scroll position. It is built last so that
    # Tab reaches it after the options, which is also where it is on screen.
    action = tk.Frame(pages["details"], bg=SIDE)
    action.grid(row=1, column=0, sticky="ew")
    tk.Frame(action, bg=w.LINE, height=1).pack(fill="x")
    action_in = tk.Frame(action, bg=SIDE)
    action_in.pack(fill="x", padx=SP["xl"], pady=SP["md"])
    buttons_box = tk.Frame(action_in, bg=SIDE)
    buttons_box.pack(side="right")
    reason_var = tk.StringVar()
    reason_label = tk.Label(action_in, textvariable=reason_var, bg=SIDE, fg=w.AMBER,
                            font=F(T["body"]), justify="left", anchor="w")
    reason_label.pack(side="left", fill="x", expand=True, padx=(0, SP["md"]))
    autowrap(reason_label, action_in, margin=330)
    stop_btn = w.PillButton(buttons_box, "Stop", lambda: stop_clicked(), "plain", F(T["body"]),
                            outer=SIDE)  # packed only during a run
    create_btn = w.PillButton(buttons_box, "Create now", lambda: create_clicked(), "plain",
                              F(T["body"]), outer=SIDE)
    create_btn.pack(side="left", padx=(0, SP["md"]))
    run_btn = w.PillButton(buttons_box, "Run installer", lambda: run_clicked(), "accent",
                           F(T["lead"], True), outer=SIDE, pad=(22, 10))
    run_btn.pack(side="left")

    # ---- Settings page ----
    st = scroll_page("settings")
    label(st, "Settings", T["title"], True).pack(anchor="w")
    heading(st, "Environment")
    env_card = w.RoundCard(st)
    env_card.pack(fill="x")
    env_rows = tk.Frame(env_card.body, bg=w.PANEL)
    env_rows.pack(fill="x")
    w.PillButton(env_card.body, "Check again", lambda: refresh_env(), "plain", F(T["caption"]),
                 outer=w.PANEL, pad=(SP["md"], 6)).pack(anchor="w", pady=(SP["sm"], 0))
    heading(st, "About")
    c = w.RoundCard(st)
    c.pack(fill="x")
    row(c, "gitfurl", f"Version {__version__}. Licence GPL-3.0-or-later. Not affiliated with Wine "
        "or any publisher.", first=True)
    row(c, "Safety", WARNING.replace("\n", " "))

    def selected_prefix() -> str:
        return prefix_var.get().strip()

    def selected_prefix_path() -> Path:
        return prefixes_root() / selected_prefix()

    def proton_dir():
        return proton_var.get().strip() or None

    def current_route():
        """(detection, RouteChoice or None, error) for the chosen installer and switches.
        Reads headers and archive indexes; cached until the file or a switch changes."""
        text = installer_var.get().strip()
        if installer_problem(text) is not None:
            return None, None, None
        installer = validate_installer(text)
        try:
            st = installer.stat()
        except OSError:
            return None, None, None
        options = tuple(window_setup_options(portable_var.get(), unattended_var.get(),
                                             runtime_var.get(), changes_var.get()))
        key = (str(installer), st.st_mtime_ns, st.st_size, selected_prefix(), setup_var.get(),
               options)
        if route_cache.get("key") != key:
            route_cache.clear()  # a failed choice must not leave the old value under a new key
            value = window_route(installer, selected_prefix(), setup_instead=setup_var.get(),
                                 setup_options=options)
            route_cache["key"], route_cache["value"] = key, value
        return route_cache["value"]

    def route_text_now():
        """The route line for the approval: the route Run takes, from current_route()."""
        det, choice, error = current_route()
        if det is None:
            return ""
        if choice is None:
            return describe_detection(det) + f"\nroute: not chosen ({error})."
        return window_words(describe_detection(det, choice))

    def native_choice():
        _det, choice, _err = current_route()
        return choice if choice is not None and choice.route == "native" else None

    def refresh_plan(*_):
        name = selected_prefix()
        title_var.set(name or "No prefix")
        try:
            path = prefixes_root() / validate_prefix_name(name)
            exists, prefix_error = path.is_dir(), None
            if exists and incomplete_marker(path) is not None:
                prefix_error = ("Prefix was not completely created (its creation stopped, "
                                "timed out or failed): use a new name, or delete the folder")
            sub_var.set(f"{short_path(path)}   "
                        f"{'incomplete' if prefix_error else 'ready' if exists else 'not created yet'}")
        except ValueError as exc:
            exists, prefix_error = False, ("Choose or create a prefix." if not name.strip()
                                           else str(exc))
            sub_var.set("invalid name")
        runtime = runtime_var.get()
        tool = wine_path["umu"] if runtime == "proton" else wine_path["value"]
        native, preview_error = None, None
        try:
            native = native_choice() if prefix_error is None else None
            if native is not None:
                plan, stops, mem_lines = native_plan_for(
                    validate_installer(installer_var.get().strip()), native,
                    create_ok=create_var.get(), memory_on=memory_var.get(),
                    wine=wine_path["value"])
        except Exception as exc:  # noqa: BLE001 - shown in the checklist; nothing runs
            native, preview_error = None, f"The plan could not be made: {exc}"
        if preview_error is not None:
            set_text(plan_box, preview_error)
            rows = plan_checklist(installer_problem(installer_var.get()), prefix_error, exists,
                                  create_var.get(), runtime, bool(tool), preview_error,
                                  as_root=running_as_root())
        elif native is not None:
            set_text(plan_box, "\n".join([window_words(ln) for ln in plan.describe()]
                                         + mem_lines))
            plan_stops = [s for s in stops if s.kind not in ("no_prefix", "no_wine")]
            needs_wine = not exists and create_var.get()
            rows = plan_checklist(installer_problem(installer_var.get()), prefix_error, exists,
                                  create_var.get(), "wine", bool(wine_path["value"]),
                                  native_stop_text(plan_stops[0]) if plan_stops else None,
                                  None, runtime_needed=needs_wine, as_root=running_as_root())
        else:
            text, err = describe_plan(installer_var.get(), name, runtime, proton_dir(),
                                      portable_var.get(), scan_var.get(), memory_var.get(),
                                      unattended_var.get(), wine_addons=addons_var.get())
            set_text(plan_box, text or (err or ""))
            rows = plan_checklist(installer_problem(installer_var.get()), prefix_error, exists,
                                  create_var.get(), runtime, bool(tool), err,
                                  memory_limit_problem(memory_var.get()), as_root=running_as_root())
        for key, (frame, mark, msg) in check_rows.items():
            hit = next((r for r in rows if r[0] == key), None)
            if hit is None:
                frame.pack_forget()
                continue
            _k, ok, line = hit
            mark.configure(text="\u2713" if ok else "\u2717", fg=w.CYAN if ok else w.RED)
            msg.configure(text=line, fg=FG if ok else w.RED)
            if not frame.winfo_manager():
                frame.pack(fill="x", pady=2)
        reason = run_reason(rows, busy["on"])
        c_reason = create_reason(prefix_error, exists, busy["on"], running_as_root())
        if busy["on"]:
            reason_var.set(run_state_text(run["kind"], run["stopping"]) + ".")
        else:
            reason_var.set("\n".join(r for r in (reason, c_reason) if r))
        run_btn.state(["disabled"] if reason else ["!disabled"])
        create_btn.state(["disabled"] if c_reason else ["!disabled"])

    def start_busy(message, kind):
        busy["on"] = True
        run["kind"], run["stopping"] = kind, False
        status_var.set(message)
        run["message"] = message
        create_btn.state(["disabled"])
        run_btn.state(["disabled"])
        new_btn.state(["disabled"])
        refresh_plan()
        refresh_footer()

    def end_busy():
        busy["on"] = False
        run["kind"], run["stopping"] = None, False
        new_btn.state(["!disabled"])
        refresh_plan()
        refresh_footer()

    def new_prefix():
        name = simpledialog.askstring("New prefix", "Name for the new prefix:", parent=root)
        if not name:
            return
        try:
            validate_prefix_name(name.strip())
        except ValueError as exc:
            messagebox.showerror("Prefix name", str(exc))
            return
        extra_names.append(name.strip())
        open_prefix(name.strip())

    def set_banners(wine_missing: bool):
        for slot, card in banners:
            if wine_missing:
                card.pack(fill="x", pady=card.pady)
            else:
                card.pack_forget()
                slot.configure(height=1)  # an emptied frame would otherwise keep its old size

    def refresh_env():
        report = run_doctor()
        wine_path["value"] = shutil.which("wine")
        wine_path["umu"] = shutil.which("umu-run")
        wr, br = report["wine"], report["bwrap"]
        set_banners(bool(wr.problem))
        for child in env_rows.winfo_children():
            child.destroy()
        for _i, (n, t) in enumerate((("Wine", wr), ("Bubblewrap", br), ("umu-run (optional)", report["umu"]))):
            r = tk.Frame(env_rows, bg=w.PANEL)
            r.pack(fill="x", pady=3)
            label(r, n, T["body"], False, FG, w.PANEL).pack(side="left")
            label(r, t.version if not t.problem else f"unavailable ({t.problem})", T["body"],
                  False, w.CYAN if not t.problem else w.RED, w.PANEL).pack(side="right")
        r = tk.Frame(env_rows, bg=w.PANEL)
        r.pack(fill="x", pady=3)
        label(r, "Platform", T["body"], False, FG, w.PANEL).pack(side="left")
        label(r, report["platform"], T["body"], False, w.MUTED, w.PANEL).pack(side="right")
        report_cache["text"] = describe_environment(report)
        refresh_scan()
        refresh_plan()
        refresh_footer()

    def create_clicked():
        if running_as_root():
            messagebox.showerror("Not as root", f"{ROOT_MESSAGE} Nothing was changed.")
            return
        name = selected_prefix()
        try:
            validate_prefix_name(name)
        except ValueError as exc:
            messagebox.showerror("Prefix name", str(exc))
            return
        tool = wine_path["umu"] if runtime_var.get() == "proton" else wine_path["value"]
        if not tool:
            messagebox.showerror("Runtime not found",
                                 f"{'umu-run' if runtime_var.get() == 'proton' else 'Wine'} is not on "
                                 "PATH. Nothing was changed.")
            return
        try:
            extra_env = runtime_env(runtime_var.get(), proton_dir())
        except ValueError as exc:
            messagebox.showerror("Proton", str(exc))
            return
        extra_env = {**extra_env, **wineboot_env(runtime_var.get(), addons_var.get())}
        plan = plan_create(name, tool, extra_env=extra_env)
        boot = boot_line(runtime_var.get(), addons_var.get())
        if not messagebox.askyesno(
            "Create prefix?",
            f"Create a new dedicated prefix at:\n{plan['path']}\n\nCommand: "
            f"{' '.join(plan['argv'])}\nRuntime: {runtime_var.get()}\n\n"
            + (f"{boot}\n\n" if boot else "") +
            "Your ~/.wine is not used or changed."
            + (RUNTIME_DOWNLOAD_NOTE if runtime_var.get() == "proton" else ""),
            default="no",
        ):
            return
        from gitfurl.route import NativeControl

        control = NativeControl()  # a flag: wineboot's own loop checks it (run_in_group)
        pending["control"], pending["portable"] = control, False
        pending["proton"] = runtime_var.get() == "proton"
        start_busy("Creating prefix (this can take a minute)...", "prefix")
        stop_btn.state(["!disabled"])
        stop_btn.pack(side="left", padx=(0, SP["md"]), before=create_btn)
        run_in_thread(lambda: create_prefix(name, tool, extra_env=extra_env,
                                            should_stop=lambda: control.cancelled), results)
        poll("prefix")

    def run_clicked():
        if running_as_root():
            messagebox.showerror("Not as root", f"{ROOT_MESSAGE} Nothing was run.")
            return
        route_cache.clear()  # Run decides from the files as they are now, not from the preview
        try:
            native = native_choice()
        except Exception as exc:  # noqa: BLE001 - shown, nothing started
            messagebox.showerror("Cannot run", f"{exc}. Nothing was run.")
            return
        if native is not None:
            run_native_clicked(native)
            return
        proton = runtime_var.get() == "proton"
        from datetime import datetime

        now = datetime.now()  # one time stamp: the approved text names the same Inno log
        text, err = describe_plan(installer_var.get(), selected_prefix(), runtime_var.get(),
                                  proton_dir(), portable_var.get(), scan_var.get(),
                                  memory_var.get(), unattended_var.get(), now,
                                  wine_addons=addons_var.get())
        err = err or memory_limit_problem(memory_var.get())
        if err:
            messagebox.showerror("Cannot run", err)
            return
        tool = wine_path["umu"] if proton else wine_path["value"]
        if not tool:
            messagebox.showerror("Runtime not found", f"{'umu-run' if proton else 'Wine'} is not on "
                                 "PATH. Nothing was run.")
            return
        try:
            scan_plan = scanmod.build_scan_plan(validate_installer(installer_var.get().strip()),
                                                scanmod.find_clamscan()) \
                if scan_var.get() else None
        except ValueError as exc:
            messagebox.showerror("Cannot scan", f"{exc}. Nothing was run.")
            return
        plan = build_plan(validate_installer(installer_var.get().strip()), selected_prefix(),
                          runtime=runtime_var.get(), proton_path=proton_dir(),
                          portable=portable_var.get(), scan=scan_plan,
                          memory_limit=memory_var.get(),
                          unattended=unattended_for(validate_installer(installer_var.get().strip()),
                                                    selected_prefix(), unattended_var.get(), now),
                          wine_addons=addons_var.get())
        missing = not plan.prefix.is_dir()
        if missing and not create_var.get():
            messagebox.showerror("Prefix missing", f"{plan.prefix} does not exist. Create it first, "
                                 "or turn on 'Create the prefix if it does not exist'.")
            return
        try:
            clean = plan_cleanup(validate_installer(installer_var.get().strip()), cleanup_var.get(),
                                 [plan.prefix, prefixes_root(), logs_root()])
        except ValueError as exc:
            messagebox.showerror("Cleanup not allowed", str(exc))
            return
        extra = f"\n\nThe prefix does not exist yet and will be created first:\n{plan.prefix}" \
            if missing else ""
        if cleanup_var.get() != "none":
            extra += "\n\nIf the installer exits with status 0, " + clean.describe()
        extra += "\n\n" + route_text_now()
        from gitfurl.route import setup_unpacked

        checked = validate_installer(installer_var.get().strip())
        extra += "\n\nChecks before the run (read-only, advisory):\n" + measure(
            checked, plan.prefix, unpacked=setup_unpacked(checked, detect(checked))).describe()
        if proton:
            extra += RUNTIME_DOWNLOAD_NOTE
        extra += "\n\n" + STALL_PLAN_LINE
        if changes_var.get():
            extra += "\n\nWhat the installer changed will be listed afterwards."
            if plan.portable:
                extra += " (checked places only)"
        if find_var.get():
            extra += "\n\nA guess at the installed program will be shown afterwards."
        if scan_plan is not None:  # a bounded version probe for the database date, as doctor
            extra += "\n\n" + "\n".join(scanmod.describe_database(
                scanmod.database_info(scan_plan.clamscan)))
            extra += ("\n\nThe ClamAV scan runs first, after you approve. A match stops the run "
                      "(you are then asked a second time whether to run it anyway); a scan error "
                      "always stops it and is never overridden, also when a signature matched "
                      "in a scan that had an error.")
        if not messagebox.askyesno("Run this installer?", f"{text}{extra}\n\n{WARNING}",
                                   icon="warning", default="no"):
            return
        pending["cleanup"] = clean
        cleanup_var.set("none")  # one run at a time; choose again for the next
        if plan.unattended is not None:
            busy = ("Preparing the prefix, then running the installer without its windows."
                    if missing else "Installer running without its windows.")
        else:
            busy = ("Preparing the prefix, then running the installer. Close its window when done."
                    if missing else "Installer running. Close its window when it is done.")
        start_busy(busy, "install")
        create, track = create_var.get(), changes_var.get() or find_var.get()
        ctx = {"prefix": selected_prefix_path(), "changes": changes_var.get(), "find": find_var.get(),
               "portable": plan.portable}
        bar.set(0)
        progress_var.set("Waiting for the installer to start reading its files...")
        control = RunControl()
        pending["control"], pending["proton"] = control, proton
        pending["portable"] = plan.portable
        options = RunOptions(on_progress=ui_updates.put, progress_interval=3.0,
                             detailed_log=detail_var.get(), control=control,
                             on_note=note_received, stall_after_s=WINDOW_STALL_S)
        if scan_plan is not None:
            status_var.set("Scanning the installer with ClamAV first...")
        launch(plan, tool, create, track, options, ctx)

    def run_native_clicked(choice):
        """The native route: Setup is not run; gitfurl unpacks the archives itself after one
        approval of the whole plan (scan, prefix creation, unpack, listings, cleanup)."""
        installer = validate_installer(installer_var.get().strip())
        det, _c, _e = current_route()
        xplan = choice.extract
        try:
            scan_plan = scanmod.build_scan_plan(installer, scanmod.find_clamscan()) \
                if scan_var.get() else None
            clean = plan_cleanup(installer, cleanup_var.get(),
                                 [xplan.prefix, prefixes_root(), logs_root()])
        except ValueError as exc:
            messagebox.showerror("Cannot run", f"{exc}. Nothing was run.")
            return
        db = scanmod.database_info(scan_plan.clamscan) if scan_plan is not None else None
        plan, stops, mem_lines = native_plan_for(
            installer, choice, create_ok=create_var.get(), memory_on=memory_var.get(),
            wine=wine_path["value"], scan_plan=scan_plan, db_info=db,
            cleanup_text=clean.describe() if cleanup_var.get() != "none" else None)
        if stops:
            messagebox.showerror("Cannot run", "\n\n".join(native_stop_text(s) for s in stops)
                                 + "\n\nNothing was run, created or written.")
            return
        text = native_plan_text(plan, det, memory_lines=mem_lines,
                                preflight=measure(installer, xplan.prefix).describe(),
                                wine_addons=addons_var.get())
        if not messagebox.askyesno("Unpack this installer?", text, icon="warning",
                                   default="no"):
            return
        pending["cleanup"] = clean
        cleanup_var.set("none")  # one run at a time; choose again for the next
        start_busy("Creating the prefix, then unpacking (Setup is not run)..."
                   if plan.create_prefix else "Unpacking (Setup is not run)...", "native")
        bar.set(0)
        progress_var.set("Waiting for the first files to be written...")
        if scan_plan is not None:
            status_var.set("Scanning the installer with ClamAV first...")
        launch_native(plan, scan_plan, None, False, memory_var.get(), detail_var.get())

    def launch_native(plan, scan_plan, scan_result, allow_hit, memory_on, detail):
        """Start the approved native run in a thread (scan, prefix creation, unpack)."""
        from gitfurl.route import NativeControl, perform_native

        control = NativeControl()
        pending["control"], pending["proton"], pending["portable"] = control, False, False
        pending["native"] = (plan, scan_plan, memory_on, detail)  # as approved
        pending["notes"] = []
        stop_btn.state(["!disabled"])
        stop_btn.pack(side="left", padx=(0, SP["md"]), before=create_btn)
        run_in_thread(lambda: perform_native(
            plan, control, memory_limit=memory_on, scan_plan=scan_plan,
            scan_result=scan_result, allow_scan_hit=allow_hit,
            on_progress=lambda d, t, e: ui_updates.put(NativeSample(d, t, e)),
            on_note=note_received, detailed_log=detail), results)
        poll("native")

    def native_hit_override(blocked) -> bool:
        """After a ClamAV match on the native route: a second confirmation (default No)."""
        plan, scan_plan, memory_on, detail = pending["native"]
        if not messagebox.askyesno(
                "Unpack it anyway?", f"{blocked}\n\nThis is a second confirmation. Unpack the "
                "installer even though ClamAV matched a signature?", icon="warning",
                default="no"):
            return False
        start_busy("Unpacking (ClamAV match overridden; Setup is not run)...", "native")
        launch_native(plan, scan_plan, blocked.result, True, memory_on, detail)
        return True

    def native_finished(state, value, control):
        if state == "error" and isinstance(value, scanmod.ScanBlocked):
            return False  # handled with the Wine route's scan branch
        if state == "error":
            status_var.set(f"Failed: {value}")
            status_label.configure(fg=w.RED)
            pending["cleanup"] = None
            return True
        cleanup_line = None
        clean = pending["cleanup"]
        stopped = control is not None and control.cancelled
        if clean is not None and clean.mode != "none":
            if native_cleanup_allowed(value, stopped):
                deleted, problems = perform_cleanup(clean, 0)
                cleanup_line = f"Cleanup: deleted {deleted} file(s)" + (
                    f", {len(problems)} problem(s)" if problems else "")
            else:
                cleanup_line = ("Cleanup skipped: you pressed Stop." if stopped
                                else "skipped")
        pending["cleanup"] = None
        if value.result is not None and value.result.status == 0:
            bar.set(100)
            if stopped:
                pending.setdefault("notes", []).append(
                    "Stop came after the unpack had finished; the files are complete.")
        text, colour = native_end_text(value, cleanup_line, pending.get("notes") or [])
        status_var.set(text)
        status_label.configure(fg={"ok": w.CYAN, "warn": w.AMBER}.get(colour, w.RED))
        refresh_list()
        return True

    def note_received(message):
        """A run note (scan result, memory limit change, stall advisory): shown on the
        progress line now and kept for the end text."""
        pending["notes"].append(message)
        ui_updates.put(message)

    def launch(plan, tool, create, track, options, ctx):
        """Start the approved run in a thread (the scan, if any, runs first inside it)."""
        pending["rerun"] = (plan, tool, create, track, options, ctx)
        pending["notes"] = []
        stop_btn.state(["!disabled"])  # a Stop from an earlier run must not leave it disabled
        stop_btn.pack(side="left", padx=(0, SP["md"]), before=create_btn)
        run_in_thread(lambda: run_tracked(plan, tool, create, track,
                                          options=options), results)
        poll("install", ctx)

    def scan_hit_override(blocked) -> bool:
        """After a ClamAV match: a second, explicit confirmation (default No). True = run."""
        plan, tool, create, track, options, ctx = pending["rerun"]
        if not messagebox.askyesno(
                "Run it anyway?", f"{blocked}\n\nThis is a second confirmation. Run the "
                "installer even though ClamAV matched a signature?", icon="warning",
                default="no"):
            return False
        control = RunControl()
        pending["control"] = control
        start_busy("Installer running (ClamAV match overridden). Close its window when it is "
                   "done.", "install")
        launch(plan, tool, create, track,
               replace(options, control=control, scan_result=blocked.result,
                       allow_scan_hit=True), ctx)
        return True

    def stop_clicked():
        control = pending["control"]
        if not stop_applies(pending, control):
            return
        if run["kind"] == "prefix":
            if not messagebox.askyesno(
                    "Stop creating the prefix?",
                    f"{PREFIX_STOP_TEXT_PROTON if pending['proton'] else PREFIX_STOP_TEXT}"
                    "\n\nStop it?",
                    icon="warning", default="no"):
                return
            if not stop_applies(pending, control):  # wineboot ended while the dialog was open
                return
            stop_btn.state(["disabled"])
            run["stopping"] = True
            status_var.set("Stopping the prefix creation...")
            refresh_plan()
            refresh_footer()
            control.request_stop()  # sets the flag; wineboot's loop ends its group
            return
        if run["kind"] == "native":
            if not messagebox.askyesno(
                    "Stop the unpack?", f"{NATIVE_STOP_LINE}\n\nThe run is reported as "
                    "stopped and the log is kept.\n\nStop it?", icon="warning", default="no"):
                return
            if not stop_applies(pending, control):
                return
            stop_btn.state(["disabled"])
            run["stopping"] = True
            status_var.set("Stopping...")
            refresh_plan()
            refresh_footer()
            threading.Thread(target=control.request_stop, daemon=True).start()
            return
        server = ("\n\nThis was a portable run: the sandbox ends with the run's process group, "
                  "so wineserver -k is not run." if pending.get("portable") else
                  "" if pending["proton"] else
                  "\n\nThen this prefix's Wine server is asked to exit (wineserver -k). That also "
                  "ends anything you started by hand in this same prefix.")
        if not messagebox.askyesno(
                "Stop the installer?",
                "Stop sends a polite stop to the installer gitfurl started and the programs it "
                "started in its own group, waits 10 seconds, then forces whatever is left." + server
                + "\n\nNothing is deleted. Partly installed files and registry changes can remain "
                "in the prefix, and files may have been written elsewhere. A program that moved "
                "itself out of the run's group, or (for Proton) the runtime's own container, may keep "
                "running; this is not guaranteed to end everything. The run is reported as "
                "cancelled and the log is kept.\n\nStop it?", icon="warning", default="no"):
            return
        if not stop_applies(pending, control):  # the run ended while the dialog was open
            return
        stop_btn.state(["disabled"])
        run["stopping"] = True
        status_var.set("Stopping the installer...")
        refresh_plan()
        refresh_footer()
        threading.Thread(target=control.request_stop, daemon=True).start()

    def show_report(title, body):
        win = tk.Toplevel(root)
        win.title(title)
        win.configure(bg=w.BG)
        box = text_box(win, 24)
        box.pack(fill="both", expand=True, padx=12, pady=12)
        set_text(box, body)

    def poll(kind, ctx=None):
        try:
            while True:  # keep only the newest progress sample
                smp = ui_updates.get_nowait()
                if isinstance(smp, str):  # a note (memory limit change, stall advisory)
                    progress_var.set(smp)
                    if keeps_note(smp) and busy["on"] and not run["stopping"]:
                        status_var.set(f"{run.get('message', '')}\n{smp}".strip())
                        status_label.configure(fg=w.AMBER)
                    continue
                bar.set(smp.percent or 0)
                progress_var.set(smp.line())
        except queue.Empty:
            pass
        except Exception:  # noqa: BLE001 - a bad sample must not stop polling or leave the app busy
            progress_var.set("Progress is unavailable for this run.")
        try:
            state, value = results.get_nowait()
        except queue.Empty:
            root.after(300, poll, kind, ctx)
            return
        end_busy()
        stop_btn.pack_forget()
        stop_btn.state(["!disabled"])
        control = pending["control"]
        pending["control"] = None
        if kind == "native" and native_finished(state, value, control):
            return
        if state == "ok" and kind == "install" and control is not None and control.cancelled:
            log = value[1]
            notes = "".join(f"\nNote: {n}" for n in control.notes)
            facts = describe_result(value)
            status_var.set(f"Cancelled. {CANCEL_REMAINS}{notes}\n"
                           f"{facts + chr(10) if facts else ''}Cleanup skipped. "
                           f"Local log: {log} (may contain your user name and paths)")
            status_label.configure(fg=w.AMBER)
            pending["cleanup"] = None
            return
        if state == "error" and isinstance(value, scanmod.ScanBlocked):
            status_var.set(f"Not run. {value}")
            status_label.configure(fg=w.AMBER)
            hit = value.result is not None and value.result.status == scanmod.HIT
            override = native_hit_override if kind == "native" else scan_hit_override
            if hit and not (control is not None and control.cancelled) and override(value):
                return
            pending["cleanup"] = None
            return
        if state == "error" and kind == "prefix" and isinstance(value, PrefixStopped):
            status_var.set(f"Stopped: {value}")
            status_label.configure(fg=w.AMBER)
            refresh_list()
            return
        if state == "error":
            status_var.set(f"Failed: {value}")
            status_label.configure(fg=w.RED)
        elif kind == "prefix":
            status_var.set(f"The prefix tool exited with status {value}."
                           + ("" if value == 0 else " Check that Wine works; run doctor."))
            status_label.configure(fg=w.CYAN if value == 0 else w.RED)
            refresh_list()
        else:
            status, log, changes = value
            if status == 0:
                bar.set(100)
            note = "" if status == 0 else " (a non-zero status does not say why; read the log)"
            extra = ""
            clean = pending["cleanup"]
            if clean is not None and clean.mode != "none":
                if status == 0:
                    deleted, problems = perform_cleanup(clean, status)
                    extra = f"\nCleanup: deleted {deleted} file(s)" + (
                        f", {len(problems)} problem(s)" if problems else "")
                else:
                    extra = "\nCleanup skipped: the installer did not exit with status 0."
            facts = describe_result(value)
            extra += f"\n{facts}" if facts else ""
            notes = pending.get("notes") or []
            if len(notes) > 5:  # the first (the scan result, if any) and the latest four
                notes = [notes[0], "...", *notes[-4:]]
            extra += "".join(f"\n{n}" for n in notes)
            status_var.set(f"Installer exited with status {status}{note}{extra}\n"
                           f"Local log: {log} (may contain your user name and paths)")
            status_label.configure(fg=w.CYAN if status == 0 else w.AMBER)
            if changes is not None:
                parts = []
                if ctx and ctx["changes"]:
                    parts.append(changes.describe() + (
                        "\nPortable run: this covers the checked places only; it says nothing "
                        "about places gitfurl did not check." if ctx.get("portable") else ""))
                if ctx and ctx["find"]:
                    parts.append(changes.describe_executables(ctx["prefix"]))
                show_report("After the install", "\n\n".join(parts))

    def on_close():
        if busy["on"] and not messagebox.askyesno(
                "Quit while running?",
                "Something is still running. Closing gitfurl now stops watching it and the log "
                "may be incomplete; the installer's own window may stay open.\n\nQuit anyway?",
                default="no"):
            return
        root.destroy()

    def on_exception(exc, val, tb):
        import traceback

        traceback.print_exception(exc, val, tb)
        if busy["on"]:
            end_busy()
        messagebox.showerror("Something went wrong", f"{exc.__name__}: {val}\n\nNothing more was "
                             "started. Details are on the terminal if you started gitfurl from one.")

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.report_callback_exception = on_exception
    installer_var.trace_add("write", refresh_plan)
    create_var.trace_add("write", refresh_plan)
    changes_var.trace_add("write", refresh_plan)  # a Setup-only option: it changes the route
    proton_var.trace_add("write", refresh_plan)
    refresh_env()
    show(start)
    failed = {"on": False}
    if driver is not None:
        def drive():
            import traceback

            try:
                ui = {
                    "root": root,
                    "show": show,
                    "pages": pages,
                    "nav_items": nav_items,
                    "nav_state": nav_state,
                    "cards": cards,
                    "canvases": canvases,
                    "inners": inners,
                    "run_btn": run_btn,
                    "create_btn": create_btn,
                    "stop_btn": stop_btn,
                    "foot_env": foot_env,
                    "foot_hints": foot_hints,
                    "foot_state": foot_state,
                    "reason_var": reason_var,
                    "check_rows": check_rows,
                    "banners": banners,
                    "wine_path": wine_path,
                    "refresh_plan": refresh_plan,
                    "refresh_footer": refresh_footer,
                    "action": action,
                    "installer_var": installer_var,
                    "create_var": create_var,
                    "memory_var": memory_var,
                    "unattended_var": unattended_var,
                    "memory_switch": memory_switch,
                    "run": run,
                    "busy": busy,
                    "plan_box": plan_box,
                    "new_btn": new_btn,
                    "prefix_var": prefix_var,
                    "set_banners": set_banners,
                    "scan_var": scan_var,
                    "scan_switch": scan_switch,
                    "scan_reason": scan_reason,
                    "setup_var": setup_var,
                    "addons_var": addons_var,
                    "changes_var": changes_var,
                    "plan_checklist_rows": lambda: {k: (bool(f.winfo_manager()), m.cget("text"))
                                                    for k, (f, _mk, m) in check_rows.items()},
                    "run_clicked": run_clicked,
                    "stop_clicked": stop_clicked,
                    "status_var": status_var,
                    "progress_var": progress_var,
                    "pending": pending,
                }
                driver(root, ui)
            except BaseException:  # noqa: BLE001 - reported on stderr, the exit status says it
                traceback.print_exc()
                failed["on"] = True
                root.destroy()

        def driver_error(*exc):
            import traceback

            traceback.print_exception(*exc)
            failed["on"] = True
            root.destroy()

        root.report_callback_exception = driver_error
        root.after(50, drive)
    root.mainloop()
    return 1 if failed["on"] else 0
