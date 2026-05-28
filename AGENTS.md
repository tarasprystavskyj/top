# top_1 Agent Rules

All agents working in this repository must read:

- `C:\python_scripts\.codex_CLI_laptop_local_philosofy\PHILOSOPHY.md`
- `C:\python_scripts\.codex_CLI_laptop_local_philosofy\AGENT_GOVERNANCE.md`
- `C:\python_scripts\.codex_CLI_laptop_local_philosofy\AGENT_GIT_WORKTREE_POLICY.md`

Local rules:

- Do not place live orders or read secrets unless the human explicitly asks and
  the task requires it.
- Use one owner branch per non-trivial task.
- Keep worker branches off `dev`; promote through the parent/coordinator branch
  or a PR-style handoff.
- Add agent attribution trailers to new commits.
- Do not push raw loops, NPZ files, or live/paper-live runtime state to `dev`.
- Before restarting server paper/live processes, backup state and logs.

