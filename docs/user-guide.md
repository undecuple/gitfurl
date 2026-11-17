# gitfurl user guide

gitfurl helps you run a Windows installer you already have on Linux, through Wine, in a separate Wine environment ("prefix") that it creates for that purpose. Compatibility with any given installer is unverified. This guide says what works, what has been tested, and what to do when something goes wrong.

## What gitfurl does and does not do
- It runs an installer file that **you chose**, from your own disk.
- It does not search for, download or bundle installers or games, and it does not remove copy protection, bypass activation or apply cracks. If an installer offers such options, gitfurl does not select them for you.
- It makes no network requests of its own, sends no telemetry and uploads nothing.
- You must be authorised to use the software you run. gitfurl makes no claim that any use is lawful.

## Before you start
You need:
- Linux with Python 3.11 or newer.
- Wine (`wine --version` should work). Wine 11 was used for the testing described below.
- tkinter for the graphical app (Debian and Ubuntu: `python3-tk`; Fedora: `python3-tkinter`; Arch: `tk`). The command line does not need it.
- Optional: bubblewrap (`bwrap`), if you want to run an installer in a stricter sandbox yourself (see "Safer runs").

With Nix, `nix develop` gives you Python with tkinter, and `nix develop .#wine` adds Wine.

## Start the app
    python -m gitfurl

(Once installed with `pip install .`, the command is `gitfurl`.) The window has a sidebar with **Library** and **Settings**.

### Library
The Library lists your prefixes. A prefix is a folder where Wine keeps its own fake Windows drive and registry. gitfurl keeps its prefixes in `~/.local/share/gitfurl/prefixes/` (or under `$XDG_DATA_HOME`) and **never uses `~/.wine`**.
- **+ New prefix** adds a name to the list. The folder is created only when you create it or run an installer.
- Click a prefix, or move to it with Up/Down and press Enter, to open it.
- "ready" means the folder exists. "not created yet" means it does not.

### The window, keys and footer
The window can be resized (smallest size about 900 x 620); the content follows its width and the pages scroll with the mouse wheel. The bar along the bottom shows, from left to right: whether Wine and umu-run were found ("ok" or "missing"), the keys that work on this page, and what gitfurl is doing ("Idle", "Creating prefix", "Installer running" or "Stopping...").

Keys: **Tab** and **Shift+Tab** move through everything you can use, in the order it appears on screen, and the page scrolls to keep the focused item visible. The focused item gets an orange outline (white on the orange buttons): a coloured line, not colour alone. **Enter** or **Space** activates it (open a prefix, press a button, flip a switch, choose an option). In the Library, **Up** and **Down** move between prefixes. **Esc** goes from a prefix's page back to the Library, except while an installer is running: then the page with the Stop button stays in view, as it does for the sidebar. While a run is going, Run and Create are off because their buttons are disabled; that is the only guard against a second start. These keys are for the keyboard; gitfurl does not provide screen-reader support (the toolkit it uses, Tk, cannot).

If Wine is not found, an amber box on the Library and on a prefix's page says so and has a **Check again** button.

### Running an installer
Open a prefix, then:
1. **Installer:** type or browse to the installer file (`setup.exe`). The **Checklist** at the top of the page shows what Run needs, each with a tick or a cross and a sentence: an installer file chosen, Wine (or umu-run for proton) found, and the prefix existing or set to be created. **Run installer**, **Create now** and **Stop** sit in a bar at the bottom of the page that stays in view; when Run is off, the bar says why in words.
2. Read the **Plan**. It shows the exact command, the prefix, the working folder and the environment variable gitfurl will set. Nothing has run yet.
3. Choose options (below).
4. Click **Run installer**. A dialog repeats the plan and the warning. Nothing starts until you confirm.
5. The installer's own window opens. Use it as you normally would.
6. When it ends, gitfurl shows the exit status and where the log is.

If the prefix does not exist and "Create the prefix if it does not exist" is on (the default), gitfurl creates it first. If creating it fails, the installer is not run.

