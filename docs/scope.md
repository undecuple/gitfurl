# Scope

Status: first release (1.0.0). Compatibility is unverified. Planned support is not verified support.

## Problem
Some Windows installers are hard to run on Linux. Gitfurl helps a user run an installer they already have, in a dedicated Wine prefix, with settings they can inspect, and keeps local logs.

## MVP journey
1. Run an environment check (`gitfurl doctor`).
2. Choose a local installer.
3. Preview the planned command and prefix (`--dry-run`).
4. Explicitly create a dedicated prefix.
5. Explicitly launch the installer.
6. Inspect the exit status and local logs.

## In scope
Local installer files supplied by the user, Wine detection, dedicated prefixes, inspectable launch settings, local logs.

## Out of scope
Searching for or downloading installers or games, bundled installers or assets, keys, cracks, DRM removal, activation bypasses, automatic package installs or sudo, changes to an existing prefix without approval, telemetry, uploads, background network access. Nothing here is a claim that any use is lawful. Users must be authorised to use the software they run.

## Safety and threat model
- A Wine prefix is not a security sandbox. An installer can reach files the current user can reach.
- Installer names, contents and logs are untrusted data.
- Subprocesses get argument lists, never a shell string. Environment overrides apply to the child only.
- Logs stay local and may contain usernames and paths.

## Milestones
- M0: scaffolding and this document. Done when the package imports and `docs/` exists.
- M1: `doctor` and a side-effect-free `install --dry-run`. Done when the mocked tests pass and a dry run creates and runs nothing.
- M2: explicit creation of dedicated prefixes. Written and mock-tested; not run against real Wine.
- M3: installer execution, local logs, exit-status handling. Written and mock-tested; never run against real Wine or a real installer.
- M4: compatibility testing with authorised test material.

## Limitations and open questions
- Wine was not available when M1 was written, so `doctor` was only tested with mocks.
- Interface toolkits (TUI, GUI) are not chosen. See `docs/architecture.md`.
- Licence: GPL-3.0-or-later chosen by the maintainer.
