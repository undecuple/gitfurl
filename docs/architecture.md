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
- `portable.py`: pure functions that build the bwrap argument list and the plain-text description for the optional portable install (no process started, nothing written); `runner.py` wraps the Wine argv with it only when `InstallPlan.portable` is set.
- `preflight.py`: advisory checks shown before a run (measured values apart from estimates, never blocking).
- `runner.py` `RunControl`: stops the run gitfurl started, through its own process group, under a lock; nothing is signalled after the run has ended.
- `progress.py`: progress from `/proc` (how far processes have read into the payload files) and the prefix size.
- `widgets.py`, `pixelfont.py`: the app's rounded widgets and block letters.
- `theme.py`: colours, spacing, type sizes, the corner radius and the contrast calculation (no tkinter, so it is tested without a display).
