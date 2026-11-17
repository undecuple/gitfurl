<p align="center">
  <img src="assets/banner.svg" alt="gitfurl" width="660">
</p>

<h1 align="center">gitfurl</h1>

<p align="center"><b>Run a Windows installer you already have on Linux, in its own Wine environment, with a plan you approve first.</b></p>

<p align="center">
  <a href="https://github.com/undecuple/gitfurl/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/undecuple/gitfurl/ci.yml?branch=main&label=CI&labelColor=0C1021&style=flat-square" alt="CI status"></a>
  <a href="https://github.com/undecuple/gitfurl/releases/latest"><img src="https://img.shields.io/github/v/release/undecuple/gitfurl?label=release&color=FF6400&labelColor=0C1021&style=flat-square" alt="Latest release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/licence-GPL--3.0--or--later-FCB439?labelColor=0C1021&style=flat-square" alt="Licence: GPL-3.0-or-later"></a>
  <img src="https://img.shields.io/badge/python-3.11%2B-FBDE2D?labelColor=0C1021&style=flat-square" alt="Python 3.11 or newer">
  <img src="https://img.shields.io/badge/platform-Linux-00C1E4?labelColor=0C1021&style=flat-square" alt="Platform: Linux">
  <img src="https://img.shields.io/badge/runtime-Wine%20%7C%20Proton-FF6400?labelColor=0C1021&style=flat-square" alt="Runtime: Wine, optional Proton">
</p>

<p align="center"><a href="#getting-started">Getting started</a> · <a href="#why-would-i-want-this">Why would I want this?</a> · <a href="#status">Status</a> · <a href="#known-issues">Known issues</a> · <a href="#ai-use">AI use</a> · <a href="docs/user-guide.md">User guide</a> · <a href="#building">Building</a> · <a href="https://github.com/undecuple/gitfurl/issues/new/choose">Report a bug</a></p>

---

gitfurl is a graphical app (with an optional command line) for running a Windows installer on Linux through Wine. **You supply the installer.** Gitfurl does not find, download or include any.

