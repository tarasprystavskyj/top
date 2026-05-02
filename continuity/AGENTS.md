# Continuity

This repository has several live layers: research/backtest code in `obw_platform/`, root-level trading utilities and runners, live/paper execution wrappers, DB/NPZ cache builders, a UI in `UI/`, and previous patch/task artifacts such as `multi_agent/` and `deploy_patch_full/`.

The continuity layer exists to keep those layers oriented across AI/Codex/ChatGPT sessions. It is not a transcript archive, not a knowledge base dump, and not a place for generated reports.

## Working rules

- Before work, read `continuity/AGENTS.md`, `continuity/MAP.md`, and only the relevant file from `continuity/lines/`.
- Do not edit trading code, runner code, YAML configs, or UI code from a continuity-only task.
- Keep tasks narrow. One task should not mix UI, live runner, trading-core, cache, and deploy changes.
- Do not copy logs, `_reports`, databases, `.npz`, `.db`, `.sqlite`, secrets, `.env` files, generated plots, or full session transcripts into `continuity/`.
- After meaningful work, update only the relevant line file with: what changed, what drift risk appeared, and the smallest next useful move.
- Prefer blunt current-state notes over polished documentation.
- If a file is historical/noisy, say so. Do not preserve dead paths as if they are active.

## Repository zones

- `obw_platform/` — strategy research, backtest engines, tuners, configs, runners, tests, reports.
- `UI/` — FastAPI backend and Next.js frontend for configs, runs, validation, deploy, and live result inspection.
- root scripts — older or parallel bot/backtest/cache utilities. Treat as potentially active until confirmed.
- `multi_agent/` — previous task artifacts and worker instructions.
- `deploy_patch_full/` — precedent for overlay-style patch delivery.
- `continuity/` — short orientation layer only.

## Output discipline for AI workers

Every worker should report:

1. Files changed.
2. Files intentionally not touched.
3. Test/validation command run.
4. Result.
5. Remaining risk.
6. Next smallest useful move.

If the task cannot be completed, report the blocker directly. Do not invent success.
