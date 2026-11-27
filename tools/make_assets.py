#!/usr/bin/env python3
"""Generate the banner, social preview and icon as SVG. Run: python3 tools/make_assets.py
With --png it also renders the PNGs and favicon.ico (needs resvg and ImageMagick on PATH).

The banner follows the ANSI/NFO banner of undecuple/me (block letters as bricks, dithered
shadow, double frame); the icon and social preview lay the letters in smooth 2x1 tiles on a
studded plate. Colours follow vim-tictoc.
"""
import math
import re
import shutil
import subprocess
import sys
import tempfile
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


def width_cells(word, font):
    return cells(word, font)[1]


LOOP = 15  # seconds between the banner's builds


def _mix(colour, other, t):
    """colour moved a fraction t of the way towards other (both #RRGGBB)."""
    a = [int(colour[i:i + 2], 16) for i in (1, 3, 5)]
    b = [int(other[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02X}" for x, y in zip(a, b))


def _brick(id_, colour, s):
    """One brick seen from above: shaded and lit edges, the face, and one stud."""
    hi, lo = _mix(colour, "#FFFFFF", 0.35), _mix(colour, "#000000", 0.35)
    r, c = s * 0.29, s / 2
    return (f'<g id="{id_}"><rect width="{s}" height="{s}" fill="{lo}"/>'
            f'<rect width="{s - 1}" height="{s - 1}" fill="{hi}"/>'
            f'<rect x="1" y="1" width="{s - 2}" height="{s - 2}" fill="{colour}"/>'
            f'<circle cx="{c + 0.6}" cy="{c + 0.6}" r="{r}" fill="{lo}"/>'
            f'<circle cx="{c}" cy="{c}" r="{r}" fill="{colour}" stroke="{hi}" stroke-width=".7"/></g>')


def banner():
    """The word as bricks that drop into place (CSS only, no script), letter by letter, bottom
    row first; the dithered shadow fades in last. It builds again every LOOP seconds. With
    reduced motion it is still."""
    w, h, cell = 671, 158, 11
    word = "gitfurl"
    n = width_cells(word, BIG)
    ox, oy = (w - n * cell) // 2, 46
    rows = len(BIG[word[0]])
    bricks = "".join(_brick(f"k{i}", c, cell) for i, c in enumerate(ACCENTS))
    cs, _ = cells(word, BIG)
    shadow = (f'<path class="s" d="{block_path(cs, cell, ox + cell, oy + cell)}" '
              f'fill="url(#d)" shape-rendering="crispEdges"/>')
    uses, x, i, t = [], 0, 0, 0.0
    for ch in word:
        if ch != " ":
            mine = sorted(cells(ch, BIG)[0], key=lambda p: (-p[1], p[0]))
            for c, r, _ in mine:
                delay = t + (rows - 1 - r) * 0.07 + c * 0.025
                uses.append(f'<use href="#k{i % len(ACCENTS)}" x="{ox + (x + c) * cell}" '
                            f'y="{oy + r * cell}" class="b" style="animation-delay:{delay:.3f}s"/>')
            t += 0.32
            i += 1
        x += len(BIG[ch][0]) + 1
    # One 15 s cycle per brick: drop in (0.5 s, with a bounce), stay, fade out near the end,
    # start again. Each brick keeps its own delay, so every cycle builds in the same order.
    style = (f"<style>.b{{animation:drop {LOOP}s linear infinite both}}"
             f".s{{animation:fade {LOOP}s linear {t + 0.5:.2f}s infinite both}}"
             "@keyframes drop{0%{transform:translateY(-70px);opacity:0;"
             "animation-timing-function:cubic-bezier(.3,1.45,.55,1)}"
             "3.33%{transform:none;opacity:1}88%{transform:none;opacity:1}"
             "92%,100%{transform:translateY(0);opacity:0}}"
             "@keyframes fade{0%{opacity:0}3.33%,72%{opacity:1}76%,100%{opacity:0}}"
             "@media (prefers-reduced-motion:reduce){.b,.s{animation:none}}</style>")
    body = frame(w, h) + DEFS.replace("</defs>", bricks + "</defs>") + style + shadow + "".join(uses)
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {w} {h}" width="{w * 2}" height="{h * 2}" role="img" aria-labelledby="t">'
            f'<title id="t">gitfurl</title>{body}</svg>\n')


# --- Social preview and icon: smooth 2x1 tiles laid on a studded plate ---------------------
# A tiny 3D drawing: u runs right, v towards the viewer, z up; one unit is one stud. A View
# turns that into SVG coordinates, tilted by `tilt` degrees from straight down and, with
# `dist`, in perspective. Faces are drawn back to front, so no z-buffer is needed.
PLATE_SOCIAL, PLATE_ICON = _mix(FRAME, BG, 0.25), "#1A1F36"
STUD_R, STUD_H, PLATE_H, TILE_H, TILE_GAP = 0.3, 0.18, 0.35, 0.3, 0.05
TAGLINE = "Run Windows installers you already have, in their own Wine prefix"
FONT = "'Exo 2', 'Adwaita Sans', 'DejaVu Sans', sans-serif"


class View:
    def __init__(self, tilt, dist, centre, scale, origin, clip=None):
        self.t, self.d = math.radians(tilt), dist
        self.clip = clip  # (width, height): studs outside it are left out
        self.uc, self.vc = centre
        self.k = scale
        self.ox, self.oy = origin

    def p(self, u, v, z):
        y = (v - self.vc) * math.cos(self.t) - z * math.sin(self.t)
        s = 1.0
        if self.d:
            s = self.d / (self.d - (v - self.vc) * math.sin(self.t) - z * math.cos(self.t))
        return self.ox + (u - self.uc) * s * self.k, self.oy + y * s * self.k


def _pts(ps):
    return " ".join(f"{x:.1f},{y:.1f}" for x, y in ps)


def _area(ps):
    return sum(ps[i][0] * ps[i - 1][1] - ps[i - 1][0] * ps[i][1] for i in range(len(ps))) / 2


def _stud(view, u, v, colour, n=16):
    """A plate stud on cell (u, v): its side, then its top with a lit rim. The projected
    circles are drawn as ellipses (close enough at these angles, and much smaller)."""
    def ellipse(z):
        ps = [view.p(u + 0.5 + STUD_R * math.cos(2 * math.pi * i / n),
                     v + 0.5 + STUD_R * math.sin(2 * math.pi * i / n), z) for i in range(n)]
        xs, ys = [x for x, _ in ps], [y for _, y in ps]
        return ((max(xs) + min(xs)) / 2, (max(ys) + min(ys)) / 2,
                (max(xs) - min(xs)) / 2, (max(ys) - min(ys)) / 2)
    bx, by, brx, bry = ellipse(0)
    tx, ty, trx, try_ = ellipse(STUD_H)
    if view.clip and not (-brx < bx < view.clip[0] + brx and -bry - (by - ty) < by < view.clip[1] + bry):
        return ""  # outside the picture
    side = _mix(colour, "#000000", 0.32)
    return (f'<ellipse cx="{bx:.1f}" cy="{by:.1f}" rx="{brx:.1f}" ry="{bry:.1f}" fill="{side}"/>'
            f'<path d="M{bx - brx:.1f} {by:.1f}V{ty:.1f}h{2 * trx:.1f}V{by:.1f}z" fill="{side}"/>'
            f'<ellipse cx="{tx:.1f}" cy="{ty:.1f}" rx="{trx:.1f}" ry="{try_:.1f}" '
            f'fill="{_mix(colour, "#FFFFFF", 0.06)}" stroke="{_mix(colour, "#FFFFFF", 0.35)}" '
            f'stroke-width="{view.k * 0.035:.2f}"/>')


def _box(view, u0, v0, u1, v1, z0, z1, colour, edge=True):
    """A box: whichever of its sides face the camera, then its top, with lit far and left
    edges when edge is set."""
    faces = [  # corners in the order that makes a face seen from outside wind one way
        ([(u0, v0, z1), (u0, v1, z1), (u0, v1, z0), (u0, v0, z0)], 0.18),  # left
        ([(u1, v1, z1), (u1, v0, z1), (u1, v0, z0), (u1, v1, z0)], 0.5),   # right
        ([(u0, v1, z1), (u1, v1, z1), (u1, v1, z0), (u0, v1, z0)], 0.38),  # front
        ([(u0, v0, z1), (u1, v0, z1), (u1, v1, z1), (u0, v1, z1)], 0),     # top
    ]
    out = []
    for corners, dark in faces:
        ps = [view.p(*c) for c in corners]
        if _area(ps) < -1e-6:
            out.append(f'<polygon points="{_pts(ps)}" fill="{_mix(colour, "#000000", dark)}"/>')
    if edge:
        ps = [view.p(u0, v1, z1), view.p(u0, v0, z1), view.p(u1, v0, z1)]
        out.append(f'<polyline points="{_pts(ps)}" fill="none" '
                   f'stroke="{_mix(colour, "#FFFFFF", 0.4)}" stroke-width="{view.k * 0.05:.2f}"/>')
    return "".join(out)


def tile_cells(cellset):
    """Cover the cells with 2x1 tiles, either way round, with as few 1x1s as possible; on a
    tie, prefer tiles that run along the stroke. Returns [(u0, v0, u1, v1)]."""
    cellset = set(cellset)
    order = sorted(cellset, key=lambda c: (c[1], c[0]))

    def run(c, du, dv):
        n = 0
        for sign in (1, -1):
            u, v = c if sign == 1 else (c[0] - du, c[1] - dv)
            while (u, v) in cellset:
                n, u, v = n + 1, u + sign * du, v + sign * dv
        return n
    best = []

    def go(i, used, acc, singles, score):
        if best and singles > best[0][0]:
            return
        while i < len(order) and order[i] in used:
            i += 1
        if i == len(order):
            if not best or (singles, -score) < best[0]:
                best[:] = [(singles, -score), acc]
            return
        u, v = order[i]
        opts = [((u, v, u + 1 + du, v + 1 + dv), (u + du, v + dv), run((u, v), du, dv))
                for du, dv in ((1, 0), (0, 1))
                if (u + du, v + dv) in cellset and (u + du, v + dv) not in used]
        for tile, other, sc in sorted(opts, key=lambda o: -o[2]):
            go(i + 1, used | {(u, v), other}, acc + [tile], singles, score + sc)
        go(i + 1, used | {(u, v)}, acc + [(u, v, u + 1, v + 1)], singles + 1, score)
    go(0, frozenset(), [], 0, 0)
    return best[1]


def word_tiles(word, u0=0, v0=0, colours=ACCENTS):
    """The word's block letters as 2x1 tiles, one accent per letter: [(u0, v0, u1, v1, colour)]."""
    out, x, i = [], 0, 0
    for ch in word:
        cs = [(u0 + x + c, v0 + r) for c, r, _ in cells(ch, BIG)[0]]
        if cs:
            out += [(*t, colours[i % len(colours)]) for t in tile_cells(cs)]
            i += 1
        x += len(BIG[ch][0]) + 1
    return out


def plate_scene(view, plate, colour, tiles):
    """A studded plate with tiles laid on it; the studs show wherever no tile covers them."""
    u0, v0, u1, v1 = plate
    out = [_box(view, u0, v0, u1, v1, -PLATE_H, 0, colour, edge=False)]
    covered = {(a, b) for a0, b0, a1, b1, _ in tiles for a in range(a0, a1) for b in range(b0, b1)}
    items = [((v, -abs(u + 0.5 - view.uc)), _stud(view, u, v, colour))
             for u in range(u0, u1) for v in range(v0, v1) if (u, v) not in covered]
    g = TILE_GAP
    items += [(((b0 + b1) / 2 - 0.5, -abs((a0 + a1) / 2 - view.uc)),
               _box(view, a0 + g, b0 + g, a1 - g, b1 - g, 0, TILE_H, c))
              for a0, b0, a1, b1, c in tiles]
    return out + [s for _, s in sorted(items, key=lambda i: i[0])]


def _svg(w, h, body, width=None, height=None):
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<svg xmlns="http://www.w3.org/2000/svg" '
            f'viewBox="0 0 {w} {h}" width="{width or w}" height="{height or h}" role="img" '
            f'aria-labelledby="t"><title id="t">gitfurl</title>'
            f'<rect width="{w}" height="{h}" fill="{BG}"/>{body}</svg>\n')


