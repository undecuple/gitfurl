# Development

Python 3.11 or newer, no runtime dependencies. The graphical app needs tkinter. `nix develop` provides it, plus pytest, ruff, bubblewrap and Xvfb for a headless window check.

    pip install -e '.[dev]'   # needs network; ask first
    python -m unittest discover -s tests
    pytest
    ruff check .

Tests use temporary directories and mocked subprocess calls. They need no Wine, no network and no real installer. Do not use a real installer or game file for development.

CI also fails if a tracked file is an installer, archive or key, or is over 1 MiB. A pre-commit hook in `.githooks/` blocks installers, archives, keys and files over 1 MiB. Enable it with `git config core.hooksPath .githooks`.

## Repository layout
The [code map](code-map.md) shows how the modules, entry points, tests and CI jobs fit together.
```
src/gitfurl/   the app, command line and core
tests/         mock-based tests (no Wine, no network, no installers)
docs/          user guide, scope, status, testing, architecture, code map, development
native/        gitfurl-codecs: the optional native decoder module (Rust, PyO3)
tools/         sandbox wrapper, asset generator, native module build
assets/        banner, icon, favicon, social preview
.github/       CI and issue forms
```

## The native decoder module (optional)
The cm stage of FreeArc extraction is decoded by `gitfurl._codecs`, a Rust extension in `native/gitfurl-codecs` (edition 2021, PyO3 with the stable ABI for Python 3.11 and newer, built with maturin). gitfurl itself stays pure Python and installs without Rust; the release workflow builds the module (pinned Rust and maturin, `cargo test --locked`, `tools/build_codecs.sh`) and PyInstaller bundles it into the single-file program, whose smoke test checks that `gitfurl cli doctor` reports "native decoders: cm, rolz". Without the module the cm stage is refused with "the cm stage needs gitfurl's native decoder module, which is not installed", and everything else works as before. The rolz stage uses the module's port of `farc_rolz.py` (`src/rolz/`) when the module has it (`farc_codecs.rolz_native()`), and `farc_rolz.py` otherwise; the Python decoder stays the reference, and `tests/test_farc_rolz_native.py` checks that both give the same bytes or the same refusal text for every stream the rolz tests build. When you change one decoder, change the other the same way. The decoder modules forbid `unsafe`; only the Python bindings (`src/py.rs`, the `python` feature) use PyO3's.

    nix develop .#native -c sh tools/build_codecs.sh   # builds the wheel, puts gitfurl/_codecs.abi3.so into src/
    nix build .#codecs                                 # or: the module as a Nix package (nixpkgs' python3)

Without Nix: a Rust toolchain (CI pins 1.99.1) and `pip install maturin==1.15.0`, then `sh tools/build_codecs.sh`. The built `.so` in `src/gitfurl/` is ignored by git. The crate's own checks, from `native/gitfurl-codecs`:

    cargo fmt --check
    cargo clippy --locked --all-targets -- -D warnings
    cargo clippy --locked --features python --all-targets -- -D warnings
    cargo test --locked

CI runs these in its `native` job, builds the module and runs the Python suite with it. Keep local builds to a few threads (`CARGO_BUILD_JOBS=4`, `nice -n 19`). `examples/cmcheck.rs` is a manual checksum tool for the real-data check in [testing.md](testing.md): it prints CRC-32s and SHA-256s and writes no decoded data.

## Release build dry run
`.github/workflows/release.yml` builds the single-file program. A pushed `v*` tag builds it and attaches it to a draft release; started by hand, it is a build-only dry run:

    gh workflow run release.yml --ref main

The dry run runs the same steps as a release build (lint, tests, `cargo test --locked`, the native module, PyInstaller, the smoke test with `cli doctor` and the window under Xvfb) and uploads one workflow artifact, `gitfurl-linux-x86_64`, holding:
- `gitfurl-main-linux-x86_64`: the program (named after the ref; slashes in a branch name become `-`);
- `THIRD_PARTY.md`: the third-party notices;
- `BUNDLED.txt`: what PyInstaller put into the program (`pyi-archive_viewer -l -r -b`), with the runner image and the CPython, OpenSSL, SQLite, zlib, expat and Tcl/Tk versions the runner's CPython reports;
- `SHA256SUMS`: checksums of those three files.

It creates no release, tag or draft, and so does not start the notify workflow; the draft-release job runs only when `github.event_name` is `push`, even if the run is started on a tag. Fetch the artifact with `gh run download RUN_ID -n gitfurl-linux-x86_64`. A release gets the same four files as assets. When `BUNDLED.txt` lists a library that `THIRD_PARTY.md` does not name, add it there before the release.

## Running an installer in a sandbox
This wrapper is a manual script, **tested on NixOS only**: it binds `/nix` and `/run/current-system` and exits on other systems. It is not part of the app. It sets no `DISPLAY`, so a graphical installer opens no window unless you start Xvfb inside it. With `SANDBOX_ETC=1` it also exposes `/etc/passwd`, `/etc/group`, `/etc/hosts`, `/etc/machine-id`, `/etc/zoneinfo` and `/sys` read-only, which weakens the "empty home" idea.

`tools/sandbox.sh INSTALLER_DIR WORKDIR command...` runs a command in bubblewrap with the network off, an empty home folder and the installer folder read-only at `/installer`. Only `WORKDIR` (at `/work`) is writable; delete it afterwards. It starts no display; for a graphical installer, start Xvfb inside the command (see the `wine` dev shell, which has Xvfb). `SANDBOX_ETC=1` adds read-only `/etc` files and `/sys`, which umu-launcher and Proton's container need.

Use only installers you are authorised to use. Do not copy logs, screenshots or output from such a run off the machine.

## Assets
`tools/make_assets.py` generates `assets/*.svg` from `src/gitfurl/pixelfont.py`, which the app's wordmark also uses. It also makes the README's dividers (`docs/assets/divider-*.svg`) and recolours `docs/project-facts.svg` (made as a black-on-white label) to the same palette; running it again changes nothing. With `--png` it also renders the PNG sizes, `src/gitfurl/icon.png` (the window and menu-entry icon, 64 pixels) and the favicon, with resvg and ImageMagick from PATH (for example `nix shell nixpkgs#resvg nixpkgs#imagemagick`); the favicon's 16, 32 and 48 pixel images are drawn on the pixel grid, with the tiles shown only at 48. The social preview's tagline uses Exo 2 when it is installed.
