"""Block-letter glyphs shared by the app and tools/make_assets.py (5 rows tall)."""

BIG = {
    "g": [".XXXX", "X....", "X.XXX", "X...X", ".XXXX"],
    "i": ["XXX", ".X.", ".X.", ".X.", "XXX"],
    "t": ["XXXXX", "..X..", "..X..", "..X..", "..X.."],
    "f": ["XXXXX", "X....", "XXXX.", "X....", "X...."],
    "u": ["X...X", "X...X", "X...X", "X...X", ".XXX."],
    "r": ["XXXX.", "X...X", "XXXX.", "X..X.", "X...X"],
    "l": ["X....", "X....", "X....", "X....", "XXXXX"],
}
SMALL = {
    "a": ["XXX", "X.X", "XXX", "X.X", "X.X"], "b": ["XX.", "X.X", "XX.", "X.X", "XX."],
    "c": ["XXX", "X..", "X..", "X..", "XXX"], "d": ["XX.", "X.X", "X.X", "X.X", "XX."],
    "e": ["XXX", "X..", "XXX", "X..", "XXX"], "f": ["XXX", "X..", "XX.", "X..", "X.."],
    "g": ["XXX", "X..", "X.X", "X.X", "XXX"], "h": ["X.X", "X.X", "XXX", "X.X", "X.X"],
    "i": ["XXX", ".X.", ".X.", ".X.", "XXX"], "k": ["X.X", "X.X", "XX.", "X.X", "X.X"],
    "l": ["X..", "X..", "X..", "X..", "XXX"], "m": ["X.X", "XXX", "XXX", "X.X", "X.X"],
    "n": ["XX.", "X.X", "X.X", "X.X", "X.X"], "o": ["XXX", "X.X", "X.X", "X.X", "XXX"],
    "p": ["XXX", "X.X", "XXX", "X..", "X.."], "r": ["XXX", "X.X", "XX.", "X.X", "X.X"],
    "s": ["XXX", "X..", "XXX", "..X", "XXX"], "t": ["XXX", ".X.", ".X.", ".X.", ".X."],
    "u": ["X.X", "X.X", "X.X", "X.X", "XXX"], "w": ["X.X", "X.X", "XXX", "XXX", "X.X"],
    "x": ["X.X", "X.X", ".X.", "X.X", "X.X"], "y": ["X.X", "X.X", ".X.", ".X.", ".X."],
    " ": ["...", "...", "...", "...", "..."],
}


def cells(word, font, gap=1):
    out, x = [], 0
    for ch in word:
        g = font[ch]
        for r, row in enumerate(g):
            for c, v in enumerate(row):
                if v == "X":
                    out.append((x + c, r, ch))
        x += len(g[0]) + gap
    return out, x - gap
