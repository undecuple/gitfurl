# Development

Python 3.11 or newer, no runtime dependencies. The graphical app needs tkinter. `nix develop` provides it, plus pytest, ruff, bubblewrap and Xvfb for a headless window check.

    pip install -e '.[dev]'   # needs network; ask first
    python -m unittest discover -s tests
    pytest
    ruff check .

Tests use temporary directories and mocked subprocess calls. They need no Wine, no network and no real installer. Do not use a real installer or game file for development.

CI also fails if a tracked file is an installer, archive or key, or is over 1 MiB. A pre-commit hook in `.githooks/` blocks installers, archives, keys and files over 1 MiB. Enable it with `git config core.hooksPath .githooks`.

## Running an installer in a sandbox
This wrapper is a manual script, **tested on NixOS only**: it binds `/nix` and `/run/current-system` and exits on other systems. It is not part of the app. It sets no `DISPLAY`, so a graphical installer opens no window unless you start Xvfb inside it. With `SANDBOX_ETC=1` it also exposes `/etc/passwd`, `/etc/group`, `/etc/hosts`, `/etc/machine-id`, `/etc/zoneinfo` and `/sys` read-only, which weakens the "empty home" idea.

`tools/sandbox.sh INSTALLER_DIR WORKDIR command...` runs a command in bubblewrap with the network off, an empty home folder and the installer folder read-only at `/installer`. Only `WORKDIR` (at `/work`) is writable; delete it afterwards. It starts no display; for a graphical installer, start Xvfb inside the command (see the `wine` dev shell, which has Xvfb). `SANDBOX_ETC=1` adds read-only `/etc` files and `/sys`, which umu-launcher and Proton's container need.

Use only installers you are authorised to use. Do not copy logs, screenshots or output from such a run off the machine.

## Assets
`tools/make_assets.py` generates `assets/*.svg` from `src/gitfurl/pixelfont.py`, which the app's wordmark also uses. PNG sizes and the favicon are made from the SVGs with ImageMagick.
