# Security

Gitfurl runs a Windows installer you chose, through Wine, as your user.

- **A Wine prefix is not a sandbox.** The installer can read and write anything your user can. Run installers you trust and are authorised to use. For untrusted ones, use a separate account, a VM, or a bubblewrap wrapper with the network off.
- **Dry run first.** `gitfurl cli install FILE --dry-run` shows the command, prefix and environment and changes nothing.
- **Approval.** Creating a prefix and running an installer each need a typed `yes` (or `--yes`). Without a terminal and without `--yes` nothing happens.
- **Never as root.** Gitfurl refuses to create prefixes or run installers as root. Do not run an installer as root to get around a permission problem.
- **No network.** Gitfurl makes no network requests, sends no telemetry and uploads nothing. Logs stay in `$XDG_STATE_HOME/gitfurl/logs` (mode 0600) and may hold your user name and local paths.
- **Your own prefix is not used.** Gitfurl uses its own prefixes under `$XDG_DATA_HOME/gitfurl/prefixes`, never `~/.wine` as the prefix. Wine and the installer can still write elsewhere (for example `~/.cache/wine`, menu entries, file-type handlers). The optional changes report lists some of that.
- **Environment.** Variables starting with `WINE` that your shell sets are not passed to the installer. Gitfurl sets what the plan shows.
- **Stop.** The Stop button and Ctrl-C signal only the process group of the run gitfurl started (SIGTERM, then SIGKILL after 10 seconds), never a process found by name and never another Wine session. With Wine, they then ask the Wine server of that one prefix to exit (`wineserver -k`), which also ends anything you started by hand in that prefix. Nothing is deleted; partial installs can remain. A program that left the run's process group is out of reach, so complete process-tree termination is not guaranteed. For Proton through `umu-run`, stopping is unverified.
- **What detection reads.** Before a run, gitfurl reads the installer file's first bytes and up to its first 32 MiB looking for one text ID, and the first 4 bytes of up to 16 large files beside it. The detection code opens files read-only, does not follow symlinks, never blocks on a pipe, starts no process, and writes nothing. It looks at no more than 1,000 directory entries. Separately, `doctor` runs `innoextract --version` (a bounded version probe with fixed arguments) if that program is on PATH, as it does for `wine`; nothing ever runs it to extract. File names it shows are stripped of control characters. A match is a guess, not a verified format.
- **What the progress display reads.** While an installer runs and progress is on, gitfurl reads `/proc` for processes of your own user only: which file offsets they have open on the files beside the installer, and, to tell this prefix's processes from others, each process's environment, looking for the single entry `WINEPREFIX=<this prefix>`. From those processes it keeps only a match, the program name and CPU time. Nothing else from the environment is kept, shown, logged or sent anywhere, and it reads no file contents.

To report a vulnerability, open a private security advisory on the repository. Please do not use a public issue.

**Cleanup.** The option to delete the original installer is off by default and needs an installer exit status of 0. An exit status of 0 does not prove the install was complete, so keep the files until you have checked the result.

## Status and threat model
Gitfurl 1.0.0 is a first release. Compatibility is unverified and the code has not been audited. Only the latest commit is supported.

What it tries to prevent: a run or a deletion you did not approve, a run in your main Wine prefix, silent network use by gitfurl itself, and a log or report that leaks your environment.

What it does not prevent: a malicious or buggy installer acting as your user. It is not a sandbox. The optional wrapper `tools/sandbox.sh` (NixOS only, manual) and the container files in `docs/docker.md` add layers; neither is a guarantee.

If private vulnerability reporting is off for the repository, open an issue that says only that you have a security report, with no details, and ask for a contact.
