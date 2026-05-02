# codex-workflow

## Stage

seed

## What This Line Is

Rules for giving AI/Codex workers narrow tasks without letting them damage unrelated parts of the repo.

## Current Shape

The repo already contains task artifacts and patch-style delivery precedent. The process exists, but scope discipline must be forced. A vague task is expensive because the repo has too many adjacent surfaces: UI, live runner, backtester, cache, YAML configs, and deploy.

## Required Task Shape

Every Codex task should include:

- Title.
- Repo.
- Base branch.
- Branch to create.
- PR target.
- Context.
- Goal.
- Allowed files.
- Forbidden files.
- Exact test/validation commands.
- Acceptance criteria.
- Required report-back format.

## Default Forbidden Files for UI-only Tasks

- `obw_platform/**`
- trading/backtest scripts in repo root
- strategy YAMLs unless explicitly listed
- DB/NPZ/cache/session files
- `.env*`
- deploy secrets

## Drift Risks

- One task touches UI, backend, trading logic, YAML, and deploy at once.
- Worker “cleans up” files outside the requested scope.
- No acceptance criteria, so the worker reports subjective success.
- No test command, so the result is not verifiable.
- Diff becomes too large to review.

## Next Useful Move

Use one strict task template for every AI/Codex request. Reject tasks that do not state allowed files, forbidden files, and validation command.
