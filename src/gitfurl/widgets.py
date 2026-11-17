"""Small tkinter widgets: rounded cards, buttons, switches, a progress bar and the pixel
wordmark. Colours follow vim-tictoc, shapes follow libadwaita (12 px cards, pill buttons),
the wordmark follows the banner in assets/. Needs a display; import only from the GUI."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from gitfurl.pixelfont import BIG, cells

BG, PANEL, LINE, FG = "#0C1021", "#1a1f36", "#465374", "#F8F8F8"
ORANGE, AMBER, YELLOW, CYAN, MUTED, RED = "#FF6400", "#FCB439", "#FBDE2D", "#00C1E4", "#AEAEAE", "#E06C5A"
HOVER, ACCENTS = "#5a6a92", [ORANGE, AMBER, YELLOW, CYAN]


_FAMILIES: set[str] = set()


def pick_font(root, size=10, bold=False):
    if not _FAMILIES:
        _FAMILIES.update(tkfont.families(root))
    have = _FAMILIES
    for fam in ("Adwaita Sans", "Cantarell", "Inter", "DejaVu Sans", "Noto Sans"):
        if fam in have:
            return (fam, size, "bold" if bold else "normal")
    return ("sans-serif", size, "bold" if bold else "normal")


def rounded(canvas, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    # Each straight-edge end is listed twice so the spline hugs the full radius.
    pts = [x0 + r, y0, x0 + r, y0, x1 - r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y0 + r,
           x1, y1 - r, x1, y1 - r, x1, y1, x1 - r, y1, x1 - r, y1, x0 + r, y1, x0 + r, y1,
           x0, y1, x0, y1 - r, x0, y1 - r, x0, y0 + r, x0, y0 + r, x0, y0]
    return canvas.create_polygon(pts, smooth=True, **kw)


def make_focusable(widget, activate, redraw):
    """Tab reaches the widget, a ring shows, and Return or space activates it."""
    widget.configure(takefocus=1)
    widget._focused = False

    def focus(v):
        widget._focused = v
        redraw()

    widget.bind("<FocusIn>", lambda _e: focus(True), add="+")
    widget.bind("<FocusOut>", lambda _e: focus(False), add="+")
    widget.bind("<Return>", lambda _e: activate(), add="+")
    widget.bind("<space>", lambda _e: activate(), add="+")
    widget.bind("<Button-1>", lambda _e: widget.focus_set(), add="+")


def focus_ring(canvas, w, h, r):
    if getattr(canvas, "_focused", False):
        rounded(canvas, 1, 1, w - 1, h - 1, r, fill="", outline=YELLOW, width=2)


class RoundCard(tk.Canvas):
    """A rounded panel. Put children in .body."""

    def __init__(self, master, fill=PANEL, outline=LINE, radius=12, pad=14, outer=BG):
        super().__init__(master, bg=outer, highlightthickness=0, bd=0, height=40)
        self.fill, self.outline, self.radius, self.pad = fill, outline, radius, pad
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad, window=self.body, anchor="nw")
        self.bind("<Configure>", self._draw)
        self.body.bind("<Configure>", self._fit)

    def _fit(self, _e=None):
        self.configure(height=self.body.winfo_reqheight() + 2 * self.pad)

    def _draw(self, _e=None):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        rounded(self, 1, 1, w - 1, h - 1, self.radius, fill=self.fill, outline=self.outline,
                tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(10, w - 2 * self.pad))


class PillButton(tk.Canvas):
    """style: accent (orange pill), plain (rounded), ghost (text only)."""

    def __init__(self, master, text, command, style="plain", font=None, outer=BG, pad=(16, 8)):
        self._font = font or ("sans-serif", 10)
        f = tkfont.Font(font=self._font)
        self.text, self.command, self.style, self._outer = text, command, style, outer
        w, h = f.measure(text) + 2 * pad[0], f.metrics("linespace") + 2 * pad[1]
        super().__init__(master, width=w, height=h, bg=outer, highlightthickness=0, bd=0,
                         cursor="hand2")
        self._state, self._hover = "normal", False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda _e: self._draw())
        make_focusable(self, self._activate, self._draw)
        self._draw()

    def _activate(self):
        if self._state == "normal" and self.command:
            self.command()

    def _set_hover(self, v):
        self._hover = v
        self._draw()

    def _click(self, _e):
        if self._state == "normal" and self.command:
            self.command()

    def state(self, spec):  # ttk-style: ["disabled"] / ["!disabled"]
        if "disabled" in spec:
            self._state = "disabled"
        elif "!disabled" in spec:
            self._state = "normal"
        self.configure(cursor="arrow" if self._state == "disabled" else "hand2",
                       takefocus=0 if self._state == "disabled" else 1)
        self._draw()

    def set_text(self, text):
        self.text = text
        self._draw()

    def _draw(self):
        self.delete("all")
        w, h = self.winfo_reqwidth(), self.winfo_reqheight()
        dis = self._state == "disabled"
        if self.style == "accent":
            fill = "#5a3a22" if dis else (AMBER if self._hover else ORANGE)
            fg = MUTED if dis else BG
            rounded(self, 0, 0, w, h, h / 2, fill=fill, outline=fill)
        elif self.style == "plain":
            fill = HOVER if self._hover and not dis else LINE
            fg = MUTED if dis else FG
            rounded(self, 0, 0, w, h, 8, fill=fill, outline=fill)
        else:
            fg = MUTED if dis else (YELLOW if self._hover else FG)
            if self._hover and not dis:
                rounded(self, 0, 0, w, h, 8, fill=HOVER, outline=HOVER)
        self.create_text(w / 2, h / 2, text=self.text, fill=fg, font=self._font)
        focus_ring(self, w, h, h / 2 if self.style == "accent" else 8)


class Switch(tk.Canvas):
    def __init__(self, master, variable: tk.BooleanVar, outer=PANEL, command=None):
        super().__init__(master, width=46, height=26, bg=outer, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.var, self.command = variable, command
        self.bind("<Button-1>", lambda _e: self._toggle())
        variable.trace_add("write", lambda *_: self._draw())
        make_focusable(self, self._toggle, self._draw)
        self._draw()

    def _toggle(self):
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def _draw(self):
        self.delete("all")
        on = self.var.get()
        rounded(self, 1, 1, 45, 25, 12, fill=ORANGE if on else LINE, outline=ORANGE if on else LINE)
        x = 33 if on else 13
        self.create_oval(x - 9, 4, x + 9, 22, fill=BG if on else FG, outline="")
        focus_ring(self, 46, 26, 12)


class Bar(tk.Canvas):
    def __init__(self, master, outer=PANEL):
        super().__init__(master, height=12, bg=outer, highlightthickness=0, bd=0)
        self.value = 0.0
        self.bind("<Configure>", lambda _e: self.set(self.value))

    def set(self, pct: float):
        self.value = max(0.0, min(100.0, pct))
        self.delete("all")
        w = self.winfo_width()
        rounded(self, 0, 0, w, 12, 6, fill=BG, outline=LINE)
        if self.value > 0:
            rounded(self, 0, 0, max(12, w * self.value / 100), 12, 6, fill=ORANGE, outline=ORANGE)


class Wordmark(tk.Canvas):
    """The banner's block letters with the dithered shadow, drawn on a canvas."""

    def __init__(self, master, word="gitfurl", cell=5, outer=BG):
        cs, n = cells(word, BIG)
        super().__init__(master, width=n * cell + 2 * cell, height=5 * cell + 2 * cell, bg=outer,
                         highlightthickness=0, bd=0)
        for c, r, _ in cs:  # shadow: stippled to look dithered
            self.create_rectangle(c * cell + cell, r * cell + cell, c * cell + 2 * cell,
                                  r * cell + 2 * cell, fill=LINE, outline="", stipple="gray50")
        x = 0
        for i, ch in enumerate(word):
            colour = ACCENTS[i % len(ACCENTS)]
            for c, r, _ in cells(ch, BIG)[0]:
                self.create_rectangle((x + c) * cell, r * cell, (x + c + 1) * cell,
                                      (r + 1) * cell, fill=colour, outline="")
            x += len(BIG[ch][0]) + 1


class Tile(tk.Canvas):
    """A small block-letter tile, like a game cover in rc's library."""

    def __init__(self, master, letter, colour, size=44, outer=PANEL):
        super().__init__(master, width=size, height=size, bg=outer, highlightthickness=0, bd=0)
        rounded(self, 0, 0, size, size, 10, fill=BG, outline=LINE)
        g = BIG.get(letter.lower())
        if g is None:  # the block font has only the letters of the wordmark
            self.create_text(size / 2, size / 2, text=(letter or "?").upper(), fill=colour,
                             font=("sans-serif", 16, "bold"))
            return
        cell = 4
        gw, gh = len(g[0]) * cell, len(g) * cell
        ox, oy = (size - gw) / 2, (size - gh) / 2
        for r, row in enumerate(g):
            for c, v in enumerate(row):
                if v == "X":
                    self.create_rectangle(ox + c * cell, oy + r * cell, ox + (c + 1) * cell,
                                          oy + (r + 1) * cell, fill=colour, outline="")
