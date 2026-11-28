"""The window's "Setup's choices" card: Setup's questions as checkboxes and radio buttons.

A thin tkinter layer over gitfurl.setup_choices (window_rows, window_choice): the rows come
from the setup data (read-only), start at the script's defaults, follow the plan (the route
decides whether the answers are used or shown for information only) and start again at the
defaults when another installer is chosen. Nothing here runs anything.
"""

from __future__ import annotations

import os
from pathlib import Path

from gitfurl import setup_choices as sc

NO_QUESTIONS = ("No questions to show: the installer is not an Inno Setup installer gitfurl "
                "can read, or its setup asks for no language, setup type, component or task.")


class ChoicesPanel:
    """Builds its widgets into `parent`; `on_change()` is called after the user changes an
    answer (the window refreshes its plan then)."""

    def __init__(self, parent, *, bg: str, fg: str, muted: str, font, small, on_change):
        import tkinter as tk

        self.tk = tk
        self.bg, self.fg, self.muted, self.font, self.small = bg, fg, muted, font, small
        self.on_change = on_change
        self.frame = tk.Frame(parent, bg=bg)
        self.note = tk.Label(self.frame, bg=bg, fg=muted, font=small, justify="left",
                             anchor="w", wraplength=640)
        self.note.pack(fill="x")
        self.body = tk.Frame(self.frame, bg=bg)
        self.body.pack(fill="x")
        self.key = None
        self.mode = sc.MODE_WIZARD
        self.loaded = None  # (Setup, Choices) or None
        self.values: dict = {}
        self.vars: dict = {}

    # -- state --

    def update(self, installer: str, mode: str) -> None:
        """Follow the chosen installer (its questions, reset to the defaults when it changes)
        and the route's mode (how the answers are used)."""
        try:
            st = os.stat(installer) if installer else None
            key = (installer, st.st_mtime_ns, st.st_size) if st else None
        except OSError:
            key = None
        if key != self.key:
            self.key = key
            got = sc.load(Path(installer)) if key else None
            self.loaded = got if got is not None and got[1].asks else None
            self.values = {}
            self._build()
        self.mode = mode
        text = sc.MODE_TEXT[mode]
        self.note.configure(text=text[:1].upper() + text[1:] + "."
                            if self.loaded else NO_QUESTIONS)

    def choice(self):
        """The answered gitfurl.inno.Choice, or None when there is nothing to answer."""
        if self.loaded is None:
            return None
        ch = self.loaded[1]
        return sc.window_choice(ch, self.values) if self.values else ch.answer()

    def picked(self):
        """A setup_choices.Picked for the plan, or None."""
        if self.loaded is None:
            return None
        setup, ch = self.loaded
        if self.mode in sc.APPLIED:
            return sc.Picked(ch, self.choice(), self.mode, "chosen in the window", setup)
        return sc.Picked(ch, ch.defaults(), self.mode, "shown", setup)

    # -- widgets --

    def _changed(self) -> None:
        self.values = {k: v.get() for k, v in self.vars.items()}
        self.frame.after_idle(self._rebuild)

    def _rebuild(self) -> None:
        if not self.frame.winfo_exists():
            return
        self._build()
        self.on_change()

    def _build(self) -> None:
        tk = self.tk
        for child in self.body.winfo_children():
            child.destroy()
        self.vars = {}
        if self.loaded is None:
            return
        ch = self.loaded[1]
        for r in sc.window_rows(ch, self.choice()):
            pad = 18 * r.indent
            if r.kind in ("heading", "text"):
                tk.Label(self.body, text=r.text, bg=self.bg,
                         fg=self.fg if r.kind == "heading" else self.muted,
                         font=self.font if r.kind == "heading" else self.small,
                         justify="left", anchor="w", wraplength=640
                         ).pack(fill="x", padx=(pad, 0), pady=(6 if r.kind == "heading" else 0, 0))
            elif r.kind == "check":
                var = tk.BooleanVar(value=bool(r.value))
                self.vars[r.key] = var
                tk.Checkbutton(self.body, text=r.text, variable=var, command=self._changed,
                               bg=self.bg, fg=self.fg, selectcolor=self.bg,
                               activebackground=self.bg, activeforeground=self.fg,
                               font=self.font, anchor="w", highlightthickness=0,
                               state="normal" if r.enabled else "disabled"
                               ).pack(fill="x", padx=(pad, 0))
            else:  # radio
                var = tk.StringVar(value=str(r.value))
                self.vars[r.key] = var
                box = tk.Frame(self.body, bg=self.bg)
                box.pack(fill="x", padx=(pad, 0))
                tk.Label(box, text=r.text, bg=self.bg, fg=self.fg, font=self.font,
                         anchor="w").pack(fill="x")
                for value, text in r.options:
                    tk.Radiobutton(box, text=text, value=value, variable=var,
                                   command=self._changed, bg=self.bg, fg=self.fg,
                                   selectcolor=self.bg, activebackground=self.bg,
                                   activeforeground=self.fg, font=self.font, anchor="w",
                                   highlightthickness=0,
                                   state="normal" if r.enabled else "disabled"
                                   ).pack(fill="x", padx=(18, 0))