def social():
    """1280x640: a studded plate in perspective, edge to edge, with the word in tiles and the
    tagline on a row of dark 1x8 tiles. Everything that matters sits inside GitHub's middle
    1200x600."""
    w, h = 1280, 640
    word = word_tiles("gitfurl")  # 39 x 5 studs
    mid = width_cells("gitfurl", BIG) / 2
    view = View(40, 34, (mid, 4.5), 27, (w / 2, h / 2 + 2), clip=(w, h))
    strip = [(u, v, u + 8, v + 1, BG) for v in (7, 8) for u in range(-1, 39, 8)]
    body = plate_scene(view, (-16, -30, 55, 22), PLATE_SOCIAL, word + strip)
    x, y = view.p(mid, 8, TILE_H)
    body.append(f'<text x="{x:.1f}" y="{y:.1f}" text-anchor="middle" dominant-baseline="central" '
                f'font-family="{FONT}" font-weight="600" font-size="30" fill="{FG}" '
                f'stroke="{BG}" stroke-width="5" stroke-linejoin="round" paint-order="stroke">'
                f"{TAGLINE}</text>")
    return _svg(w, h, "".join(body))


def icon():
    """The app icon, 256 units: a G of orange 2x1 tiles on a 7x7 studded plate, tilted 30°."""
    s = 256
    view = View(30, None, (3.5, 3.5), s / 7.2, (s / 2, s / 2 + 4))
    g = word_tiles("g", 1, 1, colours=[ACCENTS[0]])
    return _svg(s, s, "".join(plate_scene(view, (0, 0, 7, 7), PLATE_ICON, g)))


