# Status and known issues

What has been tried, on which setup, and what is known not to work. The [testing notes](testing.md) have the details of each run.

## Status

Last updated 2026-11-23 (UTC). On Linux x86_64, Wine 11.0, tested headless (no GPU, a virtual display):

| Setup | How it runs | State | Tested on |
|---|---|---|---|
| Unit tests | `pytest`, mocks only | verified | passing at the last commit; see [docs/testing.md](testing.md) |
| Graphical app | tkinter | verified (renders and starts) | headless screenshot; never used by a person on a real desktop here |
| Installer start, Wine | `gitfurl cli install`, in a no-network bubblewrap sandbox | verified: two user-supplied installers started and stayed open 120 s | Wine 11.0; then stopped. No install was completed. The installers are not published (see [docs/testing.md](testing.md)) |
| Installer start, Proton | `umu-run` with GE-Proton, same sandbox | verified: one installer started and stayed open 120 s | one error line in Proton's output, source not found |
| Complete install | Wine, `--unattended` | verified with installers built for the test (Inno Setup 7.1.1: random payload, a custom page, `[Run]` entries), in the sandbox, from the command line and from the window | Wine 11.0, 2026-11-17 and 2026-11-18; no third-party installer completed |
| Running an installed program | any runtime | **not tested** | - |
| Progress numbers on a real install | `/proc` read offsets | implemented | tested only against fake `/proc` data |
| Proton from the app | runtime setting | implemented | not run end to end |
| ClamAV scan (optional) | `clamscan` on PATH | implemented | tested with a stand-in `clamscan` only; a real `clamscan` was never run |
| Memory limit (on by default) | `systemd-run --user --scope` | implemented | stand-ins and test programs in a real scope (systemd 262); Wine with the test installer in the sandbox inside a scope with the limits: over the limit it moved to swap and was not stopped. Not Proton, not gitfurl's own scope around Wine |
| Docker image and compose file | container, [docs/docker.md](docker.md) | experimental: the image builds, `cli doctor` runs and the window opens | Podman 6.1, 2026-11-16; not Docker itself, and no installer run in it |
| FreeArc inspection (read-only) | the plan reads an archive's own index; nothing is extracted by the inspection itself (the route is chosen separately, see the native route below) | implemented | synthetic archives built in memory by the tests; on 2026-11-19 a read-only parse of 15 archives made in the sandbox with a FreeArc-compatible tool. Not checked against archives written by the original FreeArc |
| FreeArc extraction: 4x4 (lzma inside) and srep, alone or in chains | `gitfurl cli extract` | implemented | test vectors, synthetic streams; on 2026-11-20 (UTC), 47 archives made in the sandbox with seb3773/freearc-c and srep 3.93a built from source extracted byte for byte, 49 standalone srep streams decoded, 2 archives with an unhandled method refused. Not checked against archives written by the original FreeArc or third-party installers; srep blocks needing more than 1 GiB of memory are refused |
| FreeArc extraction (storing, lzma, exe, delta, rep, and chains of them) | `gitfurl cli extract`, gitfurl's own reader; the installer is not run | implemented | synthetic archives built by the tests; on 2026-11-19, 8 storing/lzma archives made in the sandbox with seb3773/freearc-c extracted byte for byte (6 others correctly refused); later that day, 15 more archives made the same way using these methods (storing, exe, delta, rep and lzma, alone or chained; two of them are byte-identical) extracted byte for byte, and 5 with methods gitfurl does not handle refused. Not checked against archives written by the original FreeArc or third-party installers; no window option |
| Native route for `install` (CLI) | `gitfurl cli install` picks native or Wine/Proton itself and says why; `--route` overrides; on native, Setup is not run; the automatic choice needs archives linked to the installer by name and no Setup-only option | implemented | unit tests with a synthetic Inno Setup file and archives built by the tests (2026-11-19). In the sandbox on 2026-11-19 (22:20-22:48) with a maintainer-approved synthetic installer: only the Wine fallback was exercised (its archives do not match their checksums, and nearly all of their data uses a method gitfurl does not handle); creating the prefix was held up by Wine's Mono install dialog until the 300 s timeout (the native route now turns those dialogs off for the prefixes it creates; not rerun); the Ctrl-C path passed. On 2026-11-20, in the sandbox, with a second synthetic installer built for the test (Inno Setup 5.5.9, two FreeArc archives using `rep+exe+delta+lzma` and `lzma`) and gitfurl at 3e24106f: the automatic choice was Wine (the archives' names are not linked to the installer's), and `--route native` wrote all 70 files, each matching its source by sha256. Not rerun since #25 and #26 |
| Native route (window) | - | not yet: the window always runs Setup and says when the command line would have used the native route | pure-function test of the route line only |
| Inno Setup reader in the plan (read-only) and archive-name preflight | the plan reads the setup's own header blocks and entry lists; nothing is run or extracted; the route choice is unchanged | implemented | synthetic setups built by the tests; on 2026-11-20 (UTC) a read-only parse, in the sandbox, of 17 setups made with Inno Setup 5.5.9, 6.3.3, 6.7.4 and 7.1.1 (16 read, 1 fully encrypted refused) and of one approved modified 5.5.0 setup (all 39 stored files' SHA-1 matched; three `[Code]` file names reported missing) |
| Inno Setup extraction (`{app}` files stored in setup.exe) | `gitfurl cli extract-inno`, gitfurl's own reader; the installer is not run | implemented | synthetic setups built by the tests; on 2026-11-20 (UTC), in the sandbox, the 10 setups of the set above without a password or disk spanning were extracted into a stand-in prefix and every file matched its stored hash and the source file's sha256 in the test-data manifest (first 16 hex digits compared); password, encrypted and disk-spanning setups were refused. Not run under Wine, not with third-party setups |
| Flatpak | - | planned | - |

"verified" means a run with a named setup. "implemented" means the code is in but not run there. "planned" means neither.

## Known issues
- Compatibility with any particular installer is unverified.
- Progress and the "installed program" guess are heuristics. They can be wrong or show nothing for installers that read their files in another way.
- The app is dark only, with a fixed pixel font. Tab and Return/space work on buttons, switches and the sidebar (added 2026-11-16, lightly tested). Screen readers cannot read it: Tk does not expose its widgets to them.
- Gitfurl refuses to create prefixes or run installers as root.
- The memory limit is on by default. Without a user systemd manager, or without the memory controller delegated to it, runs are refused until you turn the limit off (`--no-memory-limit`, or the option in the window). Swap is not limited.
- Proton needs a Proton folder you already have, and umu-run downloads its own runtime on first use if it is missing.
- The single-file Linux build in the 1.0.0 release starts and runs `cli` here, and its window opens in a Debian image with Tk and fonts. The 1.1.0, 1.2.0, 1.2.1 and 1.3.0 builds are made by the same workflow and have not been checked separately. It is **not proven on every desktop**: it expects the system's standard X11 libraries and at least one font. On NixOS it stops with `libxcb.so.1: cannot open shared object file` unless the libraries are provided (for example with nix-ld) or you run from source with `nix develop`. See the [roadmap](../ROADMAP.md).

