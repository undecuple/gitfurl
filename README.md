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

<p align="center"><a href="#getting-started">Getting started</a> · <a href="docs/user-guide.md">User guide</a> · <a href="docs/status.md">Status and known issues</a> · <a href="#ai-use">AI use</a> · <a href="#community">Community</a> · <a href="https://github.com/undecuple/gitfurl/issues/new/choose">Report a bug</a></p>

---

gitfurl is a graphical app (with an optional command line) for running a Windows installer on Linux through Wine. **You supply the installer.** Gitfurl does not find, download or include any.

<p align="center">
  <img src="docs/project-facts.svg" alt="Project Facts: what gitfurl does, its limits and how to use it" width="420">
</p>

> **Version 1.3.0, released 2026-11-18.** The app, the command line and the safety checks are complete for the scope in [docs/scope.md](docs/scope.md). That does **not** mean any particular installer works: compatibility is unverified, a complete install has been tested only with an installer built for the test, and the code has not been audited. Details: [docs/status.md](docs/status.md).

![The Library page: a list of Wine prefixes](docs/screenshot.png)

*The Library page with demo prefixes (empty folders). No installer was run to make this picture. More pictures are in the [user guide](docs/user-guide.md).*

## Getting started

The [latest release](https://github.com/undecuple/gitfurl/releases/latest) has a single-file Linux program and a `SHA256SUMS` file (check it with `sha256sum -c SHA256SUMS`). It is not proven on every desktop (see [docs/status.md](docs/status.md#known-issues)). Or run it from source. You need Linux and Wine; from source also Python 3.11 or newer and tkinter (`python3-tk` on Debian and Ubuntu).

1. `git clone https://github.com/undecuple/gitfurl.git && cd gitfurl`
2. `python -m gitfurl cli doctor` reports Wine, tkinter, bubblewrap and umu-run.
3. `python -m gitfurl`, press **+ New prefix**, name it, open it.
4. Pick your installer, read the plan, press **Run installer** and confirm.
5. Gitfurl shows the exit status and where the log is.

Terminal instead: `python -m gitfurl cli --help` (same code, same approvals).

## When you probably do not need it
- If you are happy running Wine yourself, you do not.
- Gitfurl is for the installer step. It is **not** a game launcher or a library manager, and it does not claim to run any particular installer.
- Wine prefixes are not sandboxes. Gitfurl makes the run visible and tidy; it does not make an untrusted installer safe.

## Read more
| Topic | Where |
|---|---|
| What it is for, every option, screenshots, the command line | [docs/user-guide.md](docs/user-guide.md) |
| What it does and does not do; who it is for | [docs/scope.md](docs/scope.md) |
| Tested setups, known issues | [docs/status.md](docs/status.md) |
| How the core, app and progress reading fit together | [docs/architecture.md](docs/architecture.md) |
| Building, tests, the sandbox wrapper, repository layout | [docs/development.md](docs/development.md), [docs/testing.md](docs/testing.md) |
| Docker image and compose file (experimental) | [docs/docker.md](docs/docker.md) |
| What is read from `/proc` and what is not | [SECURITY.md](SECURITY.md) |
| Plans for later | [ROADMAP.md](ROADMAP.md) |

## Privacy
Nothing goes online from gitfurl itself: no network requests, no telemetry, no uploads. Two things you start yourself can use the network: umu-run downloading its runtime on first use, and anything the installer does. Prefixes live in `~/.local/share/gitfurl/prefixes/` and logs in `~/.local/state/gitfurl/logs/` (readable only by you; they can hold your user name and local paths, so remove those before sharing one). Nothing is written to `~/.wine`.

## Community
[![Discord](https://img.shields.io/badge/Discord-join-5865F2?logo=discord&logoColor=white)](https://discord.gg/W8fMT78Q7k)

Questions and discussion: the gitfurl forum on the [undecuple Discord server](https://discord.gg/W8fMT78Q7k). Bugs go to [GitHub issues](https://github.com/undecuple/gitfurl/issues/new/choose), security problems to [private advisories](SECURITY.md). Remove your user name and local paths from logs before posting them. Contributing: [CONTRIBUTING.md](CONTRIBUTING.md).

## Related projects
[Wine](https://www.winehq.org/) (the compatibility layer gitfurl drives), [umu-launcher](https://github.com/Open-Wine-Components/umu-launcher) with [Proton](https://github.com/ValveSoftware/Proton) and [GE-Proton](https://github.com/GloriousEggroll/proton-ge-custom) (the optional runtime; gitfurl ships none of them) and [vim-tictoc](https://github.com/adonaldson/vim-tictoc) (the colour scheme). Gitfurl is not affiliated with these projects or with any software publisher.

## Licence
Copyright (C) 2026 undecuple. GPL-3.0-or-later; see [LICENSE](LICENSE). The images in `assets/` are generated by `tools/make_assets.py` and are under the same licence. The licence covers gitfurl only, not any installer or software you run with it.

## AI use
Gitfurl is written with AI coding agents. Much of its code, tests and documentation was written by Claude (Anthropic's model, used through Claude Code) under the maintainer's direction. A second Claude session reviews each change before it goes public, and the maintainer approves every merge, tag and release. The code has not been audited by a security professional (see [SECURITY.md](SECURITY.md)). Read the plan before you approve a run, and report problems through the issue forms.

## Disclaimer
Gitfurl comes with no warranty, as in section 15 of the GPL. You are responsible for what you run and for being authorised to use it. Names of other products belong to their owners; gitfurl is not affiliated with them. Running an installer can harm your system; a Wine prefix is not a sandbox.
