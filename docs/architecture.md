# Architecture

One shared core, thin interfaces. The graphical app (`gui.py`, tkinter) is the default and is started by `app.py`. The command line (`cli.py`) is opt-in through `gitfurl cli`. Both use the same core and safety checks. A TUI may follow.

- `doctor.py`: read-only probes with a timeout.
- `prefixes.py`: name validation and per-user location. Creates nothing.
- `runner.py`: builds an execution plan (argument list, prefix, working dir, child-only environment). `execute` runs it only after approval and writes output to a local log (mode 0600), never the environment.
- `cli.py`: argparse front end. Real actions need an explicit prefix and approval (a typed yes, or --yes). Without a terminal and without --yes they exit with status 2 and change nothing.

A dry run never falls through to real execution.

- `app.py`: entry point. No arguments opens the GUI; `cli ...` runs the CLI.
- `gui.py`: window and confirmation dialogs. Logic that can be tested without a display (`describe_plan`) is kept in plain functions.
- `cleanup.py`: optional removal of the installer after a good run, with the guards in the user guide.
- `changes.py`: optional before and after snapshots of the prefix and a few host places.
- `detect.py`: read-only format detection from file headers (bounded reads, no process started, nothing written). It produces a guess and a route (Wine/Proton, or unsupported if the file cannot be read as a regular file); it never returns a native route in this version.
- `farc.py`: reads a FreeArc archive's own index (footer and directory blocks) with bounded reads and CRC-32 checks, classifies each block's method (`parse_method()`: a chain of supported stages, kept by gitfurl's own names, or "a method gitfurl does not handle"; the archive's text is not kept), and summarises it for the plan. It never decodes data blocks, extracts or writes anything; anything it cannot read is refused with `FarcError`, which `detect.py` turns into a plain reason.
- `extract.py`: native extraction for `gitfurl cli extract`. `plan_extract()` reads every FreeArc archive beside the installer through `farc.py` and checks every name, writing nothing; `extract()` re-reads each index, creates a new destination folder in the prefix and writes the files with `O_EXCL|O_NOFOLLOW` relative to that folder's file descriptor (names are checked through a table keyed by parent folder and case-folded name, so planning is linear in the names' length; at most 64 folder descriptors are held open), decoding each block through `farc_codecs.py`, checking CRC-32 and stopping on a cancel flag or Ctrl-C.
- `farc_codecs.py`: streaming decoders for the methods extraction supports, `storing`, `lzma` (Python's `lzma`), `exe`, `delta` and `rep`, chained right to left as a pull pipeline that reads the block a chunk at a time. Every buffer is capped before the block is read (`stage_plan()`, `chain_memory()` for the plan's memory line); damaged data raises `CodecError` with gitfurl's own wording. Written from clean-room specifications (behaviour only).
- `portable.py`: pure functions that build the bwrap argument list and the plain-text description for the optional portable install (no process started, nothing written); `runner.py` wraps the Wine argv with it only when `InstallPlan.portable` is set.
- `scan.py`: the optional ClamAV scan: builds the `clamscan` argument list (no option that changes files), reads its output conservatively and decides (a match blocks unless overridden; an error, including a match in a scan that also had an error, always blocks); runs the `clamscan` on PATH in its own process group with a timeout. `runner.run_with_prefix` calls it before anything is created when `InstallPlan.scan` is set.
- `memlimit.py`: the memory limit: the budget from `/proc/meminfo`, availability checks (files first, then read-only `systemd-run --version` and `systemctl --user show` when a run starts), the `systemd-run --user --scope` argv that `runner.py` puts outermost, the soft-limit adjustment thread and the end-of-run report for that one unit.
- `preflight.py`: advisory checks shown before a run (measured values apart from estimates, never blocking).
- `runner.py` `RunControl`: stops the run gitfurl started, through its own process group, under a lock; nothing is signalled after the run has ended.
- `progress.py`: progress from `/proc` (how far processes have read into the payload files) and the prefix size; the advisory stall detector and the "observed activity" heuristic (neither acts on the run).
- `widgets.py`, `pixelfont.py`: the app's rounded widgets and block letters.
- `theme.py`: colours, spacing, type sizes, the corner radius and the contrast calculation (no tkinter, so it is tested without a display).
