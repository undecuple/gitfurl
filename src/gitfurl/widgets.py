"""Small tkinter widgets drawn as bricks: cards, buttons, switches, a progress bar and the
pixel wordmark. Colours follow vim-tictoc (see gitfurl.theme); bricks are square, with a lit
top edge, a shaded bottom edge and studs; an orange focus ring on everything Tab can reach;
the wordmark follows the banner in assets/. Only rectangles, ovals and text are drawn. Needs a
display; import only from the GUI."""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

from gitfurl.pixelfont import BIG, cells
from gitfurl.theme import (  # noqa: F401 - re-exported for gui.py
    ACCENTS,
    AMBER,
    BG,
    CYAN,
    DISABLED_ACCENT,
    DISABLED_FILL,
    FG,
    FOCUS_RING,
    FOCUS_RING_ON_ACCENT,
    FOCUS_WIDTH,
    HOVER,
    LINE,
    MUTED,
    ORANGE,
    PANEL,
    RADIUS,
    RED,
    SIDE,
    STUD_H,
    YELLOW,
    lit,
    mix,
    shade,
)

_FAMILIES: set[str] = set()


def pick_font(root, size=10, bold=False):
    if not _FAMILIES:
        _FAMILIES.update(tkfont.families(root))
    have = _FAMILIES
    for fam in ("Exo 2", "Adwaita Sans", "Cantarell", "Inter", "DejaVu Sans", "Noto Sans"):
        if fam in have:
            return (fam, size, "bold" if bold else "normal")
    return ("sans-serif", size, "bold" if bold else "normal")


def rounded(canvas, x0, y0, x1, y1, r, **kw):
    r = max(0, min(r, (x1 - x0) / 2, (y1 - y0) / 2))
    if r <= 0:  # a square outline, still a polygon so focus rings stay one item type
        return canvas.create_polygon([x0, y0, x1, y0, x1, y1, x0, y1], **kw)
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


def focus_ring(canvas, w, h, r, colour=FOCUS_RING, top=0):
    """Draw the keyboard focus outline (orange, like the selected card) when the canvas has focus.
    top: where the brick starts (below its studs)."""
    if getattr(canvas, "_focused", False):
        rounded(canvas, 1, top + 1, w - 1, h - 1, r, fill="", outline=colour, width=FOCUS_WIDTH)


