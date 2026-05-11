# Next Worker Prompt: Akela Meta Short

You are working on the Akela Meta Short research lane.

Repository root:

```text
/var/www/vps2.happyuser.info/top/top_1
```

Branch:

```text
akela-meta-short-worker
```

Your job is to improve the upper-level router that selects symbols and regimes
for the existing short leg. Do not rewrite exchange, slippage, fee, liquidation,
or core backtest math. Ask the human before changing any of those models.

## Productive Loop

1. Read `obw_platform/meta_strategies/akela_meta_short/AGENT_STATE.md`.
2. Run one research iteration:

   ```bash
   python3 obw_platform/meta_strategies/akela_meta_short/akela_meta_iteration.py
   ```

3. Inspect:

   - `obw_platform/meta_strategies/akela_meta_short/reports/latest_summary.md`
   - `_reports/akela_meta_short/latest/`

4. If there is a stable improvement, implement it in this subproject.
5. Commit only files under `obw_platform/meta_strategies/akela_meta_short/`.
6. Do not commit unrelated dirty worktree changes.

## What Counts As Progress

- Better evidence that a candidate symbol appears across independent windows.
- A selector rule that improves short-leg risk adjusted results out of sample.
- A cleaner data manifest or reproducible dataset builder for missing windows.
- A report that falsifies a weak idea and narrows the next search.

## What Does Not Count

- One impressive single-window backtest.
- A new optimistic backtest model.
- A change that silently loosens drawdown, unrealized loss, or margin-call risk.
- A large report dump with no conclusion.
