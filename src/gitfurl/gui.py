"""Graphical interface (tkinter). A thin layer over the shared core.

The window never runs anything by itself: creating a prefix or running an installer
each needs a confirmation dialog that shows what will happen.
"""

from __future__ import annotations

import os
import queue
import shutil
import threading
from pathlib import Path

from gitfurl import __version__
from gitfurl.cleanup import MODES, perform_cleanup, plan_cleanup
from gitfurl.detect import describe as describe_detection
from gitfurl.detect import detect
from gitfurl.doctor import run_doctor
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
    RUNTIMES,
    RunControl,
    RunOptions,
    build_plan,
    logs_root,
    run_tracked,
    runtime_env,
    validate_installer,
)

# vim-tictoc colours (same as the assets and gitfurl.widgets)
FG, MUTED = "#F8F8F8", "#AEAEAE"

WARNING = (
    "A Wine prefix is not a sandbox: the installer can reach files your user can reach.\n"
    "Only run installers you are authorised to use and have chosen to trust."
)


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


def describe_plan(installer: str, prefix: str, runtime: str = "wine",
                  proton_path: str | None = None) -> tuple[str, str | None]:
    """Return (preview text, error). No side effects."""
    if not installer.strip():
        return "", "Choose an installer file."
    if not prefix.strip():
        return "", "Choose or create a prefix."
    try:
        plan = build_plan(validate_installer(installer.strip()), prefix.strip(),
                          runtime=runtime, proton_path=proton_path)
    except ValueError as exc:
        return "", str(exc)
    text = (
        f"command:     {plan.preview()}\n"
        f"prefix:      {plan.prefix}\n"
        f"working dir: {plan.cwd}\n"
        + "".join(f"env:         {k}={v}\n" for k, v in plan.env_overrides.items())
    )
    return text.rstrip(), None


def run_in_thread(fn, results: queue.Queue) -> None:
    def work():
        try:
            results.put(("ok", fn()))
        except BaseException as exc:  # noqa: BLE001 - shown to the user, not swallowed
            results.put(("error", exc))

    threading.Thread(target=work, daemon=True).start()


RUNTIME_DOWNLOAD_NOTE = ("\n\nProton runs through umu-run. If its runtime is not already on this "
                         "machine, umu-run downloads it on first use (a network download).")


def stop_applies(pending: dict, control) -> bool:
    """True while `control` belongs to the run in progress and has not been stopped yet.

    The Stop dialog is modal and the run can end while it is open, so this is checked again
    after the answer, before anything on screen is changed.
    """
    return control is not None and pending.get("control") is control and not control.cancelled


