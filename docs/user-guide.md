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
- Click a prefix to open it.
- "ready" means the folder exists. "not created yet" means it does not.

### Running an installer
Open a prefix, then:
1. **Installer:** type or browse to the installer file (`setup.exe`).
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
- **Delete the original installer after a good run:** `none` (default), `installer` (just the file) or `folder` (**everything in the installer's folder**, including other files and sub-folders). See "Cleanup".

### Progress
While an installer runs, the Progress card shows how far the installer has read into the large files beside it (its payload), the read rate, an estimated time left, and how much has been written into the prefix. This does not depend on the installer's own progress display, which can be poor.
Limits: it assumes the large files next to the installer are its payload. The estimate is rough and appears only after about 1% has been read. If the installer reads its files in another way, the numbers can be wrong or stay at zero. It reads no file contents. To tell the prefix's processes from other Wine programs it also checks, for your own processes only, for the entry `WINEPREFIX=<this prefix>` in their environment and keeps only the match, the program name and CPU time (see SECURITY.md). The progress line also lists how many processes are running, their CPU use and helper program names, and says when it looks quiet.

### Checks before a run
The dry run and the approval step show a read-only preflight. Everything is advisory: it never blocks a run and never changes anything.
- **Measured:** free space on the disk that will hold the prefix, the total size of the files in the installer's folder (symlinks are not followed; if counting stops early the value is shown as "at least"), memory available, CPU threads.
- **Estimate:** the installed size. gitfurl cannot know how much space an install needs, so it says "unknown" and does not multiply the installer's size. It warns only when free space is smaller than the installer's own files (an estimate: the install will very likely not fit) and when under 2 GB of memory is available (an estimate). You can still go ahead.

### Stopping a run
While an installer runs, the **Stop** button (and **Ctrl-C** on the command line) stops it. A confirmation explains what happens, with "No" as the default.
1. gitfurl sends a polite stop (SIGTERM) to the programs of the run it started, and only those: the installer is started in its own process group and nothing is ever found by name.
2. It waits 10 seconds, then forces (SIGKILL) whatever is left in that same group.
3. With Wine (not Proton), it then asks this prefix's own Wine server to exit (`wineserver -k` for that one prefix). That also ends anything you started by hand in the same prefix.

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
| `--wine-debug CHANNELS` | Set `WINEDEBUG` for the run, for more output in the log (for example `err+all`). |
| `--show-changes` | List what the installer changed afterwards. |
| `--find-exe` | Guess which new program was installed (local only). |
| `--runtime wine\|proton` | Runtime to use (default `wine`). `--proton-path DIR` or `PROTONPATH` names the Proton folder. |
| `--cleanup installer\|folder` | Delete the installer (file or folder) after exit status 0. |

Exit status: 0 on success, 1 on an error or a non-zero installer status, 2 when not approved.

## Logs
- The log is in `~/.local/state/gitfurl/logs/` (or under `$XDG_STATE_HOME`), one file per run, readable only by you. It holds the command, the prefix, Wine's output and the exit status. It never holds your environment variables.
- It can contain your user name and local paths. Remove them before sharing a log.
- With **Detailed log**, `<name>.events.jsonl` has one JSON object per line: start (command, prefix, names of environment variables set), progress samples, exit status and duration. It holds no environment values.
- Logs stay on your machine. gitfurl uploads nothing.

## Cleanup
Deleting the original installer is off by default. `folder` mode **deletes everything in the installer's folder**, not just the installer. When on:
- It happens only if the installer exits with status 0. A status of 0 does **not** prove the installation finished correctly, so keep the files until you have checked the result.
- The dialog lists exactly what will be deleted before you approve.
- Symlinks are removed as links and never followed.
- It refuses your home folder, general-purpose folders directly under it (`Downloads`, `Desktop`, `Documents`, `Music`, `Pictures`, `Videos`, `Public`, `Templates`, `Games`, `Projects`), the filesystem root, mount points, system folders (`/usr`, `/nix`, `/var` and similar), folders holding gitfurl's own data, and folders with more than 200 entries. Put an installer in its own folder to use `folder` mode; `installer` mode works anywhere that is not a system folder.

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
