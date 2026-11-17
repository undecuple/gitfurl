# Contributing

- Python 3.11 or newer, no runtime dependencies. Enter the dev shell with `nix develop`, or install pytest and ruff yourself.
- Run `ruff check .` and `PYTHONPATH=src python -m pytest -q` before a pull request.
- Tests use temporary directories and mocked subprocess calls. They must not need Wine, a network or a real installer.
- Do not commit installers, game files, archives, keys or logs. The pre-commit hook in `.githooks/` blocks them; enable it with `git config core.hooksPath .githooks`.
- Subprocess calls take argument lists, never `shell=True`.
- Do not add downloads, installer search, cracks or activation bypasses. They are out of scope.
- State what you ran. Do not claim compatibility you did not test.
