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

## Data Collection Objective

The current priority is to get enough data for a real portfolio-router test.
The first candidate basket is:

```text
IDOL
FREEDOMMONEY
MAXXING
SUP
```

Before repeating proxy reports, check whether 1m 1y NPZ exists for each symbol.
Known present files:

```text
DB/fast_cache_1m_freedommoney_1y_bingx.npz
DB/fast_cache_1m_maxxing_1y_bingx.npz
```

For missing symbols, use only the existing fetcher:

```bash
python3 obw_platform/scripts/fetch_backfill_ohlcv_npz_from_now_v1.py \
  --input-csv obw_platform/meta_strategies/akela_meta_short/data/universe_<symbol>_1m_1y.txt \
  --timeframe 1m \
  --back-bars 525600 \
  --exchange bingx \
  --ccxt-symbol-format usdtm \
  --npz-out DB/akela_meta_short_1m_1y_<symbol>_bingx.npz \
  --feature-set none \
  --cache-pack-trend
```

Do not invent a new fetcher, exchange model, backtest model, fee model, or
slippage model. If BingX fails, record the exact failure and cool down before
retrying. A clean failure report is progress; repeating the same blocked
request every cycle is not.

## Automatic Next Step Rule

If all four first-basket datasets exist, stop spending the main iteration on
proxy-only repetition and move to basket validation.

Required first basket:

```text
IDOL/USDT:USDT          DB/akela_meta_short_1m_1y_idol_bingx.npz
FREEDOMMONEY/USDT:USDT  DB/fast_cache_1m_freedommoney_1y_bingx.npz
MAXXING/USDT:USDT       DB/fast_cache_1m_maxxing_1y_bingx.npz
SUP/USDT:USDT           DB/akela_meta_short_1m_1y_sup_bingx.npz
```

Basket validation ladder:

1. Create a timestamped report directory under `_reports/akela_meta_short/`.
2. Run one per-symbol smoke backtest with the existing backtester:

   ```bash
   python3 obw_platform/backtester_dual_long_short_fast_pack_v2.py \
     --cfg obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml \
     --npz <symbol_npz> \
     --symbol <symbol> \
     --plots <run_dir>/<symbol_slug> \
     --export-curves <run_dir>/<symbol_slug>/curves.csv
   ```

3. Save stdout/stderr logs and any generated summaries/curves.
4. Build a basket report from existing backtester outputs only. Do not invent
   portfolio accounting. If curves align, compute an equal-weight basket curve;
   otherwise report per-symbol results and mark basket curve as unavailable.
5. Write the result to
   `obw_platform/meta_strategies/akela_meta_short/reports/latest_basket_summary.md`.
6. Commit only files under `obw_platform/meta_strategies/akela_meta_short/`.

Promotion rule: the selector is interesting only if basket risk-adjusted MTM is
better than the weakest individual symbol and failures are explainable. Do not
promote based on one per-symbol winner.

## What Counts As Progress

- Better evidence that a candidate symbol appears across independent windows.
- A missing 1y candidate NPZ was fetched or a precise exchange/IP failure was
  recorded.
- A per-symbol or basket backtest report was produced from existing backtester
  outputs.
- A selector rule that improves short-leg risk adjusted results out of sample.
- A cleaner data manifest or reproducible dataset builder for missing windows.
- A report that falsifies a weak idea and narrows the next search.

## What Does Not Count

- One impressive single-window backtest.
- A new optimistic backtest model.
- A change that silently loosens drawdown, unrealized loss, or margin-call risk.
- A large report dump with no conclusion.
