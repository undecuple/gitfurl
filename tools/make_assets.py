#!/usr/bin/env python3
"""Generate the banner, social preview and icon as SVG. Run: python3 tools/make_assets.py

Style follows the ANSI/NFO banner of undecuple/me (block letters, dithered shadow,
double frame); colours follow vim-tictoc.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from gitfurl.pixelfont import BIG, cells

BG, FRAME, FG = "#0C1021", "#465374", "#F8F8F8"
ACCENTS = ["#FF6400", "#FCB439", "#FBDE2D", "#00C1E4"]
SHADOW = "#465374"

def block_path(cs, cell, ox, oy):
    return "".join(f"M{ox + c * cell} {oy + r * cell}h{cell}v{cell}h-{cell}z" for c, r, _ in cs)


def frame(w, h):
    return (
        f'<rect width="{w}" height="{h}" fill="{BG}"/>'
        f'<rect x="3" y="3" width="{w - 6}" height="{h - 6}" fill="none" stroke="{FRAME}" stroke-width="2"/>'
        f'<rect x="8" y="8" width="{w - 16}" height="{h - 16}" fill="none" stroke="{FRAME}" stroke-width="2"/>'
    )


DEFS = (
    f'<defs><pattern id="d" width="4" height="4" patternUnits="userSpaceOnUse">'
    f'<rect width="2" height="2" fill="{SHADOW}"/><rect x="2" y="2" width="2" height="2" fill="{SHADOW}"/>'
    f"</pattern></defs>"
)


def word_svg(word, font, cell, ox, oy, shadow=1, colour_each=True):
    """Shadow layer (dither) then solid letters, one path per letter in a cycling accent."""
    cs, _ = cells(word, font)
    out = [(f'<path d="{block_path(cs, cell, ox + cell * shadow, oy + cell * shadow)}" '
           f'fill="url(#d)" shape-rendering="crispEdges"/>')]
    x, i = 0, 0
    for ch in word:
        g = font[ch]
        mine = [(c, r, ch) for (c, r, _) in cells(ch, font)[0]]
        if ch != " ":
            colour = ACCENTS[i % len(ACCENTS)] if colour_each else FG
            out.append(f'<path d="{block_path(mine, cell, ox + x * cell, oy)}" fill="{colour}" '
                       f'shape-rendering="crispEdges"/>')
            i += 1
        x += len(g[0]) + 1
    return "".join(out)


def width_cells(word, font):
    return cells(word, font)[1]


def banner():
    w, h, cell = 671, 158, 11
    n = width_cells("gitfurl", BIG)
    ox = (w - n * cell) // 2
    body = frame(w, h) + DEFS + word_svg("gitfurl", BIG, cell, ox, 46)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {w} {h}" width="{w * 2}" height="{h * 2}" role="img" aria-labelledby="t">'
            f'<title id="t">gitfurl</title>{body}</svg>\n')


def social():
    w, h = 1280, 640
    cell = 22
    n = width_cells("gitfurl", BIG)
    ox = (w - n * cell) // 2
    body = (f'<rect width="{w}" height="{h}" fill="{BG}"/>'
            f'<rect x="10" y="10" width="{w - 20}" height="{h - 20}" fill="none" stroke="{FRAME}" stroke-width="4"/>'
            f'<rect x="24" y="24" width="{w - 48}" height="{h - 48}" fill="none" stroke="{FRAME}" stroke-width="4"/>'
            + DEFS.replace('width="4" height="4"', 'width="8" height="8"')
                  .replace('width="2" height="2"', 'width="4" height="4"')
                  .replace('x="2" y="2" width="2" height="2"', 'x="4" y="4" width="4" height="4"')
            + word_svg("gitfurl", BIG, cell, ox, (h - 5 * cell) // 2, shadow=1))
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {w} {h}" width="{w}" height="{h}" role="img">'
            f"<title>gitfurl</title>{body}</svg>\n")


def icon(size=64):
    cell = 7
    cs, n = cells("g", BIG)
    ox = (size - n * cell) // 2 - 2
    oy = (size - 5 * cell) // 2 - 2
    body = (f'<rect width="{size}" height="{size}" fill="{BG}"/>'
            f'<rect x="1.5" y="1.5" width="{size - 3}" height="{size - 3}" fill="none" stroke="{FRAME}" stroke-width="2"/>'
            f'<rect x="5" y="5" width="{size - 10}" height="{size - 10}" fill="none" stroke="{FRAME}" stroke-width="1"/>'
            f'<path d="{block_path(cs, cell, ox + 3, oy + 3)}" fill="{SHADOW}" shape-rendering="crispEdges"/>'
            f'<path d="{block_path(cs, cell, ox, oy)}" fill="{ACCENTS[0]}" shape-rendering="crispEdges"/>')
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {size} {size}" width="{size * 4}" height="{size * 4}" role="img">'
            f"<title>gitfurl</title>{body}</svg>\n")


if __name__ == "__main__":
    out = Path(__file__).resolve().parent.parent / "assets"
    out.mkdir(exist_ok=True)
    (out / "banner.svg").write_text(banner())
    (out / "social-preview.svg").write_text(social())
    (out / "icon.svg").write_text(icon())
    print("wrote", *sorted(p.name for p in out.glob("*.svg")))
