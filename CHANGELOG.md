# Changelog

All notable changes are documented here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/).

## [Unreleased]

## [1.2.0] - 2026-11-17
An optional ClamAV scan before a run, and a memory limit for the run that is on by default. Neither was tried with Wine, Proton or a real installer: both were tested with stand-ins, and the memory limit also with test programs in a real systemd scope.

### Added
- An optional **ClamAV scan** before a run (`gitfurl cli install --scan`, and a toggle in the app), off by default. It runs the `clamscan` already on PATH on the installer and the regular files directly beside it, in its own process group with a timeout (`--scan-timeout`, default 600 s), only after approval. gitfurl passes no option that changes files (never `--remove`, `--move` or `--copy`); `clamscan` may still write its own temporary files. A match stops the run with nothing run or created, naming the files and signatures and saying that false positives are common for game installers; `--allow-scan-hit` or a second dialog in the app (default No) overrides it. A scan error, a timeout or unreadable output always stops the run and can not be overridden; a match in a scan where some files could not be scanned or read is such an error. gitfurl never runs `freshclam` or updates the database; `doctor` and the approval show the database date and warn when it is older than 7 days. A real `clamscan` was never run: this was tested with a stand-in script only.
- A **memory limit**, on by default (`--no-memory-limit` and the window option "Limit memory (reduces the chance of running out)" turn it off). The run's command, including `bwrap` for a portable install and `umu-run` for Proton, is started with `systemd-run --user --scope` with `MemoryMax` = `MemAvailable` minus a reserve of `max(1 GiB, 10% of MemTotal)` and `MemoryHigh` = 90% of that; it refuses to start below a 1 GiB hard limit and says how much memory to free. While the run lasts, the soft limit is lowered when other programs need memory and raised back later (only on that one unit, through `systemctl --user set-property --runtime`). After the run, an out-of-memory stop inside the scope is reported. It reduces the chance that the system runs out of memory; it does not guarantee that the installer succeeds. If systemd-run, a user manager or the memory controller is missing, the plan says why and a run needs the option turned off. `doctor` reports whether it is available. `wineboot` during `install --create-prefix` gets the same limits. **Visible change:** on a system without a user systemd manager or without the memory controller delegated to it, runs are refused until the limit is turned off. Swap is not limited, and programs already running outside the scope (for example a Wine server left from an earlier run) are not covered. Tested with fakes and test programs in a real scope only: Wine, Proton and installers were never run under the limit.

## [1.1.0] - 2026-11-17
The second release: Stop for a running installer, cleanup when prefix creation times out or is interrupted, a read-only guess at the installer's format, a reworked window (footer, keyboard use, checklist, banner, resizing), an optional portable install inside bubblewrap, and run events. As with 1.0.0, it does not mean any particular installer works: compatibility is unverified, a complete install has not been tested, and the code has not been audited.

### Added
- A **Stop** button (and Ctrl-C on the command line) for a running installer. It signals only the run's own process group (SIGTERM, then SIGKILL after 10 s), then, for Wine, asks that prefix's Wine server to exit. The run is reported as cancelled (exit status 130, a `cancelled` event); the log is kept and nothing is deleted. Complete process-tree termination is not guaranteed, and stopping through `umu-run` is unverified.
- A read-only, advisory preflight before a run: measured free space, installer-folder size and memory, apart from estimates; required space is shown as unknown and no multiplier is applied.
- The progress line shows how many processes run in the prefix, their CPU use and helper names, with a "quiet" hint; `doctor` reports CPU threads and available memory.

- A footer on every page: whether Wine and umu-run were found, the keys that work on the page, and the run state (Idle, Creating prefix, Installer running, Stopping...).
- Keyboard use: an orange focus outline on every button, switch, choice, prefix card and sidebar item; Tab follows the screen order and scrolls the page to the focused item; Enter or Space activates; Up and Down move between prefixes in the Library; Esc goes from a prefix's page back to the Library. There is no screen-reader support.
- A prefix's page has a checklist (installer chosen, Wine or umu-run found, prefix exists or will be created, each with a tick or cross and a sentence) and a bar at the bottom with Stop, Create now and Run installer that stays in view, with the reason in words when a button is off.
- The "Wine was not found" message is a banner with a Check again button, on the Library and on a prefix's page.
- An optional **portable install** (`gitfurl cli install --portable`, and a toggle in the app), off by default. The Wine installer runs inside bubblewrap with the dedicated prefix, a temporary home and `/tmp`, the run's log file and `/dev/dri` writable, the network off, the environment cleared except an allowlist (so tokens and keys are not passed), your home folder and the D-Bus sockets hidden and Wine's menu builder disabled. Whatever the installer leaves in the prefix runs again outside the sandbox when the prefix is used later. It reduces changes outside the prefix as far as bwrap isolates them; it is not a guarantee or a security boundary. It refuses (no fallback) without `bwrap`, with the proton runtime and as root.
- Optional, advisory inactivity detection (`--stall-after SECONDS`, `RunOptions.stall_after_s`; off by default): one `possibly_stalled` event and one note if no payload read, no CPU use and no log growth for that long. It never stops or signals anything.
- With the detailed log and the progress monitor, `processes` and `memory` events (program names, count, `MemAvailable`); no environment values.
- A run now returns a result with `started`, `extraction_observed` and `exit_status` as separate fields (also in the last event and in the end text). `extraction_observed` is a heuristic ("observed activity": the prefix grew and payload files were read); it does not show that anything was extracted or installed. The old `(status, log)` unpacking still works. Tested with fakes only, not with Wine or a real installer.

