# Scope

Status: early release (1.3.0). Compatibility is unverified. Planned support is not verified support.

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

## Hybrid direction (approved in principle 2026-11-16; automatic native-first route confirmed by the maintainer 2026-11-20)
`gitfurl cli install` now picks the route itself and says why, in the dry run and before the approval. It picks the **native route** when the installer is Inno Setup (or is itself a FreeArc archive) and every FreeArc archive beside it reads cleanly, uses only `storing`, `lzma`, `exe`, `delta`, `rep`, `4x4` (with `lzma` inside) and `srep`, and holds only names gitfurl can write safely, and (for the automatic choice) the archives are linked to the installer (its setup script names them all, or they share its name) and no option that only a Setup run uses was given. Setup is then not run at all: Wine only creates the prefix (when asked), gitfurl unpacks the archives itself into `C:\Games\PREFIX`, and the plan lists word for word what is skipped (Setup's own files, shortcuts, registry entries, redistributables and every step of its script). Otherwise it picks **Wine/Proton** and gives the reason (for example "a method gitfurl does not handle", or an archive whose checksums do not match). `--route native|wine` overrides the choice; `--route native` refuses, with the reason and nothing run, when it is not possible. The window picks the route the same way; its "Run Setup instead" switch (off by default) asks for Wine/Proton. The plan preview reads installer headers and the first 4 bytes of a few large files beside it, and for a FreeArc archive its own index (the list of files, read-only). `gitfurl cli extract` remains as a files-only command into an existing prefix. Native extraction covers FreeArc archives whose data uses `storing`, `lzma`, `exe`, `delta`, `rep`, `4x4` (with `lzma` inside) or `srep`, alone or chained. The decoders are gitfurl's own (Python's `lzma` for lzma), written from behaviour-only specifications (clean room: the people who wrote the decoders did not read the original sources); no extraction program is run, and gitfurl never compresses anything. Decoders for closed-source methods are added only from a published format description or an openly licensed reference decoder; otherwise those methods stay "a method gitfurl does not handle" and the route is Wine/Proton. It runs only after one approval of a plan that shows every step, writes into a new folder in a dedicated prefix, never runs the installer or any extraction tool, and is described as "files extracted", never "installed". Encrypted archives and every other method are refused. The out-of-scope list below still applies.

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
- Interface: a tkinter window first, with the command line (`gitfurl cli`) as the opt-in alternative; there is no TUI. See `docs/architecture.md`.
- Licence: GPL-3.0-or-later chosen by the maintainer.
