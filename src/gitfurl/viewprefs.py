"""The window's view settings: text size, reduced motion and the remembered window size.

They live in one small JSON file, $XDG_CONFIG_HOME/gitfurl/view.json (mode 0600), written only
when the user changes a view setting or closes the window with "Remember window size" on.
Nothing else is stored there, and a missing or damaged file gives the defaults.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import asdict, dataclass, replace
from pathlib import Path

TEXT_SIZES = {"smaller": 0.9, "normal": 1.0, "larger": 1.2}
_GEOMETRY = re.compile(r"^\d{2,5}x\d{2,5}$")  # size only: the desktop decides the place
MAX_BYTES = 4096


@dataclass(frozen=True)
class ViewPrefs:
    text_size: str = "normal"
    reduced_motion: bool = False
    remember_size: bool = False
    size: str = ""  # "WIDTHxHEIGHT" when remember_size is on and the window was closed once

    @property
    def scale(self) -> float:
        return TEXT_SIZES[self.text_size]


def prefs_path(env=None) -> Path:
    env = os.environ if env is None else env
    base = env.get("XDG_CONFIG_HOME") or str(Path(env.get("HOME") or Path.home()) / ".config")
    return Path(base) / "gitfurl" / "view.json"


def clean(raw) -> ViewPrefs:
    """A ViewPrefs from whatever the file held; anything unknown or out of range is dropped."""
    if not isinstance(raw, dict):
        return ViewPrefs()
    p = ViewPrefs()
    if raw.get("text_size") in TEXT_SIZES:
        p = replace(p, text_size=raw["text_size"])
    for key in ("reduced_motion", "remember_size"):
        if isinstance(raw.get(key), bool):
            p = replace(p, **{key: raw[key]})
    size = raw.get("size")
    if isinstance(size, str) and _GEOMETRY.match(size):
        p = replace(p, size=size)
    return p


def load(path: Path | None = None) -> ViewPrefs:
    path = prefs_path() if path is None else path
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
            return ViewPrefs()
        return clean(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        return ViewPrefs()


def save(prefs: ViewPrefs, path: Path | None = None) -> str | None:
    """Write the file (0600, through a temporary file and a rename). Returns a problem, or None."""
    path = prefs_path() if path is None else path
    data = asdict(prefs)
    if not prefs.remember_size:
        data["size"] = ""
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if path.is_symlink():
            return f"{path} is a symbolic link; the view settings were not saved"
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)
    except OSError as exc:
        return f"the view settings were not saved: {exc}"
    return None