def studs(canvas, x, y, w, colour, pitch=24, width=12):
    """Studs seen from the side along a brick's top edge (x to x + w), standing above y."""
    n = max(1, int((w - 8) // pitch))
    gap = (w - n * width) / (n + 1)
    for i in range(n):
        sx = x + gap + i * (width + gap)
        canvas.create_rectangle(sx, y - STUD_H, sx + width, y, fill=colour, outline="")
        canvas.create_rectangle(sx, y - STUD_H, sx + width, y - STUD_H + 1, fill=lit(colour),
                                outline="")
        canvas.create_rectangle(sx + width - 2, y - STUD_H, sx + width, y, fill=shade(colour),
                                outline="")


def brick(canvas, x0, y0, x1, y1, colour, with_studs=True):
    """A brick seen from the side: lit top edge, shaded bottom and right edge, studs on top
    (they need STUD_H pixels above y0)."""
    canvas.create_rectangle(x0, y0, x1, y1, fill=colour, outline="")
    canvas.create_rectangle(x0, y0, x1, y0 + 2, fill=lit(colour), outline="")
    canvas.create_rectangle(x0, y1 - 3, x1, y1, fill=shade(colour), outline="")
    canvas.create_rectangle(x1 - 2, y0, x1, y1, fill=shade(colour), outline="")
    if with_studs:
        studs(canvas, x0, y0, x1 - x0, colour)


def top_brick(canvas, x, y, s, colour):
    """A 1x1 brick seen from above, with its stud when there is room for one (s >= 5)."""
    canvas.create_rectangle(x, y, x + s, y + s, fill=shade(colour), outline="")
    canvas.create_rectangle(x, y, x + s - 1, y + s - 1, fill=lit(colour), outline="")
    canvas.create_rectangle(x + 1, y + 1, x + s - 1, y + s - 1, fill=colour, outline="")
    if s >= 5:
        r, c = s * 0.29, s / 2
        canvas.create_oval(x + c - r + 0.6, y + c - r + 0.6, x + c + r + 0.6, y + c + r + 0.6,
                           fill=shade(colour), outline="")
        canvas.create_oval(x + c - r, y + c - r, x + c + r, y + c + r, fill=colour,
                           outline=lit(colour))


class RoundCard(tk.Canvas):
    """A panel drawn as a plate: square, a row of studs along the top, a lit top edge and a
    shaded bottom edge. Put children in .body. (The name is older than the brick look.)"""

    def __init__(self, master, fill=PANEL, outline=LINE, radius=RADIUS, pad=14, outer=BG,
                 activate=None):
        super().__init__(master, bg=outer, highlightthickness=0, bd=0, height=40)
        self.fill, self.outline, self.radius, self.pad = fill, outline, radius, pad
        self.body = tk.Frame(self, bg=fill)
        self._win = self.create_window(pad, pad + STUD_H, window=self.body, anchor="nw")
        self.bind("<Configure>", self._draw)
        self.body.bind("<Configure>", self._fit)
        if activate is not None:  # a card you can open: Tab reaches it, Enter or space opens it
            make_focusable(self, activate, self._draw)

    def _fit(self, _e=None):
        self.configure(height=self.body.winfo_reqheight() + 2 * self.pad + STUD_H)

    def _draw(self, _e=None):
        self.delete("bg")
        w, h = self.winfo_width(), self.winfo_height()
        focused = getattr(self, "_focused", False)
        top = STUD_H
        ids = [self.create_rectangle(1, top, w - 1, h - 1, fill=self.fill, outline="",
                                     tags="bg")]
        ids.append(self.create_rectangle(1, top, w - 1, top + 1, fill=lit(self.fill), outline="",
                                         tags="bg"))
        ids.append(self.create_rectangle(1, h - 4, w - 1, h - 1, fill=shade(self.fill),
                                         outline="", tags="bg"))
        before = set(self.find_all())
        studs(self, 1, top, w - 2, self.fill, pitch=28, width=14)
        for i in set(self.find_all()) - before:
            self.addtag_withtag("bg", i)
        rounded(self, 1, top, w - 1, h - 1, self.radius, fill="",
                outline=FOCUS_RING if focused else self.outline,
                width=FOCUS_WIDTH if focused else 1, tags="bg")
        self.tag_lower("bg")
        self.itemconfigure(self._win, width=max(10, w - 2 * self.pad))


class PillButton(tk.Canvas):
    """style: accent (orange pill), plain (rounded), ghost (text only)."""

    def __init__(self, master, text, command, style="plain", font=None, outer=BG, pad=(16, 8)):
        self._font = font or ("sans-serif", 10)
        self.text, self.command, self.style, self._outer = text, command, style, outer
        self._pad = pad
        self._top = 0 if style == "ghost" else STUD_H  # room for the studs
        w, h = self._size()
        super().__init__(master, width=w, height=h, bg=outer, highlightthickness=0, bd=0,
                         cursor="hand2")
        self._state, self._hover = "normal", False
        self.bind("<Enter>", lambda _e: self._set_hover(True))
        self.bind("<Leave>", lambda _e: self._set_hover(False))
        self.bind("<Button-1>", self._click)
        self.bind("<Configure>", lambda _e: self._draw())
        make_focusable(self, self._activate, self._draw)
        self._draw()

    def _size(self):
        f = self._font if isinstance(self._font, tkfont.Font) else tkfont.Font(font=self._font)
        return (f.measure(self.text) + 2 * self._pad[0],
                f.metrics("linespace") + 2 * self._pad[1] + self._top)

    def refit(self):
        """Measure the text again (after the text size changed) and redraw."""
        w, h = self._size()
        self.configure(width=w, height=h)
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
        top = self._top
        dis = self._state == "disabled"
        if self.style == "accent":
            fill = DISABLED_ACCENT if dis else (AMBER if self._hover else ORANGE)
            fg = MUTED if dis else BG
            brick(self, 0, top, w, h, fill)
        elif self.style == "plain":
            fill = DISABLED_FILL if dis else (HOVER if self._hover else LINE)
            fg = MUTED if dis else FG
            brick(self, 0, top, w, h, fill)
        else:
            fg = MUTED if dis else FG
            if self._hover and not dis:
                brick(self, 0, top, w, h, HOVER, with_studs=False)
        # centred on the brick's face, above its shaded bottom edge
        self.create_text(w / 2, top + (h - top - 2) / 2, text=self.text, fill=fg, font=self._font)
        # white on the orange brick (an orange ring would vanish there), orange elsewhere
        focus_ring(self, w, h, RADIUS,
                   FOCUS_RING_ON_ACCENT if self.style == "accent" and not dis else FOCUS_RING,
                   top=top)


class Switch(tk.Canvas):
    def __init__(self, master, variable: tk.BooleanVar, outer=PANEL, command=None):
        super().__init__(master, width=46, height=26, bg=outer, highlightthickness=0, bd=0,
                         cursor="hand2")
        self.var, self.command = variable, command
        self.enabled = True  # False: clicks and keys do nothing (the caller says why)
        self.bind("<Button-1>", lambda _e: self._toggle())
        variable.trace_add("write", lambda *_: self._draw())
        make_focusable(self, self._toggle, self._draw)
        self._draw()

    def _toggle(self):
        if not self.enabled:
            return
        self.var.set(not self.var.get())
        if self.command:
            self.command()

    def set_enabled(self, enabled: bool) -> None:
        self.enabled = enabled
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    def _draw(self):
        """A 2x1 plate; a 1x1 brick sits at its left end when off and its right end when on."""
        self.delete("all")
        on = self.var.get()
        base = mix(PANEL, LINE, 0.45)
        self.create_rectangle(0, 0, 46, 26, fill=base, outline="")
        self.create_rectangle(0, 0, 46, 2, fill=shade(PANEL), outline="")
        self.create_rectangle(0, 25, 46, 26, fill=LINE, outline="")
        for cx in (12, 34):
            self.create_oval(cx - 4, 9, cx + 4, 17, fill=shade(base), outline="")
        knob = ORANGE if on else (FG if self.enabled else MUTED)
        top_brick(self, 24 if on else 2, 3, 20, knob)
        focus_ring(self, 46, 26, RADIUS)


class Bar(tk.Canvas):
    """Progress as a row of 1x1 bricks: filled from the left, empty sockets after them."""

    CELL = 16

    def __init__(self, master, outer=PANEL):
        super().__init__(master, height=self.CELL, bg=outer, highlightthickness=0, bd=0)
        self.value = 0.0
        self.bind("<Configure>", lambda _e: self.set(self.value))

    def set(self, pct: float):
        self.value = max(0.0, min(100.0, pct))
        self.delete("all")
        s = self.CELL
        n = max(1, self.winfo_width() // s)
        full = round(n * self.value / 100)
        if self.value > 0:
            full = max(1, full)
        socket = shade(PANEL)
        for i in range(n):
            x = i * s
            if i < full:
                top_brick(self, x, 0, s, ORANGE)
            else:
                self.create_rectangle(x + 1, 1, x + s - 1, s - 1, fill=socket, outline="")
                r = s * 0.29
                self.create_oval(x + s / 2 - r, s / 2 - r, x + s / 2 + r, s / 2 + r,
                                 fill=mix(PANEL, BG, 0.5), outline="")


class Wordmark(tk.Canvas):
    """The banner's block letters as bricks, with the dithered shadow, drawn on a canvas."""

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
                top_brick(self, (x + c) * cell, r * cell, cell, colour)
            x += len(BIG[ch][0]) + 1


class Tile(tk.Canvas):
    """A small block-letter tile, like a game cover in rc's library."""

    def __init__(self, master, letter, colour, size=44, outer=PANEL):
        super().__init__(master, width=size, height=size, bg=outer, highlightthickness=0, bd=0)
        self.create_rectangle(0, 0, size - 1, size - 1, fill=BG, outline=LINE)
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
                    top_brick(self, ox + c * cell, oy + r * cell, cell, colour)
