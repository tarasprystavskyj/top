# Agent Rules

This repository allows agent-written code, but responsibility must stay explicit.

## Identity

Every agent must report:

- agent/tool name;
- human owner for the task;
- branch used;
- resumable dialogue/session reference when available;
- files changed;
- validation run;
- remaining risk.

If the agent makes or prepares a commit, include a footer like:

```text
Human: Taras <role: CTO/team lead>
Agent: Codex CLI <resume/session: ...>
```

Use the human's name for accountability. Use the agent line for traceability.

## Branch Boundaries

- `ui-admin`: UI, UX, design, frontend, and backend endpoints used by UI.
- `akela-meta-short-worker`: Akela, V21 research, configs, tuners, backtests, data collection plans, symbol ranking.
- `main`: stable base only.

Do not mix UI refactors with strategy logic or live-trading logic in the same patch unless the human explicitly asks.

## Safety Rules

- Do not edit live trading/deploy state unless explicitly asked.
- Do not introduce a new exchange, slippage, liquidation, funding, accounting, or backtest model without explicit human approval.
- Do not commit `.env`, secrets, private keys, DB/NPZ files, generated reports, `_handoff`, `_reports`, `runs`, plots, or full transcripts.
- Do not run destructive git commands unless the human explicitly asks.
- Before replacing another agent's work, inspect it and explain what will be changed.

## Reporting Contract

Final reports should include:

1. What changed.
2. Files changed.
3. Validation command and result.
4. What was intentionally not touched.
5. Remaining risk.
6. Next smallest useful move.

Keep this short. Humans can ask for details.

## Commit Discipline

Prefer small stable commits. A good checkpoint commit:

- has a narrow purpose;
- passes at least a relevant smoke check;
- does not include runtime artifacts;
- can be explained in two or three sentences.

When a stable checkpoint exists, tell the human that it should be pushed.