def icon_small(px):
    """The icon redrawn on the pixel grid for 48, 32 and 16 pixels: the plate's colour, then the
    G. At 48 the tiles show (lit and shaded edges, 1 px gaps); below that it is a solid G."""
    cell = px // 5 if px < 48 else 8
    off = (px - 5 * cell) / 2
    out = [f'<rect width="{px}" height="{px}" fill="{PLATE_ICON}"/>']
    o = ACCENTS[0]
    if px < 48:
        cs, _ = cells("g", BIG)
        out.append(f'<path d="{block_path(cs, cell, off, off)}" fill="{o}"/>')
    else:
        for a0, b0, a1, b1, _c in word_tiles("g", colours=[o]):
            x, y, tw, th = off + a0 * cell, off + b0 * cell, (a1 - a0) * cell - 1, (b1 - b0) * cell - 1
            out.append(f'<rect x="{x}" y="{y}" width="{tw}" height="{th}" '
                       f'fill="{_mix(o, "#000000", 0.4)}"/>'
                       f'<rect x="{x}" y="{y}" width="{tw - 1}" height="{th - 1}" '
                       f'fill="{_mix(o, "#FFFFFF", 0.35)}"/>'
                       f'<rect x="{x + 1}" y="{y + 1}" width="{tw - 2}" height="{th - 2}" fill="{o}"/>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {px} {px}" width="{px}" '
            f'height="{px}" shape-rendering="crispEdges">{"".join(out)}</svg>\n')