def main(start: str = "library", prefix: str | None = None) -> int:
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
    root.geometry("1040x740")
    try:
        icon = Path(__file__).resolve().parent / "icon.png"
        if icon.is_file():
            root.iconphoto(True, tk.PhotoImage(file=str(icon)))
    except tk.TclError:
        pass
    def F(size=10, bold=False):
        return w.pick_font(root, size, bold)

    SIDE = "#10152a"

    results: queue.Queue = queue.Queue()
    ui_updates: queue.Queue = queue.Queue()
    installer_var, status_var = tk.StringVar(), tk.StringVar()
    prefix_var = tk.StringVar(value=prefix or "default")
    create_var, changes_var, detail_var = (tk.BooleanVar(value=True), tk.BooleanVar(value=False),
                                           tk.BooleanVar(value=False))
    cleanup_var = tk.StringVar(value="none")
    runtime_var = tk.StringVar(value="wine")
    find_var = tk.BooleanVar(value=False)
    progress_var = tk.StringVar(value="Shown while an installer runs: payload read, rate, rough ETA.")
    pending = {"cleanup": None, "control": None, "proton": False}
    proton_var = tk.StringVar(value=os.environ.get("PROTONPATH", ""))
    busy = {"on": False}
    wine_path = {"value": None, "umu": None}
    extra_names: list[str] = ["default"]
    report_cache = {"text": ""}
    pages: dict[str, tk.Frame] = {}
    nav_items: dict[str, tk.Canvas] = {}

    def label(parent, text="", size=10, bold=False, fg=FG, bg=w.BG, **kw):
        return tk.Label(parent, text=text, bg=bg, fg=fg, font=F(size, bold), **kw)

    def text_box(parent, height):
        return tk.Text(parent, height=height, bg=w.BG, fg=FG, relief="flat", wrap="word",
                       highlightthickness=1, highlightbackground=w.LINE, state="disabled",
                       font=("monospace", 10), padx=10, pady=8)

    def set_text(box, value):
        box.configure(state="normal")
        box.delete("1.0", "end")
        box.insert("end", value)
        box.configure(state="disabled")

    def entry(parent, var):
        return tk.Entry(parent, textvariable=var, bg=w.BG, fg=FG, insertbackground=FG, relief="flat",
                        highlightthickness=1, highlightbackground=w.LINE, highlightcolor=w.ORANGE,
                        font=F(10))

    def heading(parent, text):
        label(parent, text.upper(), 9, True, w.MUTED).pack(anchor="w", pady=(14, 6), padx=4)

    def row(card, title, subtitle=None, widget=None, first=False):
        """One row of a boxed list: title and subtitle left, a control right."""
        if not first:
            tk.Frame(card.body, bg=w.LINE, height=1).pack(fill="x", pady=6)
        r = tk.Frame(card.body, bg=w.PANEL)
        r.pack(fill="x")
        if widget:  # packed first so a narrow window clips the text, never the control
            widget(r).pack(side="right", padx=(12, 0))
        left = tk.Frame(r, bg=w.PANEL)
        left.pack(side="left", fill="x", expand=True)
        label(left, title, 10, False, FG, w.PANEL).pack(anchor="w")
        if subtitle:
            label(left, subtitle, 9, False, w.MUTED, w.PANEL, wraplength=440, justify="left"
                  ).pack(anchor="w")
        return r

    # ---- sidebar: pages, like rc's navigation split view ----
    side = tk.Frame(root, bg=SIDE, width=230)
    side.pack(side="left", fill="y")
    side.pack_propagate(False)
    w.Wordmark(side, cell=5, outer=SIDE).pack(anchor="w", padx=18, pady=(20, 2))
    label(side, f"v{__version__}  first release", 9, False, w.MUTED, SIDE).pack(anchor="w", padx=20)
    nav = tk.Frame(side, bg=SIDE)
    nav.pack(fill="x", pady=(22, 0), padx=10)

    def nav_item(key, text):
        c = tk.Canvas(nav, height=38, bg=SIDE, highlightthickness=0, bd=0, cursor="hand2")
        c.pack(fill="x", pady=2)

        def go():
            if not busy["on"]:
                show(key)

        def draw(_e=None):
            c.delete("all")
            width = c.winfo_width()
            on = nav_state["page"] == key or (key == "library" and nav_state["page"] == "details")
            if on:
                w.rounded(c, 0, 0, width, 38, 8, fill=w.LINE, outline=w.LINE)
            c.create_rectangle(14, 15, 22, 23, fill=w.ORANGE if on else w.LINE, outline="")
            c.create_text(36, 19, text=text, anchor="w", fill=FG if on else w.MUTED, font=F(10, on))
            w.focus_ring(c, width, 38, 8)

        c.bind("<Configure>", draw)
        c.bind("<Button-1>", lambda _e: go())
        w.make_focusable(c, go, draw)
        c.redraw = draw
        nav_items[key] = c

    nav_state = {"page": start}
    nav_item("library", "Library")
    nav_item("settings", "Settings")
    env_umu = label(side, "", 9, False, w.MUTED, SIDE, justify="left", anchor="w")
    env_umu.pack(side="bottom", fill="x", padx=20, pady=(0, 16))
    env_wine = label(side, "", 9, False, w.MUTED, SIDE, justify="left", anchor="w")
    env_wine.pack(side="bottom", fill="x", padx=20)

    content = tk.Frame(root, bg=w.BG)
    content.pack(side="left", fill="both", expand=True)

    canvases: dict[str, tk.Canvas] = {}

    def wheel(direction):
        def handler(e):
            if e.widget.winfo_toplevel() is not root or isinstance(e.widget, tk.Text):
                return
            cv = canvases.get(nav_state["page"])
            if cv is not None:
                cv.yview_scroll(direction * 3, "units")
        return handler

    root.bind_all("<Button-4>", wheel(-1))  # X11 wheel up
    root.bind_all("<Button-5>", wheel(1))
    root.bind_all("<MouseWheel>", lambda e: wheel(-1 if e.delta > 0 else 1)(e))

    def scroll_page(name):
        outer = tk.Frame(content, bg=w.BG)
        cv = tk.Canvas(outer, bg=w.BG, highlightthickness=0, bd=0)
        cv.pack(fill="both", expand=True)
        inner = tk.Frame(cv, bg=w.BG)
        win = cv.create_window(0, 0, window=inner, anchor="nw")
        cv.bind("<Configure>", lambda e: cv.itemconfigure(win, width=e.width))
        inner.bind("<Configure>", lambda _e: cv.configure(scrollregion=cv.bbox("all")))
        canvases[name] = cv
        pad = tk.Frame(inner, bg=w.BG)
        pad.pack(fill="both", expand=True, padx=28, pady=22)
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

    # ---- Library page ----
    lib = scroll_page("library")
    top = tk.Frame(lib, bg=w.BG)
    top.pack(fill="x")
    label(top, "Library", 22, True).pack(side="left")
    new_btn = w.PillButton(top, "+  New prefix", lambda: new_prefix(), "accent", F(10, True))
    new_btn.pack(side="right")
    label(lib, "Each prefix is a separate, dedicated Wine environment. Your own ~/.wine is never used.",
          10, False, w.MUTED).pack(anchor="w", pady=(2, 8))
    wine_note = label(lib, "", 10, False, w.AMBER, wraplength=640, justify="left")
    wine_note.pack(anchor="w", pady=(0, 8))
    list_area = tk.Frame(lib, bg=w.BG)
    list_area.pack(fill="x")
    names: list[str] = []

    def open_prefix(name):
        if busy["on"]:
            return
        prefix_var.set(name)
        show("details")

    def refresh_list(_select=None):
        for child in list_area.winfo_children():
            child.destroy()
        existing = list_prefixes()
        names[:] = sorted(set(existing) | set(extra_names))
        for i, n in enumerate(names):
            c = w.RoundCard(list_area, pad=12)
            c.pack(fill="x", pady=5)
            r = tk.Frame(c.body, bg=w.PANEL)
            r.pack(fill="x")
            w.Tile(r, n[:1] or "g", w.ACCENTS[i % 4]).pack(side="left")
            ready = n in existing
            label(r, "ready" if ready else "not created yet", 9, True,
                  w.CYAN if ready else w.AMBER, w.PANEL).pack(side="right", padx=6)
            mid = tk.Frame(r, bg=w.PANEL)
            mid.pack(side="left", padx=14, fill="x", expand=True)
            label(mid, n, 12, True, FG, w.PANEL).pack(anchor="w")
            label(mid, short_path(prefixes_root() / n), 9, False, w.MUTED, w.PANEL,
                  wraplength=400, justify="left").pack(anchor="w")
            for widget in [c, c.body, r, mid, *mid.winfo_children(), *r.winfo_children()]:
                widget.bind("<Button-1>", lambda _e, n=n: open_prefix(n))
                widget.configure(cursor="hand2")
        refresh_plan()

    # ---- Details page ----
    det = scroll_page("details")
    dtop = tk.Frame(det, bg=w.BG)
    dtop.pack(fill="x")
    w.PillButton(dtop, "<  Library", lambda: show("library"), "ghost", F(10)).pack(anchor="w")
    dhead = tk.Frame(det, bg=w.BG)
    dhead.pack(fill="x", pady=(8, 0))
    title_var, sub_var = tk.StringVar(), tk.StringVar()
    run_btn = w.PillButton(dhead, "Run installer", lambda: run_clicked(), "accent", F(11, True),
                           pad=(22, 10))
    run_btn.pack(side="right")
    create_btn = w.PillButton(dhead, "Create now", lambda: create_clicked(), "plain", F(10))
    create_btn.pack(side="right", padx=10)
    stop_btn = w.PillButton(dhead, "Stop", lambda: stop_clicked(), "plain", F(10))  # shown during a run
    tk.Label(dhead, textvariable=title_var, bg=w.BG, fg=FG, font=F(22, True)).pack(side="left", anchor="n")
    tk.Label(det, textvariable=sub_var, bg=w.BG, fg=w.MUTED, font=F(9), wraplength=640,
             justify="left").pack(anchor="w")
    hint_var = tk.StringVar()
    tk.Label(det, textvariable=hint_var, bg=w.BG, fg=w.AMBER, font=F(10), justify="left",
             wraplength=640).pack(anchor="w", pady=(4, 0))
    status_label = tk.Label(det, textvariable=status_var, bg=w.BG, fg=w.CYAN, font=F(10),
                            justify="left", wraplength=640)
    status_label.pack(anchor="w", pady=(4, 0))

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
    w.PillButton(ir, "Browse...", browse, "plain", F(10), outer=w.PANEL).pack(side="left", padx=(10, 0))

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
                b = w.PillButton(f, key, lambda k=key: pick(k), "plain", F(9), outer=w.PANEL,
                                 pad=(10, 5))
                b.pack(side="left", padx=2)
                buttons[key] = b
            paint()
            return f

        return build

    heading(det, "Options  (all off except creating the prefix)")
    c = w.RoundCard(det)
    c.pack(fill="x")
    row(c, "Create the prefix if it does not exist", "On by default. The prefix is created first, "
        "then the installer runs.", lambda p: w.Switch(p, create_var), first=True)
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
            "plain", F(9), outer=w.PANEL, pad=(10, 5)).pack(side="left", padx=(8, 0))
        return f

    row(c, "Proton folder", "Only used with the proton runtime. Defaults to PROTONPATH.", proton_row)

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
    tk.Label(c.body, textvariable=progress_var, bg=w.PANEL, fg=w.MUTED, font=F(9), justify="left",
             anchor="w", wraplength=640).pack(fill="x", pady=(8, 0))
    tk.Label(det, text=WARNING, bg=w.BG, fg=w.YELLOW, font=F(9), justify="left"
             ).pack(anchor="w", pady=(14, 0))

    # ---- Settings page ----
    st = scroll_page("settings")
    label(st, "Settings", 22, True).pack(anchor="w")
    heading(st, "Environment")
    env_card = w.RoundCard(st)
    env_card.pack(fill="x")
    env_rows = tk.Frame(env_card.body, bg=w.PANEL)
    env_rows.pack(fill="x")
    w.PillButton(env_card.body, "Check again", lambda: refresh_env(), "plain", F(9), outer=w.PANEL,
                 pad=(12, 6)).pack(anchor="w", pady=(8, 0))
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

    def refresh_plan(*_):
        name = selected_prefix()
        title_var.set(name or "No prefix")
        try:
            path = prefixes_root() / validate_prefix_name(name)
            exists = path.is_dir()
            sub_var.set(f"{short_path(path)}   {'ready' if exists else 'not created yet'}")
        except ValueError:
            exists = False
            sub_var.set("invalid name")
        text, err = describe_plan(installer_var.get(), name, runtime_var.get(), proton_dir())
        set_text(plan_box, text or (err or ""))
        hint_var.set("" if not err or busy["on"] else f"Run is off: {err}")
        ok = not err and not busy["on"]
        run_btn.state(["!disabled"] if ok else ["disabled"])
        create_btn.state(["!disabled"] if (not exists and not busy["on"]) else ["disabled"])

    def start_busy(message):
        busy["on"] = True
        status_var.set(message)
        create_btn.state(["disabled"])
        run_btn.state(["disabled"])
        new_btn.state(["disabled"])

    def end_busy():
        busy["on"] = False
        new_btn.state(["!disabled"])
        refresh_plan()

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

    def refresh_env():
        report = run_doctor()
        wine_path["value"] = shutil.which("wine")
        wine_path["umu"] = shutil.which("umu-run")
        wr, br = report["wine"], report["bwrap"]
        env_wine.configure(text=wr.version or "wine missing",
                           fg=w.CYAN if not wr.problem else w.RED)
        ur = report["umu"]
        if not ur.problem:
            umu_text = "umu-run ok"
        elif "not found" in ur.problem:
            umu_text = "umu-run not installed"
        else:
            umu_text = f"umu-run: {ur.problem}"
        env_umu.configure(text=umu_text, fg=w.CYAN if not ur.problem else w.MUTED)
        wine_note.configure(
            text="" if not wr.problem else
            "Wine was not found. Install it with your package manager (for example the "
            "'wine' package), then press Check again in Settings.")
        for child in env_rows.winfo_children():
            child.destroy()
        for _i, (n, t) in enumerate((("Wine", wr), ("Bubblewrap", br), ("umu-run (optional)", report["umu"]))):
            r = tk.Frame(env_rows, bg=w.PANEL)
            r.pack(fill="x", pady=3)
            label(r, n, 10, False, FG, w.PANEL).pack(side="left")
            label(r, t.version if not t.problem else f"unavailable ({t.problem})", 10, False,
                  w.CYAN if not t.problem else w.RED, w.PANEL).pack(side="right")
        r = tk.Frame(env_rows, bg=w.PANEL)
        r.pack(fill="x", pady=3)
        label(r, "Platform", 10, False, FG, w.PANEL).pack(side="left")
        label(r, report["platform"], 10, False, w.MUTED, w.PANEL).pack(side="right")
        report_cache["text"] = describe_environment(report)

    def create_clicked():
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
        plan = plan_create(name, tool, extra_env=extra_env)
        if not messagebox.askyesno(
            "Create prefix?",
            f"Create a new dedicated prefix at:\n{plan['path']}\n\nCommand: "
            f"{' '.join(plan['argv'])}\nRuntime: {runtime_var.get()}\n\n"
            "Your ~/.wine is not used or changed."
            + (RUNTIME_DOWNLOAD_NOTE if runtime_var.get() == "proton" else ""),
            default="no",
        ):
            return
        start_busy("Creating prefix (this can take a minute)...")
        run_in_thread(lambda: create_prefix(name, tool, extra_env=extra_env), results)
        poll("prefix")

    def run_clicked():
        proton = runtime_var.get() == "proton"
        text, err = describe_plan(installer_var.get(), selected_prefix(), runtime_var.get(),
                                  proton_dir())
        if err:
            messagebox.showerror("Cannot run", err)
            return
        tool = wine_path["umu"] if proton else wine_path["value"]
        if not tool:
            messagebox.showerror("Runtime not found", f"{'umu-run' if proton else 'Wine'} is not on "
                                 "PATH. Nothing was run.")
            return
        plan = build_plan(validate_installer(installer_var.get().strip()), selected_prefix(),
                          runtime=runtime_var.get(), proton_path=proton_dir())
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
        extra += "\n\n" + describe_detection(detect(validate_installer(installer_var.get().strip())))
        extra += "\n\nChecks before the run (read-only, advisory):\n" + measure(
            validate_installer(installer_var.get().strip()), plan.prefix).describe()
        if proton:
            extra += RUNTIME_DOWNLOAD_NOTE
        if changes_var.get():
            extra += "\n\nWhat the installer changed will be listed afterwards."
        if find_var.get():
            extra += "\n\nA guess at the installed program will be shown afterwards."
        if not messagebox.askyesno("Run this installer?", f"{text}{extra}\n\n{WARNING}",
                                   icon="warning", default="no"):
            return
        pending["cleanup"] = clean
        cleanup_var.set("none")  # one run at a time; choose again for the next
        start_busy("Preparing the prefix, then running the installer. Close its window when done."
                   if missing else "Installer running. Close its window when it is done.")
        create, track = create_var.get(), changes_var.get() or find_var.get()
        ctx = {"prefix": selected_prefix_path(), "changes": changes_var.get(), "find": find_var.get()}
        bar.set(0)
        progress_var.set("Waiting for the installer to start reading its files...")
        control = RunControl()
        pending["control"], pending["proton"] = control, proton
        options = RunOptions(on_progress=ui_updates.put, progress_interval=3.0,
                             detailed_log=detail_var.get(), control=control)
        stop_btn.state(["!disabled"])  # a Stop from an earlier run must not leave it disabled
        stop_btn.pack(side="right", padx=10)
        run_in_thread(lambda: run_tracked(plan, tool, create, track,
                                          options=options), results)
        poll("install", ctx)

    def stop_clicked():
        control = pending["control"]
        if not stop_applies(pending, control):
            return
        server = ("" if pending["proton"] else
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
        status_var.set("Stopping the installer...")
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
        if state == "ok" and kind == "install" and control is not None and control.cancelled:
            log = value[1]
            notes = "".join(f"\nNote: {n}" for n in control.notes)
            status_var.set(f"Cancelled. {CANCEL_REMAINS}{notes}\nCleanup skipped. "
                           f"Local log: {log} (may contain your user name and paths)")
            status_label.configure(fg=w.AMBER)
            pending["cleanup"] = None
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
            status_var.set(f"Installer exited with status {status}{note}{extra}\n"
                           f"Local log: {log} (may contain your user name and paths)")
            status_label.configure(fg=w.CYAN if status == 0 else w.AMBER)
            if changes is not None:
                parts = []
                if ctx and ctx["changes"]:
                    parts.append(changes.describe())
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
    proton_var.trace_add("write", refresh_plan)
    refresh_env()
    show(start)
    root.mainloop()
    return 0
