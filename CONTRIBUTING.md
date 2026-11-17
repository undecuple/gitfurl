# Contributing

AI agents working in this repository: read [AGENTS.md](AGENTS.md) first.

- Python 3.11 or newer, no runtime dependencies. Enter the dev shell with `nix develop`, or install pytest and ruff yourself.
- Run `ruff check .` and `PYTHONPATH=src python -m pytest -q` before a pull request.
- Tests use temporary directories and mocked subprocess calls. They must not need Wine, a network or a real installer.
- Do not commit installers, game files, archives, keys or logs. The pre-commit hook in `.githooks/` blocks them; enable it with `git config core.hooksPath .githooks`.
- Subprocess calls take argument lists, never `shell=True`.
- Do not add downloads, installer search, cracks or activation bypasses. They are out of scope.
- State what you ran. Do not claim compatibility you did not test.

## Reporting a problem
Use the bug form. Give the gitfurl version, your distribution and Wine version, and a log with your user name and paths removed. Do not attach installers, game files or keys. Security problems go to the private reporting link in [SECURITY.md](SECURITY.md), not to a public issue.

## Tests and style
Mock-based tests live in `tests/`; the window test runs only when Xvfb and tkinter are available. CI runs Python 3.11 and 3.12, ruff, shellcheck and a secrets scan. Use the ruff and pytest versions pinned in `.github/workflows/ci.yml`.

## Commits and pull requests
Short plain subject line, a body that says why. Do not add AI or co-author trailers. Pull requests use the template. Changes to `.github/workflows/` get extra review because they run with repository permissions.