### Changed
- Before a run, the plan preview, the approval dialog and `gitfurl cli install --dry-run` show a read-only guess at the installer's format (Inno Setup, FreeArc), from file headers only. Nothing is extracted and the route is always Wine/Proton; `doctor` also notes whether `innoextract` is on PATH by running its `--version` (a bounded probe, as for `wine`); nothing runs it to extract.
- The content follows the window width (the window already had a smallest size of 900 x 620) and nothing is clipped at that size; one spacing scale, one type scale, one corner radius, and a computed contrast check of every text colour (disabled buttons and the hover colour of the ghost button were below 4.5:1 and were changed).
- Run is now off, with the reason shown, when Wine (or umu-run) is not on PATH or when the prefix does not exist and creating it is off. Before, the button was on and a dialog said so after the click. The dialogs remain as a second check. Approvals, dry runs, Stop and cleanup are unchanged.
- The README, the user guide and SECURITY.md state exactly what the progress display reads from `/proc`, including the process environment (only to find the prefix).

## [1.0.0] - 2026-11-16
The first release. It means the app, the command line and the safety checks are complete for the stated scope. It does not mean any particular installer works; compatibility is unverified, and the single-file Linux build is not proven on every desktop (see the README known issues).

### Added (after review)
- `release.yml`: builds the graphical app as one Linux program on a `v*` tag and attaches it, with SHA256SUMS, to a draft release. Not yet run.
- `Dockerfile`, `compose.yaml`, `docs/docker.md`: a container with the network off and a read-only installer folder. Builds and starts under Podman.
- Refusal to create prefixes or run installers as root.
- `doctor` reports tkinter.
- `docs/testing.md`, Python 3.11 and 3.12 in CI, shellcheck, a check for tracked installers and large files, and dependabot for the action pins.

### Changed (after review)
- The sidebar shows the real reason when umu-run cannot be probed, and the README screenshot is retaken with nothing missing or failing.
- `WINE*` variables inherited from your shell are no longer passed to the installer.
- Cleanup `folder` mode refuses `~/Downloads`, `~/Desktop`, `~/Documents` and similar general folders.
- `tools/sandbox.sh` exits with a clear message outside NixOS.
- Docs say what "never touches `~/.wine`" does and does not cover, and that the sandbox wrapper is NixOS-only.


### Added
- Optional guess at the installed program (`--find-exe`; a switch in the app). Local only: it runs nothing and touches no launcher.
- Proton as a runtime choice (`--runtime proton --proton-path DIR`; a control in the app), through umu-run. It needs a Proton folder you already have and downloads nothing itself. `doctor` now reports umu-run.
- Progress reporting (payload read, rate, rough ETA, bytes written to the prefix) in the app and with `--progress`. Optional detailed JSON event log (`--detailed-log`) and `--wine-debug`.
- A user guide: `docs/user-guide.md`.
- The app was redesigned with rounded cards, a sidebar of pages and block-letter tiles, to match the repository assets.
- Optional cleanup of the original installer (`--cleanup installer|folder`; a dropdown in the app). Off by default. Nothing is deleted unless the installer exits with status 0, the list of files is shown for approval first, symlinks are removed and never followed, and it refuses home, the root, mount points, system folders, folders holding gitfurl's own data, and folders with more than 200 entries.
- Optional report of what the installer changed (`--show-changes`; a checkbox in the app). It compares before and after snapshots of the prefix (files and registry key names) and of a few host places an installer could touch outside the prefix (menu entries, autostart, the home folder's top level). It lists names only, never contents, and is best effort.
- "Create it if it does not exist" option for the prefix, on by default in the app. The confirmation dialog says the prefix will be created first. On the command line it is opt-in: `--create-prefix`. If the prefix cannot be created, the installer is not run.
- Graphical app (tkinter), now the default: `gitfurl` opens it. It shows the environment, previews the plan, creates prefixes and runs installers, each after a confirmation dialog.
- The app uses a library and detail layout in the style of Bottles: a sidebar of prefixes, then cards for the installer, options and plan.
- `tools/sandbox.sh`: no network, empty home, host session variables unset; `SANDBOX_ETC=1` adds the read-only files umu and Proton need.
- "Create it if it does not exist" option for the prefix, on by default in the app. The confirmation dialog says the prefix will be created first. On the command line it is opt-in: `--create-prefix`. If the prefix cannot be created, the installer is not run.
- The command line moved behind `gitfurl cli ...`. A bare `gitfurl cli doctor` no longer runs the CLI.
- Banner, app icon, favicon and social preview in `assets/`, generated by `tools/make_assets.py`.
- `gitfurl cli doctor`: reports the platform, Wine and bubblewrap, with a timeout on each probe.
- `gitfurl cli install FILE --dry-run`: shows the command, prefix, working directory and environment overrides. Runs and creates nothing.
- `gitfurl cli prefix create NAME` and `prefix list`: dedicated prefixes under the per-user data directory, created only after approval.
- `gitfurl cli install FILE --prefix NAME`: runs the installer after approval, writes a local log (mode 0600) and reports the exit status.
- Mock-based tests; no test needs Wine, a network or a real installer.
