#!/usr/bin/env python3
"""Generate the banner, social preview and icon as SVG. Run: python3 tools/make_assets.py

Style follows the ANSI/NFO banner of undecuple/me (block letters, dithered shadow,
double frame); colours follow vim-tictoc.
"""
import re
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


# --- README dividers: transparent strips, so they sit on GitHub's light and dark pages ------
DW, DH = 660, 28  # viewBox of every divider; shown at width 660


def _divider(body, label):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {DW} {DH}" width="{DW}" height="{DH}" role="img" aria-label="{label}">'
            f'{DEFS}{body}</svg>\n')


def _blocks(cells_xy, cell, fill):
    d = "".join(f"M{x} {y}h{cell}v{cell}h-{cell}z" for x, y in cells_xy)
    return f'<path d="{d}" fill="{fill}" shape-rendering="crispEdges"/>'


def _accent_row(cx, y, cell, gap):
    """The four accent blocks, centred on cx, each with a dithered shadow like the banner."""
    n = len(ACCENTS)
    x0 = cx - (n * cell + (n - 1) * gap) // 2
    out = [_blocks([(x0 + i * (cell + gap) + 2, y + 2) for i in range(n)], cell, "url(#d)")]
    out += [_blocks([(x0 + i * (cell + gap), y)], cell, c) for i, c in enumerate(ACCENTS)]
    return "".join(out), x0, x0 + n * cell + (n - 1) * gap


def divider_frame():
    """Opens the page: the banner's double frame as corner brackets, the accents in the middle."""
    mid = DH // 2
    row, left, right = _accent_row(DW // 2, mid - 5, 10, 6)
    s = f'stroke="{FRAME}" stroke-width="2" fill="none" shape-rendering="crispEdges"'
    body = [
        f'<path d="M3 {DH - 3}V3h22M{DW - 25} 3h22V{DH - 3}" {s}/>',
        f'<path d="M8 {DH - 8}V8h12M{DW - 20} 8h12V{DH - 8}" {s}/>',
        f'<path d="M34 {mid}H{left - 14}M{right + 14} {mid}H{DW - 34}" {s}/>',
        row,
    ]
    return _divider("".join(body), "")


def divider_rule():
    """Between sections: a dithered line with a stepped block in the four accents."""
    mid = DH // 2
    cell = 6
    steps = []
    for i, colour in enumerate(ACCENTS):
        x = DW // 2 - 2 * cell * 2 + i * cell * 2
        y = mid - cell if i % 2 == 0 else mid
        steps.append(_blocks([(x, y), (x + cell, y)], cell, colour))
    line = (f'<rect x="40" y="{mid - 1}" width="{DW // 2 - 40 - 2 * cell * 2 - 10}" height="2" '
            f'fill="url(#d)"/><rect x="{DW // 2 + 2 * cell * 2 + 10}" y="{mid - 1}" '
            f'width="{DW // 2 - 40 - 2 * cell * 2 - 10}" height="2" fill="url(#d)"/>')
    return _divider(line + "".join(steps), "")


def divider_stripe():
    """Before limits and warnings: a short band of diagonal stripes in the accents."""
    band, h, w = 300, 14, 12
    x0, y0 = (DW - band) // 2, (DH - h) // 2
    clip = (f'<clipPath id="b"><rect x="{x0}" y="{y0}" width="{band}" height="{h}"/></clipPath>')
    stripes = []
    for i in range(band // w + 2):
        x = x0 - h + i * w
        stripes.append(f'<path d="M{x} {y0 + h}l{h} -{h}h{w // 2}l-{h} {h}z" '
                       f'fill="{ACCENTS[i % len(ACCENTS)]}"/>')
    edge = (f'<rect x="{x0 - 1}" y="{y0 - 1}" width="{band + 2}" height="{h + 2}" fill="none" '
            f'stroke="{FRAME}" stroke-width="2" shape-rendering="crispEdges"/>')
    return _divider(f'<defs>{clip}</defs><g clip-path="url(#b)">{"".join(stripes)}</g>{edge}',
                    "")


# --- Project Facts label, recoloured to the palette ---------------------------------------
# docs/project-facts.svg is made as a black-on-white label (a drug facts panel). This maps
# it to the app's colours: page navy, text off-white, title and thick rules orange, headings
# amber, frames and thin rules the frame blue, bullets cyan. Running it again changes
# nothing.
AMBER, CYAN, ORANGE = ACCENTS[1], ACCENTS[3], ACCENTS[0]


def theme_facts(svg: str) -> str:
    svg = svg.replace('<rect width="420" height="673" fill="#fff"/>',
                      f'<rect width="420" height="673" rx="6" fill="{BG}"/>')

    def text(m):
        tag = m.group(0)
        if 'font-size="30"' in tag:
            return tag.replace('fill="#000"', f'fill="{ORANGE}"')
        if 'font-size="13"' in tag and 'font-weight="700"' in tag:
            return tag.replace('fill="#000"', f'fill="{AMBER}"')
        return tag.replace('fill="#000"', f'fill="{FG}"')

    svg = re.sub(r"<text [^>]*>", text, svg)

    def rect(m):
        tag = m.group(0)
        if 'stroke="#000"' in tag:
            return tag.replace('stroke="#000"', f'stroke="{FRAME}"')
        if 'width="4" height="4"' in tag:
            return tag.replace('fill="#000"', f'fill="{CYAN}"')
        if 'height="4"' in tag:
            return tag.replace('fill="#000"', f'fill="{ORANGE}"')
        return tag.replace('fill="#000"', f'fill="{FRAME}"')

    return re.sub(r"<rect [^>]*>", rect, svg)


if __name__ == "__main__":
    root = Path(__file__).resolve().parent.parent
    out = root / "assets"
    out.mkdir(exist_ok=True)
    (out / "banner.svg").write_text(banner())
    (out / "social-preview.svg").write_text(social())
    (out / "icon.svg").write_text(icon())
    docs = root / "docs" / "assets"
    docs.mkdir(exist_ok=True)
    (docs / "divider-frame.svg").write_text(divider_frame())
    (docs / "divider-rule.svg").write_text(divider_rule())
    (docs / "divider-stripe.svg").write_text(divider_stripe())
    facts = root / "docs" / "project-facts.svg"
    facts.write_text(theme_facts(facts.read_text()))
    print("wrote", *sorted(p.name for p in out.glob("*.svg")),
          *sorted(p.name for p in docs.glob("*.svg")), facts.name)