### Options (all off unless stated)
- **Create the prefix if it does not exist** (on by default).
- **Show what the installer changed on my system.** After the run, a window lists new, changed and removed files in the prefix, new registry keys, and changes in a few places outside the prefix (menu entries, autostart, `~/.local/bin`, `~/Desktop`, the top level of your home folder). It lists names only, never contents. It is best effort: it cannot see changes elsewhere on your disk.
- **Detailed log.** Also writes a JSON-lines event log next to the normal log (see "Logs").
- **Find the installed program.** After the run, gitfurl guesses which new `.exe` is the installed program (largest first, ignoring uninstallers, redistributables, crash handlers and Windows folders) and prints a command you can run yourself. It guesses from names and sizes only. It does not run the program, add it to any launcher, or look anything up online.
- **Runtime:** `wine` (default) or `proton`. Proton runs through `umu-run` and needs a Proton folder you already have, given with `--proton-path` or the `PROTONPATH` variable. gitfurl never downloads one. `umu-run` itself downloads the Steam Linux Runtime the first time it runs if it is not already on the machine; the confirmation dialog says so. This was tested only as far as starting an installer in a sandbox.
- **Portable install (reduces changes outside this prefix).** Off by default. Normal install is the default and runs the installer as today. Portable runs the installer inside bubblewrap (`bwrap`) so that, as far as bwrap isolates it, the writable places are the dedicated prefix, a temporary home and `/tmp` that vanish afterwards, the run's log file and `/dev/dri`. The network is off, the environment is filtered to a short allowlist, your home folder, the session and system D-Bus sockets are hidden, and Wine's menu and file-association builder is disabled, so entries in desktop menus, file-type handlers and `~/.cache/wine` are reduced, not guaranteed absent. See "Portable install" below. It is disabled when `bwrap` is not found or the runtime is `proton`, and then it says why.
- **Scan the installer with ClamAV first.** Off by default. See "ClamAV scan" below. It is disabled, with the reason shown, when `clamscan` is not on PATH.
- **Delete the original installer after a good run:** `none` (default), `installer` (just the file) or `folder` (**everything in the installer's folder**, including other files and sub-folders). See "Cleanup".

### Progress
While an installer runs, the Progress card shows how far the installer has read into the large files beside it (its payload), the read rate, an estimated time left, and how much has been written into the prefix. This does not depend on the installer's own progress display, which can be poor.
Limits: it assumes the large files next to the installer are its payload. The estimate is rough and appears only after about 1% has been read. If the installer reads its files in another way, the numbers can be wrong or stay at zero. It reads no file contents. To tell the prefix's processes from other Wine programs it also checks, for your own processes only, for the entry `WINEPREFIX=<this prefix>` in their environment and keeps only the match, the program name and CPU time (see SECURITY.md). The progress line also lists how many processes are running, their CPU use and helper program names, and says when it looks quiet.

### Checks before a run
The dry run and the approval step show a read-only preflight. Everything is advisory: it never blocks a run and never changes anything.
- **Measured:** free space on the disk that will hold the prefix, the total size of the files in the installer's folder (symlinks are not followed; if counting stops early the value is shown as "at least"), memory available, CPU threads.
- **Estimate:** the installed size. gitfurl cannot know how much space an install needs, so it says "unknown" and does not multiply the installer's size. It warns only when free space is smaller than the files in the installer's folder (an estimate: the install may not fit) and when under 2 GB of memory is available (an estimate). You can still go ahead.

### What gitfurl guesses about the installer
Before you approve a run, the preview shows what the installer looks like from a few bytes of its files: an Inno Setup ID string ("likely"), and the FreeArc signature at the start of the installer or of large files beside it ("possible", unverified). This reads files only: nothing is run, opened as an archive or extracted, and the route stays Wine/Proton. (`doctor` separately runs `innoextract --version` if that program is installed.) It never looks inside a payload. A guess can be wrong or missing, and it says nothing about whether the installer will work.

### Stopping a run
While an installer runs, the **Stop** button (and **Ctrl-C** on the command line) stops it. A confirmation explains what happens, with "No" as the default.
1. gitfurl sends a polite stop (SIGTERM) to the programs of the run it started, and only those: the installer is started in its own process group and nothing is ever found by name.
2. It waits 10 seconds, then forces (SIGKILL) whatever is left in that same group.
3. With Wine (not Proton), it then asks this prefix's own Wine server to exit (`wineserver -k` for that one prefix). That also ends anything you started by hand in the same prefix.

A second **Ctrl-C** while a stop is in progress skips the 10-second wait and force-kills what is left. Ctrl-C is otherwise held back while a stop runs, including while `wineserver -k` runs (up to 30 seconds), so gitfurl can take up to about 45 seconds to return after a stop. A run counts as cancelled only if the stop took effect: before the installer started, or by signalling a group that still had a running program. If the installer had already exited when you pressed Stop, it is reported as finished with its own exit status and nothing is signalled. If `wineserver` cannot be found or exits with an error, the notes say so.

Stopping while the prefix is still being created (before the installer starts): in the graphical app, Stop does not interrupt that step; it finishes, and then the installer is not started and the run is shown as cancelled. On the command line, Ctrl-C ends gitfurl at once with status 130, and Wine's own processes for that prefix (`wineboot` and its helpers) may be left running; this step is not run in its own process group and gitfurl does not stop it.

The run is shown as **cancelled**, not as a success or a failure. On the command line the exit status is 130 and the event log has a `cancelled` event. The log is kept. The original installer is never deleted after a cancelled run.

What may remain, because nothing is deleted: files that were already copied into the prefix, registry changes, files written elsewhere on this machine (for example in your home folder), and any program that moved itself out of the run's process group. A group stop cannot reach such a program, so complete process-tree termination is **not guaranteed**: check with `ps` if something looks left over. For Proton, stopping through `umu-run` is **unverified**: it needs umu's runtime, which could not be set up offline, so whether the runtime's own container always ends with the group is untested.

### Settings
Shows Wine and bubblewrap versions and the platform, and the safety note.

## Command line (optional)
The graphical app is the default. The command line is opt-in and uses the same code and the same approvals:

    gitfurl cli doctor
    gitfurl cli install /path/to/setup.exe --dry-run --prefix NAME
    gitfurl cli prefix create NAME
    gitfurl cli prefix list
    gitfurl cli install /path/to/setup.exe --prefix NAME [options]

Options for `install`:

| Option | Meaning |
|---|---|
| `--dry-run` | Show the plan and change nothing. |
| `--prefix NAME` | The prefix to use (needed for a real run). |
| `--create-prefix` | Create the prefix first if it is missing. |
| `--yes` | Approve without the typed `yes` prompt. Without a terminal and without this, nothing runs. |
| `--progress` | Print progress lines while the installer runs. `--progress-interval SECONDS` sets how often (default 10). |
| `--detailed-log` | Also write the JSON event log. |
| `--stall-after SECONDS` | Off unless given. Print one advisory note if nothing moves for that long: no growth in how far the installer has read into its payload files, no CPU use by the prefix's processes, and no growth of the log. It never stops or signals anything. Slow phases (a hash check, one huge file) can look like this, so choose a long time such as 600. Also turns on the progress monitor (the `/proc` reads described above). |
| `--wine-debug CHANNELS` | Set `WINEDEBUG` for the run, for more output in the log (for example `err+all`). |
| `--show-changes` | List what the installer changed afterwards. |
| `--portable` | Portable install: run the installer in bwrap (see "Portable install"). Off by default. `--dry-run` prints the full bwrap argument list. Needs `bwrap` on PATH and the wine runtime; without them it stops and does not fall back to a normal install. |
| `--find-exe` | Guess which new program was installed (local only). |
| `--scan` | Scan the installer and the files beside it with the `clamscan` on PATH before anything runs (see "ClamAV scan"). Off by default. A match or a scan error stops the run. `--dry-run` prints the scan command. |
| `--allow-scan-hit` | With `--scan`: run even if ClamAV matched a signature. Refused without `--scan`. A scan error is never overridden. |
| `--scan-timeout SECONDS` | With `--scan`: stop the scan after this long (default 600); a timeout stops the run. |
| `--runtime wine\|proton` | Runtime to use (default `wine`). `--proton-path DIR` or `PROTONPATH` names the Proton folder. |
| `--cleanup installer\|folder` | Delete the installer (file or folder) after exit status 0. |

Exit status: 0 on success, 1 on an error or a non-zero installer status, 2 when not approved.

## Logs
- The log is in `~/.local/state/gitfurl/logs/` (or under `$XDG_STATE_HOME`), one file per run, readable only by you. It holds the command, the prefix, Wine's output and the exit status. It never holds your environment variables.
- It can contain your user name and local paths. Remove them before sharing a log.
- With **Detailed log**, `<name>.events.jsonl` has one JSON object per line: start (command, prefix, names of environment variables set), progress samples, exit status and duration. It holds no environment values. With the progress monitor running (`--progress`, `--stall-after`, or the app) it also has a `processes` event (how many programs run in the prefix, and the names other than Wine's own background ones) and a `memory` event (`MemAvailable` from `/proc/meminfo`) with each sample, and one `possibly_stalled` event if the advisory fired. The last event (`exit` or `cancelled`) holds three separate facts: `started` (the installer process was created), `extraction_observed` and `exit_status`. Without the monitor, `observation_measured` is false and `extraction_observed` stays false because nothing was measured. The event log has no size cap: a long run with a short progress interval adds one line per sample, so delete old logs yourself. Program names in it and in the progress line come from the installer; the progress line shows only their printable characters. A program counts as "in the prefix" when its environment has this prefix's `WINEPREFIX`; whether that can be read for programs inside a portable (bwrap) run has not been checked.

**What "observed activity" means.** `extraction_observed` is a heuristic and the end text calls it "observed activity", never "extracted". It is true only if the monitor saw the prefix grow by at least 1 MiB and a program read at least 1 MiB into the files beside the installer. It can miss a short burst between two samples, and it can be true for an installer that reads and writes but fails. It does not show that anything was extracted or installed. The exit status is the installer's own; as before, 0 does not prove the install is complete.
- Logs stay on your machine. gitfurl uploads nothing.

## Cleanup
Deleting the original installer is off by default. `folder` mode **deletes everything in the installer's folder**, not just the installer. When on:
- It happens only if the installer exits with status 0. A status of 0 does **not** prove the installation finished correctly, so keep the files until you have checked the result.
- The dialog lists exactly what will be deleted before you approve.
- Symlinks are removed as links and never followed.
- It refuses your home folder, general-purpose folders directly under it (`Downloads`, `Desktop`, `Documents`, `Music`, `Pictures`, `Videos`, `Public`, `Templates`, `Games`, `Projects`), the filesystem root, mount points, system folders (`/usr`, `/nix`, `/var` and similar), folders holding gitfurl's own data, and folders with more than 200 entries. Put an installer in its own folder to use `folder` mode; `installer` mode works anywhere that is not a system folder.

## Portable install
Two modes per run. **Normal install** (default) is the installer's usual behaviour inside the dedicated prefix. **Portable install** wraps the same Wine command in `bwrap`:
- Writable: the dedicated prefix, a temporary home folder and a temporary `/tmp` (both in memory; they vanish when the run ends), the run's log file (gitfurl opens it outside and the installer's output goes into it) and the GPU device `/dev/dri`.
- Environment: the sandbox starts with an empty environment. Only `PATH`, `LANG`, `LC_*`, `LANGUAGE`, `TZ`, the display variables, the temporary home and XDG folders and the plan's own variables (`WINEPREFIX`, `WINEDLLOVERRIDES`, and `WINEDEBUG` if you set it) are set, so tokens, keys and agent sockets in your environment are not passed to the installer.
- Input: in the command line the installer shares your terminal; it runs in a session of its own (`--new-session`), which stops it from typing into your terminal.
- Network off. An installer that needs the network, for example to download its files, will fail.
- Your home folder is replaced by an empty temporary one, and `HOME` and the XDG folders point into it. The session and system D-Bus sockets are hidden.
- The installer's folder is mounted read-only, **all of it**, so keep the installer in a folder of its own. The rest of the file system is mounted read-only, so the installer can still read it.
- Wine's `winemenubuilder.exe` is disabled (`WINEDLLOVERRIDES=winemenubuilder.exe=d`, shown in the plan). Without it Wine does not create menu entries or file associations.
- The X11 or Wayland socket (and your `XAUTHORITY` file, or `~/.Xauthority`, read-only, so the cookie can be read inside) is allowed so the installer window can show. A hostile installer can use that connection. Audio is not provided and GPU access is limited to `/dev/dri`; other devices are not available.
- If the prefix has to be created first, that step (`wineboot --init`) runs outside bwrap.
- Stop signals bwrap's own process group, not the installer's: the sandboxed programs run in a session of their own (`--new-session`), and `--die-with-parent` plus the private PID namespace end them when bwrap ends. It was checked with real bwrap and a dummy `sleep`, and once with a real Wine installer in `tools/sandbox.sh`, not with every installer. `wineserver -k` is not run, since the sandboxed Wine server lives in a private `/tmp`.
- If you turn on the changes report, it covers the checked places only and says nothing about places gitfurl did not check.
- Not supported with the `proton` runtime, as root, or when wine itself is installed under your home folder (hidden inside the sandbox).

This reduces what an installer changes outside its prefix, as far as bwrap isolates it. It is **not a guarantee** and not a security boundary: a Wine prefix and bwrap do not contain a hostile installer, and some other host sockets and files remain reachable. **The prefix is kept** after the run, and whatever the installer put there (startup entries in the registry, replaced DLLs, links under `drive_c/users`) runs again, outside any sandbox, whenever you run something in that prefix later. Delete the prefix if you do not trust the installer. Whether an install keeps everything inside the prefix has not been checked against a complete real install. gitfurl's own check that the run has ended cannot see programs inside the sandbox; it relies on bwrap ending them.

## ClamAV scan
Optional and off by default (`--scan`, or "Scan the installer with ClamAV first" in the app).
- **What it runs.** The `clamscan` program already on your PATH, nothing else: `clamscan --no-summary --stdout FILE...`, with the absolute path of the installer and of each regular file directly in its folder. Subfolders and symbolic links are not scanned. It is read-only: gitfurl never passes `--remove`, `--move` or `--copy` and writes nothing into the installer's folder. The command is shown in the plan, the dry run and the approval, and the scan runs only after you approve. It runs in its own process group with a timeout (600 s by default); a timeout, Stop or Ctrl-C ends that group only.
- **The database is yours.** gitfurl never runs `freshclam`, never downloads signatures and never installs ClamAV. Keep the database current yourself (for example with `freshclam` or your distribution's service). The approval and `gitfurl cli doctor` show the database date from `clamscan --version` (a short version check, as for Wine) and warn when it is older than 7 days. The dry run does not run that check.
- **A match stops the run.** Nothing is run or created. The message names the matched files and signatures. ClamAV often flags game installers and repacks falsely, because packers and self-extractors look like malware to signatures. To run it anyway: `--allow-scan-hit` on the command line, or answer Yes to a second dialog in the app (its default is No).
- **An error also stops the run.** `clamscan` missing, no database, a timeout, an exit status other than 0 or 1, or output gitfurl cannot read: the run stops with the reason. gitfurl never falls back to running the installer unscanned.
- **What a clean result means.** "No known signature matched (ClamAV, database dated ...)". It does **not** mean the installer is safe or free of malware: it only means that none of the signatures in your database matched. The result is written to the run's log.

## Safer runs
A Wine prefix is **not** a sandbox. The installer can read and write anything your user can. Variables starting with `WINE` in your shell are not passed on, so the plan shows what is set. For an installer you do not fully trust:
- use a separate user account or a virtual machine, or
- use the sandbox wrapper in `tools/sandbox.sh`, which uses bubblewrap with the network off, an empty home folder and the installer folder mounted read-only. It is a manual script, tested on NixOS only, and it opens no window: use it to check that an installer starts. With `SANDBOX_ETC=1` it also shows a few read-only `/etc` files and `/sys`, so its "empty home" is weaker then. See `docs/development.md`.
Never run an installer, or gitfurl, as root. Gitfurl refuses to.

## What has been tested
- The code is covered by tests that use mocks. They need no Wine and no installer.
- In a no-network bubblewrap sandbox, two real installers were **started** under Wine 11 and stayed running with their windows open for 120 seconds, and one was started under Proton (GE-Proton) through umu-launcher. Each run was stopped after that time.
- **Not tested:** a complete installation, running an installed game, or any compatibility claim. Treat gitfurl as unverified for any particular installer.

## Troubleshooting
- **"The graphical interface needs tkinter"** Install your distribution's tkinter package, or use `gitfurl cli`.
- **"wine not found on PATH"** Install Wine, then check `gitfurl cli doctor`.
- **A `.exe` does nothing, or Wine prints "no driver could be loaded"** There is no display. Run it inside a graphical session.
- **Run installer is greyed out** Choose an installer file and a valid prefix name (letters, digits, `-` and `_`, up to 64 characters).
- **"prefix path already exists"** gitfurl will not touch an existing folder when creating a prefix. Pick another name, or remove the folder yourself.
- **Progress stays at 0%** The installer may keep its payload somewhere else or read it differently. The run is unaffected.
- **The installer exited with a non-zero status** The number does not say why. Read the log; `--wine-debug err+all` adds detail.

## Reporting a problem
Open an issue and use the bug form. Do not attach installers, game files, keys or archives. Remove your user name and paths from logs first. Security problems go through a private advisory, not an issue (see `SECURITY.md`).
