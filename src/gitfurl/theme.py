"""Colours, spacing, type sizes, the corner radius and the contrast maths. No tkinter here,
so it can be tested without a display. gitfurl.widgets and gitfurl.gui draw with these."""

from __future__ import annotations

# vim-tictoc colours (same as the assets)
BG, PANEL, SIDE, LINE, FG = "#0C1021", "#1a1f36", "#10152a", "#465374", "#F8F8F8"
ORANGE, AMBER, YELLOW, CYAN, MUTED, RED = (
    "#FF6400", "#FCB439", "#FBDE2D", "#00C1E4", "#AEAEAE", "#E06C5A")
HOVER, ACCENTS = "#5a6a92", [ORANGE, AMBER, YELLOW, CYAN]
DISABLED_FILL = "#2b3350"       # a disabled plain button
DISABLED_ACCENT = "#5a3a22"     # a disabled accent button

# One spacing scale (pixels), one type scale (points), one corner radius (0: bricks are square).
SPACE = {"xs": 4, "sm": 8, "md": 12, "lg": 16, "xl": 24}
TYPE = {"caption": 9, "body": 10, "lead": 12, "title": 22}
RADIUS = 0
STUD_H = 4  # how far a brick's studs stand above its top edge (pixels)
FOCUS_WIDTH = 2

# Every text colour on every background it is drawn on: (name, text, background).
# Body text must reach 4.5:1 (WCAG AA); tests/test_gitfurl.py checks the list.
TEXT_PAIRS = [
    ("body on page", FG, BG), ("body on card", FG, PANEL), ("body on sidebar", FG, SIDE),
    ("muted on page", MUTED, BG), ("muted on card", MUTED, PANEL),
    ("muted on sidebar", MUTED, SIDE),
    ("accent button label", BG, ORANGE), ("accent button label hover", BG, AMBER),
    ("plain button label", FG, LINE), ("plain button label hover", FG, HOVER),
    ("ghost button label hover", FG, HOVER),
    ("disabled plain label", MUTED, DISABLED_FILL),
    ("disabled accent label", MUTED, DISABLED_ACCENT),
    ("ok on card", CYAN, PANEL), ("ok on page", CYAN, BG), ("ok on footer", CYAN, SIDE),
    ("warning on card", AMBER, PANEL), ("warning on page", AMBER, BG),
    ("warning on footer", AMBER, SIDE),
    ("notice on page", YELLOW, BG),
    ("error on card", RED, PANEL), ("error on page", RED, BG), ("error on footer", RED, SIDE),
    ("selected nav label", FG, LINE),
]
# Non-text parts that must stay visible (3:1, WCAG 1.4.11): the focus ring on the surfaces
# a control can sit on.
FOCUS_RING = ORANGE
FOCUS_RING_ON_ACCENT = FG
FOCUS_SURFACES = [BG, PANEL, SIDE]


def _channel(c: int) -> float:
    s = c / 255
    return s / 12.92 if s <= 0.03928 else ((s + 0.055) / 1.055) ** 2.4


def luminance(colour: str) -> float:
    """Relative luminance of '#rrggbb' (WCAG 2.x)."""
    h = colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * _channel(r) + 0.7152 * _channel(g) + 0.0722 * _channel(b)


def contrast_ratio(a: str, b: str) -> float:
    """WCAG contrast ratio between two '#rrggbb' colours, 1.0 to 21.0."""
    hi, lo = sorted((luminance(a), luminance(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def mix(colour: str, other: str, t: float) -> str:
    """'#rrggbb' moved a fraction t (0 to 1) of the way towards other."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def lit(colour: str) -> str:
    """The lit edge of a brick of this colour."""
    return mix(colour, "#FFFFFF", 0.3)


def shade(colour: str) -> str:
    """The shaded edge of a brick of this colour."""
    return mix(colour, "#000000", 0.4)
