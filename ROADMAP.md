# Roadmap

## Scope
Gitfurl runs a Windows installer you already have, in a dedicated Wine prefix, with settings you can inspect, and keeps local logs. It is for Linux users, and it is experimental.

It does not search for, download or bundle installers or games, and it does not remove DRM, bypass activation or apply cracks. It does not claim that any use is lawful.

## Done
- M0: scaffolding and scope document.
- M1: `doctor` and a side-effect-free dry run.
- M2: explicit creation of dedicated prefixes (mock-tested only).
- M3: installer execution with a local log and exit status (mock-tested only).

## Decided, not started
- **Launcher integration (Heroic, Faugus):** would write into another app's config files, whose format changes. If added: opt-in, shown before it is written, with a way to undo. The installed-program guess is the first half and exists already.
- **Dependency step (for example `winetricks vcrun2022`):** only as an explicit, approved download that says what is fetched and from where. Not done.
- **Cover art / IGDB:** not added. It needs a network call that sends the game's title to a third party, a client secret that cannot ship in a public repository, and attribution. It adds little for an installer-agnostic tool. If wanted later: off by default, the user's own key, a local cache, and the dialog says exactly what is sent.
- **Flatpak:** fits the release plan, with narrow permissions: no `--filesystem=home` (use a portal or one chosen folder), no host-command access, no network. Not started.

## After the first release
- A Docker image and a compose file. Started 2026-11-16: the image builds and starts under Podman; see [docs/docker.md](docs/docker.md). Not yet run with an installer, with Docker itself, or with Proton.

## Next
- M4: test against authorised installers in a bubblewrap sandbox with the network off, and record what happens.
- Graphical app: the default interface (done in a first form). Next: live log view, progress, and a dependency check with proposed commands.
- A TUI on the same core, with optional dependencies.
- Optional pre- and post-install malware scan with ClamAV.
- Sandbox policies (bubblewrap) as a gitfurl option, not only a manual wrapper.
- A runtime choice in the app: Wine by default, Proton through umu-launcher as an option. Both started the same test installer in a sandbox; nothing is known about install results.
- Progress and logging: more detail where the installer allows it.
