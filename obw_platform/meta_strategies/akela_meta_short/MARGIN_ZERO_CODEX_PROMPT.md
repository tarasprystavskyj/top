# Akela Margin-Zero Codex Loop Prompt

You are a fresh Codex agent working in:

```text
/var/www/vps2.happyuser.info/top/top_1
```

Branch:

```text
akela-meta-short-worker
```

## Mission

Find V21 parameter configurations for the first Akela basket candidates with:

```text
margin_call_events_total = 0
```

Primary candidates:

| symbol | NPZ |
| --- | --- |
| `IDOL/USDT:USDT` | `DB/akela_meta_short_1m_1y_idol_bingx.npz` |
| `FREEDOMMONEY/USDT:USDT` | `DB/fast_cache_1m_freedommoney_1y_bingx.npz` |
| `MAXXING/USDT:USDT` | `DB/fast_cache_1m_maxxing_1y_bingx.npz` |
| `SUP/USDT:USDT` | `DB/akela_meta_short_1m_1y_sup_bingx.npz` |

Current baseline basket result:

```text
IDOL          +44.19%, MDD -37.75%, margin calls 18
FREEDOMMONEY  +64.28%, MDD -24.09%, margin calls 0
MAXXING      +183.80%, MDD -18.37%, margin calls 8
SUP           -1.05%, MDD -219.88%, margin calls 35
```

The immediate optimization target is risk cleanup, not maximum return. A lower
return with zero margin calls is more useful than an impressive result that can
die in live trading.

## Hard Guardrails

- Do not change exchange, fee, slippage, liquidation, margin, or core backtest
  math.
- Do not create a new backtester, tuner, exchange emulator, or slippage model.
- Use the existing trusted tools:
  - `obw_platform/backtester_dual_long_short_fast_pack_v2.py`
  - `obw_platform/auto_tuner_dual_fast_pack.py`
  - `obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py`
- Do not edit live/deploy files.
- Do not edit production configs in `obw_platform/configs/` unless the human
  explicitly asks. Put experimental configs under:

```text
obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/
```

- Raw logs, curves, and large artifacts belong under:

```text
_reports/akela_meta_short/margin_zero_codex_loop/
```

- Keep raw artifacts small. Do not export curves unless the curve is needed for
  a promoted candidate or a specific diagnosis.
- When a branch is clearly rejected, write the conclusion into the compact
  report first, then delete heavy raw artifacts for that rejected branch. Keep
  generated YAMLs and compact reports until a human confirms they can be
  removed.
- If disk free space is low, stop new expensive runs and report the blocker
  instead of filling the filesystem.

- Commit only stable, compact files under:

```text
obw_platform/meta_strategies/akela_meta_short/
```

Never commit DB, NPZ, raw logs, curves, `.env`, live runtime, or unrelated
worktree changes.

## Productive Strategy

Think before launching expensive full-year runs.

Preferred loop:

1. Read:
   - `obw_platform/meta_strategies/akela_meta_short/reports/latest_basket_summary.md`
   - `obw_platform/meta_strategies/akela_meta_short/reports/latest_champion_search.md`
   - existing candidate YAMLs in `obw_platform/configs/`
2. Pick one weak symbol first, usually `SUP` or `MAXXING`, because they have
   margin calls.
3. Form a small hypothesis about which existing YAML parameters can reduce
   tail risk.
4. Create or update an experimental YAML copy under
   `generated_configs/margin_zero/`.
5. Run short or medium backtests first, for example `--limit-bars 5000`,
   `20000`, or a recent time slice, if that is enough to falsify the idea.
6. Only run full-year confirmation after a candidate looks promising.
7. Record every attempt in a compact report:

```text
obw_platform/meta_strategies/akela_meta_short/reports/latest_margin_zero_codex.md
obw_platform/meta_strategies/akela_meta_short/reports/latest_margin_zero_codex.json
```

8. If you find a zero-margin candidate, compare it against baseline on:
   - `return_mtm_pct_on_start`
   - `mdd_mtm_%`
   - `trades_total`
   - `margin_call_events_total`
   - `bars_in_margin_call`
   - final/tail unrealized exposure if reported

## Suggested Commands

Backtest a generated config:

```bash
python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py \
  --cfg obw_platform/meta_strategies/akela_meta_short/generated_configs/margin_zero/<cfg>.yaml \
  --npz <npz> \
  --symbol <symbol> \
  --limit-bars 20000
```

Run existing tuner when useful:

```bash
python3 obw_platform/auto_tuner_dual_fast_pack.py \
  --cfg <starting_cfg> \
  --npz <npz> \
  --symbol <symbol> \
  --plan obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py \
  --prefix akela_margin_zero_<symbol_slug> \
  --jobs 1 \
  --min-trades 50 \
  --score-mode mtm \
  --max-seconds 1800 \
  --debug
```

If the existing tuner score rejects margin calls with `-1e18`, use that as a
feature: search for parameter regions that escape that penalty. Do not weaken
the penalty by changing tuner/backtester math.

## Output Contract

At the end of each cycle, leave a clear report with:

- what was tried;
- exact commands or raw log paths;
- best current zero-margin candidate, if any;
- why failed attempts failed;
- next concrete action.

If you make a stable improvement or a useful report under this lane, commit it
with a message beginning:

```text
akela margin-zero codex:
```

If there is no useful stable change, do not commit.
