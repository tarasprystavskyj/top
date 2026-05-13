# Team

This project uses human-owned responsibility with AI-assisted execution.

## People

### Taras

- Role: CTO and team lead.
- Responsibility: final technical direction, strategy/research priorities, live-readiness decisions, merge decisions, and project-wide tradeoffs.
- Current main branch of work: `akela-meta-short-worker`.

### Tetiana

- Background: 3 years as PM and HR, technical education.
- Project direction: frontend and design.
- Current main branch of work: `ui-admin`.

## AI Agents

Most implementation work is expected to be done by agents:

- Codex Pro CLI.
- Gemini 3 free.
- Claude CLI 20 USD.

Agents must not be anonymous in the project history. Every agent-assisted commit should identify:

- the human responsible for the work;
- the agent/tool that produced or materially edited the change;
- a resumable dialogue/session reference when available.

## Commit Attribution

Humans should commit with their own name. If an agent prepares the patch, the commit body should include an `Agent:` line.

Recommended commit footer:

```text
Human: Taras <role: CTO/team lead>
Agent: Codex CLI <resume/session: ...>
```

or:

```text
Human: Tetiana <role: frontend/design>
Agent: Claude CLI <resume/session: ...>
```

If no resumable session link exists, write the local session/log reference or `not available`.

## Branch Ownership

- `ui-admin` — UI/backend-for-UI work. Owned by Tetiana/Taras; agents may implement UI tasks there.
- `akela-meta-short-worker` — strategy research, Akela, V21 configs, tuners, symbol ranking, and backtest workflows. Owned by Taras.
- `main` — stable base only; do not use as a scratch branch.

Before significant work, create or confirm a branch. Before a risky agent run, make a commit or backup branch.

## Minimal Human Workflow

1. Check branch: `git status -sb`.
2. Ask the agent what files it plans to touch.
3. Keep UI work and trading/research work on separate branches.
4. Commit stable checkpoints.
5. Push stable checkpoints.
6. Do not commit `.env`, secrets, DB/NPZ files, `_reports`, `_handoff`, or `runs`.

Humans can ask agents for details instead of memorizing process. The purpose of this document is only to keep ownership and responsibility explicit.
