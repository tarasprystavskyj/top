# Agent Git And Worktree Policy

Updated: 2026-05-25

This repository follows the local Petro-line policy in:

```text
C:\python_scripts\.codex_CLI_laptop_local_philosofy\AGENT_GIT_WORKTREE_POLICY.md
```

## Short Rules

- Use one owner branch per non-trivial agent task.
- Worker branches promote to the parent/coordinator branch before `dev`.
- Use PRs or PR-style handoffs for completed production-grade work.
- Use machine-prefixed agent display names in new handoffs, branch slugs, and
  commit trailers:
  - `л_` laptop agent on Taras's Windows laptop;
  - `с_` server/VPS agent;
  - `а_` Arch Linux agent.
- Add agent attribution trailers to new commits:

```text
Agent: <machine-prefixed-agent-name>
Agent-Session: <codex-session-id>
Parent-Agent: <parent-or-coordinator>
Worktree: <absolute-worktree-path>
Task: <short-task-name>
Live-Safety: no live orders, no secrets
```

- Use separate worktrees only when isolation is actually needed: long-running
  runtime, dirty work isolation, side-by-side comparison, or server safety.
- Do not push raw loops, large NPZ files, private state, or live/paper-live
  runtime state to `dev`.
- Before restarting server paper/live processes, backup state and logs.

## Promotion Chain

```text
worker branch -> parent/coordinator branch -> dev -> runtime/server checkout
```

Emergency fixes may skip a level only with explicit human approval and a clear
rollback note.

## Worktree Owner Note

Each long-lived worktree should have a short owner note in a handoff or status
report:

```text
worktree:
branch:
owner agent:
parent/coordinator:
purpose:
safe to delete when:
do not delete because:
```
