"""Small safety checks shared by the app and the command line."""

from __future__ import annotations

import os


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


ROOT_MESSAGE = (
    "gitfurl will not run installers or create prefixes as root. Run it as a normal user. "
    "Never run an installer as root to get around a permission problem."
)
