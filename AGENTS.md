# Working rules for AI agents

Read this before changing anything. Humans: this is a short summary of the project rules.

- **Scope.** Gitfurl runs a Windows installer the user already has, in a dedicated Wine or Proton prefix. It does not search for, download or bundle installers or games, and it does not add cracks, key tools or activation bypasses. See `docs/scope.md`.
- **Safety first.** Nothing runs, is created or is deleted without an approval that shows the plan. Keep dry runs, argument lists (never `shell=True`), local logs with mode 0600, and the cleanup guards. Do not add network code.
- **Tests.** Use mocks and synthetic fixtures. Do not run a real installer unless the maintainer has approved those exact files, and then only in `tools/sandbox.sh` with no network. No installer, game file, archive or key goes into the repository (the pre-commit hook and CI block them).
- **Processes.** Stop only processes you started (own process group). Never kill by name. Use `xvfb-run -a`, not a fixed display.
- **Commits.** Author and committer are the noreply identity. Do not add `Co-Authored-By`, session links or any other AI attribution to commits, pull requests or files.
- **Tool versions.** Check with the versions CI pins (`ruff==0.11.13`, `pytest==8.3.5`) before pushing.
- **Public steps.** Pushes, tags, releases and workflow-file changes wait for the maintainer's approval naming the exact head.
- **Do not state more than was tested.** Say what was run and when; mark anything else unverified.
