# Working rules for AI agents

Read this before changing anything. Humans: this is a short summary of the project rules.

- **Scope.** Gitfurl runs a Windows installer the user already has, in a dedicated Wine or Proton prefix. It does not search for, download or bundle installers or games, and it does not add cracks, key tools or activation bypasses. See `docs/scope.md`.
- **Safety first.** Nothing runs, is created or is deleted without an approval that shows the plan. Keep dry runs, argument lists (never `shell=True`), local logs with mode 0600, and the cleanup guards. Do not add network code.
- **Tests.** Use mocks and synthetic fixtures. Do not run a real installer unless the maintainer has approved those exact files, and then only in `tools/sandbox.sh` with no network. No installer, game file, archive or key goes into the repository (the pre-commit hook and CI block them).
- **Processes.** Stop only processes you started (own process group). Never kill by name. Use `xvfb-run -a`, not a fixed display.
- **Commits.** The author is the noreply identity. Changes reach `main` only through a pull request merged on GitHub (squash), so GitHub is the committer of the merged commit. Do not add `Co-Authored-By`, session links or other AI attribution to commits or pull requests. AI use is disclosed once, in the README's "AI use" section; do not add it elsewhere.
- **Tool versions.** Check with the versions CI pins (`ruff==0.11.13`, `pytest==8.3.5`) before pushing.
- **Public steps.** `main` is protected: a pull request is required, CI and CodeQL must pass, and only squash merges are allowed; nobody pushes to it directly. Pushing a branch, opening or merging a pull request, tags, releases and workflow-file changes wait for the maintainer's approval naming the exact head.
- **Do not state more than was tested.** Say what was run and when; mark anything else unverified.