def render_pngs(root):
    """--png: the PNGs and favicon.ico from the SVGs, with resvg and ImageMagick's magick from
    PATH. 64 and up come from icon.svg; 48, 32 and 16 from icon_small()."""
    assets = root / "assets"
    fonts = []
    exo = shutil.which("fc-match") and subprocess.run(
        ["fc-match", "-f", "%{file}", "Exo 2"], capture_output=True, text=True).stdout
    if exo and "Exo2" in exo:
        fonts = ["--use-font-file", exo]

    def resvg(src, dst, size=None):
        subprocess.run(["resvg", *fonts, *(["-w", str(size)] if size else []), str(src),
                        str(dst)], check=True)
    for size in (64, 128, 256, 512):
        resvg(assets / "icon.svg", assets / f"icon-{size}.png", size)
    shutil.copyfile(assets / "icon-64.png", root / "src" / "gitfurl" / "icon.png")
    resvg(assets / "social-preview.svg", assets / "social-preview.png")
    with tempfile.TemporaryDirectory() as tmp:
        small = []
        for px in (16, 32, 48):
            svg = Path(tmp) / f"icon-{px}.svg"
            svg.write_text(icon_small(px))
            resvg(svg, svg.with_suffix(".png"))
            small.append(str(svg.with_suffix(".png")))
        subprocess.run(["magick", *small, str(assets / "favicon.ico")], check=True)
    pngs = [*assets.glob("*.png"), root / "src" / "gitfurl" / "icon.png"]
    for png in pngs:  # strip timestamps so a rebuild gives the same bytes
        subprocess.run(["magick", str(png), "-strip", str(png)], check=True)


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
    if "--png" in sys.argv[1:]:
        render_pngs(root)
    print("wrote", *sorted(p.name for p in out.glob("*.svg")),
          *sorted(p.name for p in docs.glob("*.svg")), facts.name)
