# ui-admin

## Stage

sprout

## What This Line Is

The administrative UI layer: FastAPI backend, Next.js frontend, config editing, run launching, run history, live result inspection, validation views, and deploy controls.

## Current Shape

The repo has `UI/backend/api_main.py`, frontend pages for configs/runs/grid/live/validation/deploy, backend tests, frontend tests, and API utility code. The UI is already more than a viewer: it can select configs, launch workflows, and expose validation results.

## What Matters

- UI must not secretly own trading decisions.
- Backend defaults must be documented and visible.
- Config editor must show the actual YAML returned by backend.
- Deploy controls must stay local/proxied safely and must not expose secrets to the browser.
- UI-only tasks must not touch `obw_platform/` unless explicitly scoped.

## Drift Risks

- UI bug fixes accidentally modify strategy/backtester behavior.
- Backend endpoint defaults diverge from CLI defaults.
- Monaco/config editor displays stale or empty YAML while backend has correct data.
- Deploy/admin code leaks implementation details or secrets.
- UI pages become inconsistent about which run/session format they expect.

## Next Useful Move

Write a compact UI contract: key endpoints, expected payload fields, active pages, and which backend files may be touched for UI-only tasks.
