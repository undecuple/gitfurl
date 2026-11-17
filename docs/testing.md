# Testing

What has been run, and how to repeat it. Counts are not listed here because they go stale; run the commands.

## Unit tests (no Wine, no installer, no network)
    nix develop -c python3 -m pytest -q      # or: PYTHONPATH=src python -m pytest -q
    ruff check .

They use temporary folders, fake `/proc` data, fake Wine and mocked subprocess calls. One test opens the window under Xvfb for 5 seconds; it is skipped when `xvfb-run` or tkinter is missing. CI runs both commands on Python 3.11 and 3.12.

## Starting a real installer (manual, not in CI)
Checked by hand on 2026-11-16, Linux x86_64, Wine 11.0 (nixpkgs `wineWow64Packages.stable`):
1. Make a work folder on a disk with space. It will hold the prefix.
2. Run `tools/sandbox.sh INSTALLER_DIR WORKDIR sh script.sh`, where the script starts Xvfb and then `python -m gitfurl cli install /installer/setup.exe --prefix t --create-prefix --yes`. The wrapper has no network and an empty home. It is NixOS-only.
3. Count the windows (`xwininfo -root -tree`) and note whether the process is still running every 15 seconds. Stop it after 120 seconds.
4. Delete the work folder.

Result: two user-supplied installers started and stayed open for the whole 120 seconds. Only counts and the exit status were looked at. No installation was completed, and nothing was run afterwards. The installers are not published or named here, and none is in the repository. Do not copy installer output, screenshots or logs off the machine.

One installer was also started through Proton (GE-Proton) with umu-launcher in the same sandbox, with `SANDBOX_ETC=1`. It stayed open for 120 seconds. Proton's output held one error line whose source was not found.

## Not tested
A complete installation, running an installed program, the progress numbers on a real install (tested against fake `/proc` data only), Proton from the app, the Docker image, a non-NixOS system, keyboard navigation and screen readers.