> **Status: version 1.2.1, released on 2026-11-17 (the first release, 1.0.0, was on 2026-11-16).** It means the app, the command line and the safety checks are complete for the scope below. It does **not** mean any particular installer works: compatibility is unverified. Installers have been *started* in a sandbox, but a complete install has not been tested, and the code has not been audited. Details under [Status](#status).

![The Library page: a list of Wine prefixes](docs/screenshot.png)

*The Library page with demo prefixes (empty folders). No installer was run to make this picture.*

## Why would I want this?

Running a Windows installer under Wine by hand works, until it does not. These are the problems gitfurl is for:

- **You do not want it to use your main Wine prefix.** Run `wine setup.exe` with no setup and it uses `~/.wine`. Gitfurl makes a separate prefix for each thing you install and never uses `~/.wine` as the prefix. (Wine and the installer can still write elsewhere, for example `~/.cache/wine` and menu entries; the optional changes report shows some of that.)
- **You want to see what will happen before it happens.** Gitfurl shows the exact command, prefix, folder and environment variable first, and nothing runs until you approve.
- **Big installers look frozen.** Some installers sit for a long time with a bar that does not move. Gitfurl watches how far the installer has read into the large files beside it and shows a percentage, a rate and a rough time left, without relying on the installer's own display.
- **You want to know what it changed.** An optional report lists new files and registry keys in the prefix, and changes in a few places outside it (menu entries, autostart, your home folder's top level).
- **You want a log you can read.** Each run gets a log. An optional JSON event log and Wine debug output are there when you need more.
- **You want to tidy up afterwards.** An optional, guarded step deletes the original installer after a run that exits cleanly. It is off by default.
- **You want to try Proton instead of Wine.** One setting switches the runtime, through umu-run, with a Proton folder you already have.
- **You want a stricter box.** The repository includes a bubblewrap wrapper that runs an installer with no network and an empty home folder. It is a manual script, tested on NixOS only, and it opens no window.

### When you probably do not need it
- If you are happy running Wine yourself, you do not.
- Gitfurl is for the installer step. It is **not** a game launcher or a library manager, and it does not claim to run any particular installer. Tools built for managing prefixes or launching games cover more than this does.
- Wine prefixes are not sandboxes. Gitfurl makes the run visible and tidy; it does not make an untrusted installer safe.

## Screenshots
These were made with gitfurl 1.2.1 under a virtual display, in a demo home folder (`/home/demo`) with empty example prefixes. No real installer and no real Wine were run: a small stand-in script took Wine's place.

![A prefix's page with an installer chosen: the checklist, the installer field and the options, with the memory limit on](docs/screenshot-plan.png)

*Before a run: the checklist, the chosen installer and the options. Nothing runs until you approve the plan.*

![A run in progress: the progress bar at 58 percent, with payload read, rate, estimate, data written and the process count](docs/screenshot-run.png)

*During a run. The numbers are real readings from `/proc`, but of the stand-in, which slowly read 0.21 GB of example files (sparse files of zeros) and wrote the same amount into the prefix. That is why the process shown is `python3.14`.*

![The Settings page: Wine, bubblewrap, umu-run and platform, and the About card](docs/screenshot-settings.png)

*Settings, made on the maintainer's machine: the bubblewrap version and the platform line are real, the Wine version is the stand-in's.*

## Getting started

The [latest release](https://github.com/undecuple/gitfurl/releases/latest) has a single-file Linux program and a `SHA256SUMS` file (check it with `sha256sum -c SHA256SUMS`). It is not proven on every desktop; see Known issues. Or run it from source. Either way you need Linux and Wine; from source you also need Python 3.11 or newer and tkinter (`python3-tk` on Debian and Ubuntu).

1. **Get the code.** `git clone https://github.com/undecuple/gitfurl.git && cd gitfurl`
2. **Check your setup.** `python -m gitfurl cli doctor` reports Wine, tkinter, bubblewrap and umu-run. With Nix, `nix develop .#wine` gives you Python with tkinter and Wine.
3. **Open the app.** `python -m gitfurl`, then press **+ New prefix**, name it, and open it.
4. **Choose your installer and run it.** Pick the file, read the plan, press **Run installer** and confirm. The installer's own window opens; use it as normal.
5. **Check the result.** Gitfurl shows the exit status and where the log is. The [user guide](docs/user-guide.md) explains every option.

Prefer a terminal? `python -m gitfurl cli --help`. The command line uses the same code and the same approvals.

## Status

As of 2026-11-16, on Linux x86_64, Wine 11.0, tested headless (no GPU, a virtual display):

| Setup | How it runs | State | Tested on |
|---|---|---|---|
| Unit tests | `pytest`, mocks only | verified | passing at the last commit; see [docs/testing.md](docs/testing.md) |
| Graphical app | tkinter | verified (renders and starts) | headless screenshot; never used by a person on a real desktop here |
| Installer start, Wine | `gitfurl cli install`, in a no-network bubblewrap sandbox | verified: two user-supplied installers started and stayed open 120 s | Wine 11.0; then stopped. No install was completed. The installers are not published (see [docs/testing.md](docs/testing.md)) |
| Installer start, Proton | `umu-run` with GE-Proton, same sandbox | verified: one installer started and stayed open 120 s | one error line in Proton's output, source not found |
| Complete install, then running the result | any runtime | **not tested** | - |
| Progress numbers on a real install | `/proc` read offsets | implemented | tested only against fake `/proc` data |
| Proton from the app | runtime setting | implemented | not run end to end |
| ClamAV scan (optional) | `clamscan` on PATH | implemented | tested with a stand-in `clamscan` only; a real `clamscan` was never run |
| Memory limit (on by default) | `systemd-run --user --scope` | implemented | stand-ins, and test programs in a real scope on one NixOS machine (systemd 262); never with Wine, Proton or an installer |
| Docker image and compose file | - | planned, after the first release | - |
| Flatpak | - | planned | - |

"verified" means a run with a named setup. "implemented" means the code is in but not run there. "planned" means neither.

## Known issues
- Compatibility with any particular installer is unverified.
- Progress and the "installed program" guess are heuristics. They can be wrong or show nothing for installers that read their files in another way.
- The app is dark only, with a fixed pixel font. Tab and Return/space work on buttons, switches and the sidebar (added 2026-11-16, lightly tested). Screen readers cannot read it: Tk does not expose its widgets to them.
- Gitfurl refuses to create prefixes or run installers as root.
- The memory limit is on by default. Without a user systemd manager, or without the memory controller delegated to it, runs are refused until you turn the limit off (`--no-memory-limit`, or the option in the window). Swap is not limited.
- Proton needs a Proton folder you already have, and umu-run downloads its own runtime on first use if it is missing.
- The single-file Linux build in the 1.0.0 release starts and runs `cli` here, and its window opens in a Debian image with Tk and fonts. The 1.1.0, 1.2.0 and 1.2.1 builds are made by the same workflow and have not been checked separately. It is **not proven on every desktop**: it expects the system's standard X11 libraries and at least one font. On NixOS it stops with `libxcb.so.1: cannot open shared object file` unless the libraries are provided (for example with nix-ld) or you run from source with `nix develop`. See the [roadmap](ROADMAP.md).

## What goes online
Nothing, from gitfurl itself. (Inherited `WINE*` variables from your shell are not passed to the installer, so the plan shows everything that is set.) It makes no network requests, sends no telemetry and uploads nothing. Two things you start yourself can use the network: umu-run downloading its runtime on first use, and anything the installer does. The [sandbox wrapper](docs/development.md#running-an-installer-in-a-sandbox) turns the network off for the installer.

## Where your data goes
- Prefixes: `~/.local/share/gitfurl/prefixes/` (or under `$XDG_DATA_HOME`).
- Logs: `~/.local/state/gitfurl/logs/` (or under `$XDG_STATE_HOME`), readable only by you. They can contain your user name and local paths; remove those before sharing one.
- Nothing is written to `~/.wine`.

## Building
The code has no runtime dependencies. For tests and lint, enter `nix develop`, or install `pytest` and `ruff`, then `PYTHONPATH=src python -m pytest -q` and `ruff check .`. See [docs/development.md](docs/development.md).

## How it works, briefly
- **Core:** plain Python that builds an execution plan (argument list, prefix, environment) and shows it before anything runs.
- **Interfaces:** the app and the command line are thin layers over that core, with the same approvals.
- **Progress:** reads, through `/proc`, how far running processes are into the big files beside the installer, and which of your processes run in the prefix (see [SECURITY.md](SECURITY.md) for exactly what is read). It does not read the contents of your files.
- More in [docs/architecture.md](docs/architecture.md).

## Related projects
- **[Wine](https://www.winehq.org/):** the compatibility layer gitfurl drives.
- **[umu-launcher](https://github.com/Open-Wine-Components/umu-launcher):** used for the optional Proton runtime.

- **[Proton](https://github.com/ValveSoftware/Proton) and [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom):** the optional runtime umu-launcher uses. Gitfurl does not ship either.
- **[vim-tictoc](https://github.com/adonaldson/vim-tictoc):** the colour scheme the app and the images follow.

Gitfurl is not affiliated with these projects or with any software publisher.

## Roadmap
First a release that has been tested on a complete install, then a runtime choice in the app with Proton checked end to end, then a Docker image and compose file. See [ROADMAP.md](ROADMAP.md).

## Contributing
See [CONTRIBUTING.md](CONTRIBUTING.md). Security problems: [SECURITY.md](SECURITY.md).

## Repository layout
```
src/gitfurl/   the app, command line and core
tests/         mock-based tests (no Wine, no network, no installers)
docs/          user guide, scope, architecture, development
tools/         sandbox wrapper and the asset generator
assets/        banner, icon, favicon, social preview
.github/       CI and issue forms
```

## Licence
Copyright (C) 2026 undecuple. GPL-3.0-or-later; see [LICENSE](LICENSE). The images in `assets/` are generated by `tools/make_assets.py` and are under the same licence. The licence covers gitfurl only, not any installer or software you run with it.

## AI use
Gitfurl is written with AI coding agents. Much of its code, tests and documentation was written by Claude (Anthropic's model, used through Claude Code) under the maintainer's direction. A second Claude session reviews each change before it goes public, and the maintainer approves every merge, tag and release. The code has not been audited by a security professional (see [SECURITY.md](SECURITY.md)). Read the plan before you approve a run, and report problems through the issue forms.

## Disclaimer
Gitfurl comes with no warranty, as in section 15 of the GPL. You are responsible for what you run and for being authorised to use it. Names of other products belong to their owners; gitfurl is not affiliated with them. Running an installer can harm your system; a Wine prefix is not a sandbox.
