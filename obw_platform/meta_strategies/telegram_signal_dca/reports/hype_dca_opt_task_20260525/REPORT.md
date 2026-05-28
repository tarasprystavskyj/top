# HYPE DCA Opt Task 2026-05-25

Owner: TelegramDcaOps1.

Scope: research-only optimization for Telegram/Binance-copy HYPE signals with BingX-style executable paper fills. No live orders, no secrets, no private scraping, no active loop stops, no dirty source-file overwrite.

## Local And Server State

- Working tree had pre-existing dirty files in `obw_platform/meta_strategies/telegram_signal_dca/` before this task:
  - `replay_full_v21_external_signals.py`
  - `test_telegram_v21_one_leg_wrapper_smoke.py`
- Those dirty files were read only; this task writes only this report and replay outputs under `reports/hype_dca_opt_task_20260525/`.
- Active external hunter process was observed and left running:
  - PID `20488`
  - wave output: `nextbar_champion_hunter_wave_0016_seed_12516`
  - status at read time still showed prior completed best from wave 15 / wave 13.

## Data And Execution Model

- Signal source: normalized Binance copy-trading position history from `position_history_normalized.csv`.
- Market source: HYPE 1m OHLC universe NPZ, `binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz`.
- Initial equity: `500`.
- Position sizing: compound, target notional capped by current equity gate.
- Target notional: `500`.
- Min order gate: `2 USD`.
- Slippage: `4.25 bp`.
- Fill mode: `close_beyond_skip_boundary`.
- Entry-source checks run here:
  - `next_bar_open`
  - `first_bar_close`
- Binance `avgCost` / `avgClosePrice` are not treated as executable fills; they remain metadata only.

## Current Best Candidate

Candidate:

`rnd5337_t500_b12_s0p953-1p3-1p442-1p767_w0p597-0p82-1p151-1p868`

Exact params:

- `target_notional`: `500`
- `base_frac`: `0.12`
- `steps_pct`: `[0.953, 1.3, 1.442, 1.767]`
- `add_weights`: `[0.597, 0.82, 1.151, 1.868]`

Primary `next_bar_open` result:

| Metric | Value |
|---|---:|
| Trades | 122 |
| Equity start | 500.000000 |
| Equity end | 760.630529 |
| Net pct | 52.126106 |
| Profit factor | 3.715923 |
| Max realized DD pct | -5.637815 |
| Max MTM DD pct | -12.265614 |
| Min trade MTM pct equity | -13.037554 |
| Win rate pct | 90.983607 |
| Avg DCA fills | 1.549180 |
| Avg notional | 244.137089 |
| Max notional | 748.649957 |
| Min order USD | 58.944185 |

Gates:

- Margin calls: `0`
- Notional greater than equity before trade: `0`
- Max MTM DD gate: pass
- Min trade MTM gate: pass
- Min order gate: pass

Leg sizes for best `next_bar_open` replay:

| Leg metric | Value |
|---|---:|
| Trades | 122 |
| Total fills including base | 311 |
| DCA add fills | 189 |
| Min leg USD | 58.944185 |
| Median leg USD | 83.040877 |
| Min base leg USD | 59.725081 |
| Median base leg USD | 74.909146 |
| Min add leg USD | 58.944185 |
| Median add leg USD | 91.184306 |
| Sub-5 USD legs | 0 |
| Sub-2 USD legs | 0 |

Note: max notional exceeds initial equity because sizing compounds after equity growth. The active gate is current-equity bounded, and no trade violated `notional_gt_equity_before`.

## Entry-Source Crosscheck

Same candidate under `first_bar_close`:

| Metric | Value |
|---|---:|
| Trades | 122 |
| Equity start | 500.000000 |
| Equity end | 758.870428 |
| Net pct | 51.774086 |
| Profit factor | 3.699657 |
| Max MTM DD pct | -12.268260 |
| Min trade MTM pct equity | -13.014769 |
| Avg DCA fills | 1.540984 |
| Avg notional | 243.160329 |
| Max notional | 746.917579 |
| Min order USD | 58.821577 |

This is a stable result across the two allowed offline executable entry models: `52.13%` on `next_bar_open` vs `51.77%` on `first_bar_close`.

Live BingX mark/bid/ask telemetry parity is not proven by this replay. It still needs a paper/live context replay using collected `entry_market_context` and `exit_market_context` snapshots before any promotion.

## Baseline Comparison

All rows use initial equity `500`, target notional `500`, min order `2`, slippage `4.25 bp`, and strict `close_beyond_skip_boundary`.

| Candidate | Entry source | Equity end | Net pct | PF | Max MTM DD pct | Min order USD |
|---|---|---:|---:|---:|---:|---:|
| `plain_no_dca_t500` | `next_bar_open` | 537.979889 | 7.595978 | 1.084161 | -19.375196 | n/a |
| `current_like_dca3_t500` | `next_bar_open` | 580.025754 | 16.005151 | 1.260102 | -14.435340 | 102.261268 |
| `t500_b16_s0p25-0p35-0p55_w0p8-1p2-2p2` | `next_bar_open` | 578.286572 | 15.657314 | 1.246106 | -14.552652 | 75.467364 |
| `rnd3674_t500_b12_s1p046-1p22-1p534-1p668_w1p445-2p308-3p074-3p451` | `next_bar_open` | 757.507152 | 51.501430 | 3.510972 | -12.403547 | 59.240923 |
| `rnd5337_t500_b12_s0p953-1p3-1p442-1p767_w0p597-0p82-1p151-1p868` | `next_bar_open` | 760.630529 | 52.126106 | 3.715923 | -12.265614 | 58.944185 |
| `rnd5337_t500_b12_s0p953-1p3-1p442-1p767_w0p597-0p82-1p151-1p868` | `first_bar_close` | 758.870428 | 51.774086 | 3.699657 | -12.268260 | 58.821577 |

The +100% objective is not hit. The current local best is +52.13% on the available period.

## Approval Gates

Before any paper-live or live promotion:

- Explicit owner approval is required.
- Replay must not depend on Binance `avgCost` / `avgClosePrice` as fills.
- Candidate must pass no-margin-call and current-equity notional gates.
- Paper-live must confirm executable BingX entry and exit context behavior.
- Any live-order path remains out of scope for TelegramDcaOps1 in this task.

## Smallest Next Action

Let the currently running wave `0016` finish without interruption, then compare its top candidate against `rnd5337` on both `next_bar_open` and `first_bar_close`.

Next two loops:

1. Run a local neighborhood search around `rnd5337`: base fraction `0.10-0.16`, steps near `[0.953, 1.3, 1.442, 1.767]`, weights near `[0.597, 0.82, 1.151, 1.868]`, target `500`, min order `2`, strict fill mode, and both allowed entry sources.
2. Run BingX telemetry calibration: replay the same candidates against collected paper/live `entry_market_context` and `exit_market_context` mark/bid/ask snapshots. If those snapshots are insufficient, extend the shadow collector before considering promotion.
