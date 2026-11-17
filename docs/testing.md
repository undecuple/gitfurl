# Testing

What has been run, and how to repeat it. Counts are not listed here because they go stale; run the commands.

## Unit tests (no Wine, no installer, no network)
    nix develop -c python3 -m pytest -q      # or: PYTHONPATH=src python -m pytest -q
    ruff check .

They use temporary folders, fake `/proc` data, fake Wine and mocked subprocess calls. One test opens the window under Xvfb for 5 seconds; it is skipped when `xvfb-run` or tkinter is missing. CI runs both commands on Python 3.11 and 3.12.

## Starting a real installer (manual, not in CI)
This was done with the maintainer's explicit approval, for two specific files the maintainer supplied and confirmed in person. The limits: this machine only, inside `tools/sandbox.sh` (no network, empty home), no credentials present, only counts and the exit status looked at, nothing copied off the machine, and no installer path or name in any tracked file or message. Anyone else repeating this should use their own installers under the same limits.

Checked by hand on 2026-11-16, Linux x86_64, Wine 11.0 (nixpkgs `wineWow64Packages.stable`):
1. Make a work folder on a disk with space. It will hold the prefix.
2. Run `tools/sandbox.sh INSTALLER_DIR WORKDIR sh script.sh`, where the script starts Xvfb and then `python -m gitfurl cli install /installer/setup.exe --prefix t --create-prefix --yes`. The wrapper has no network and an empty home. It is NixOS-only.
3. Count the windows (`xwininfo -root -tree`) and note whether the process is still running every 15 seconds. Stop it after 120 seconds.
4. Delete the work folder.

Stop only what you started: run the sandbox in its own process group (`setsid`, then `kill -- -PGID`), never by process name, and use `xvfb-run -a` rather than a fixed display, so other people's Wine, Proton or Xvfb jobs on the same machine are never touched.

Result: two user-supplied installers started and stayed open for the whole 120 seconds. Only counts and the exit status were looked at. No installation was completed, and nothing was run afterwards. The installers are not published or named here, and none is in the repository. Do not copy installer output, screenshots or logs off the machine.

One installer was also started through Proton (GE-Proton) with umu-launcher in the same sandbox, with `SANDBOX_ETC=1`. It stayed open for 120 seconds. Proton's output held one error line whose source was not found.

## Stop and preflight
Tested with controlled dummy processes (a Python script that starts a child and a grandchild) and fake `/proc` and meminfo data, plus a fake `wineserver` script that only records its arguments. They cover: the whole group ending on Stop, a child that ignores SIGTERM being killed after the grace period, a program that moved to another session surviving (the documented limit), stopping twice or after the end doing nothing, only the recorded group and `wineserver -k` for the plan's prefix being signalled (no call by name), Ctrl-C, the `cancelled` event, a failing installer reported as failed, and the preflight labels and caps. None of this shows that any real installer stops or installs correctly. No test drives the GUI's `stop_clicked` or its dialog: only the decision it re-checks (`stop_applies`) is tested, as a plain function. The CLI cancel test replaces `run_tracked` with a fake that performs a real stop before anything started, so it covers stop-before-start only; the stop of a running group is covered by the `StopTests` above. The CI-pinned tool versions (`pytest==8.3.5`, `ruff==0.11.13`) have not been run; the checks above used the nix dev shell's newer versions.

Observed once by hand on 2026-11-16 (Wine 11.0 wow64, NixOS, a throwaway prefix, the built-in command `cmd /c ping -n 60 127.0.0.1`): the run's own processes shared the leader's process group, and Wine's server and services each had their own session, so a group stop ended the run and left the prefix's server running; `wineserver -k` for that prefix then ended the rest. Not observed: a stop during `wineboot`, or an installer attached to a Wine server that was already running.

Not shown: Stop through `umu-run`. The attempt in `tools/sandbox.sh` with the cached Proton failed before starting ("umu has not been setup for the user; an internet connection is required"), so the Proton behaviour is unverified.

## Stop against a generated test installer (2026-11-16)
The maintainer pointed at a generated test installer: an Inno Setup 5.5.0 `setup.exe` (4 MB) and two `.bin` files (3.8 GB) beside it, made for testing. Only header bytes were read; the `.bat` and the checksum tool beside it were never run. Run once, in `tools/sandbox.sh` (no network, empty home), Xvfb, Wine 11.0 wow64 from nixpkgs, branch `fix/stop-races`:
- `gitfurl cli install ... --create-prefix` did not get past creating the prefix: `wineboot --init` was still running after 300 s (`control.exe` and `rundll32.exe` were running; the cause was not confirmed), then gitfurl ended with an uncaught `subprocess.TimeoutExpired` (now a clear error message, tested with a fake runner only). A Ctrl-C during that step ended gitfurl with 130 in 1 s and left `wineboot`, `services.exe` and other Wine processes running. That leftover is not fixed; the hang itself is not explained.
- With the prefix made by hand first (`wine wineboot --init`, `WINEDLLOVERRIDES=mscoree,mshtml=d`, exit 0), the installer started (`setup.exe`, `setup.tmp`) and stayed open for 60 s with 0 bytes of the `.bin` files read. One Ctrl-C sent to the gitfurl process: it exited 130 after 1.0 s, the event log ends with `cancelled` (no `exit`), the note says the prefix's Wine server was asked to exit, and no Wine process was left in the sandbox.
- Not shown: a complete install, Stop at other phases, Proton, more than one run.

## The single-file build (release workflow)
`release.yml` builds one Linux program with PyInstaller. Checked on 2026-11-16 by running the workflow by hand (it builds and uploads only):
- The checksum file matches. `cli --version` prints 1.0.0 and `cli doctor` runs.
- The window opens (stays open 6 s under a virtual display) in CI and in a Debian image that has Tk and fonts.
- On a bare Debian with no fonts it loads its bundled Tk but cannot create a window; the app now prints that reason.
- On NixOS it stops with `libxcb.so.1: cannot open shared object file`, because the build expects the system's standard X11 libraries.
Not proven: Fedora, Arch, Wayland-only sessions, older distributions than Ubuntu 22.04.

## Not tested
A complete installation, running an installed program, the progress numbers on a real install (tested against fake `/proc` data only), Proton from the app, the Docker image, a non-NixOS system, keyboard navigation and screen readers.
