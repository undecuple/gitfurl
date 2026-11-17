"""Entry point. The graphical interface is the default; the command line is opt-in."""

from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] == "cli":
        from gitfurl.cli import main as cli_main

        return cli_main(argv[1:])
    if argv and argv[0] in ("-h", "--help"):
        print("usage: gitfurl            open the graphical app\n"
              "       gitfurl cli ...    use the command line (gitfurl cli --help)")
        return 0
    if argv:
        print("gitfurl opens the graphical app. For the command line use: gitfurl cli "
              + " ".join(argv), file=sys.stderr)
        return 2
    from gitfurl.gui import main as gui_main

    return gui_main()
