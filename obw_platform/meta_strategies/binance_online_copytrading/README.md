# Binance Online Copytrading

Research-only paper-live meta-strategy for public Binance copy-trading lead pages.

It never places orders. It polls public Binance copy-trading endpoints and uses
those public lead events to enable one real V21 directional leg in paper.

The active leg is loaded from
`obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml`:

- `strategies.cryptomine_pack_dual_full.CryptomineLongPackAdaptiveEven`
- `strategies.cryptomine_pack_dual_full.CryptomineShortPackAdaptiveEven`

The simplified DCA overlay is not used as the execution model. A public
`follow_open` signal enables the lead side. A public `contrarian_on_close`
signal enables the opposite side. The runner does not enable the opposite V21
leg for the same source/symbol while one is already active.

`paper_notional_usdt` / `--notional-usdt` is the default delegated capital for
the active V21 leg. Per-lead `delegated_capital_usdt` can override it. The
runner sets the active side's `equityForSizingUSDT` to delegated capital and
`baseOrderPctEq` to `5.0`; V21 then computes actual order quantity and notional
through its trend/regime/vol adaptive sizing path. Slippage is still applied to
paper/shadow accounting.

Default strategies:

- `lead_472867_follow_open`: follow public open positions from Binance lead
  `昕儒之水` (`4728671486012660992`).
- `lead_475183_contrarian_close`: after a lead closed position from
  `Btc星辰` (`4751838302089254401`), open the opposite V21 side. Legacy
  TTL/reversal exits remain only for non-V21 paper rows; V21 rows exit through
  the V21 sub-strategy.
- `lead_490601_contrarian_close`: same rule for `勇行致远观势`
  (`4906010685108267264`).

Run once without writing:

```powershell
python obw_platform\meta_strategies\binance_online_copytrading\binance_online_copytrading.py --dry-run
```

Run once and update paper state:

```powershell
python obw_platform\meta_strategies\binance_online_copytrading\binance_online_copytrading.py --once
```

On the first run, existing closed-position history is seeded as already seen,
so contrarian strategies start from new closes observed after the runner starts.
Use `--trade-existing-history` only for manual experiments.

Run polling loop:

```powershell
python obw_platform\meta_strategies\binance_online_copytrading\binance_online_copytrading.py --loop --interval-sec 60 --paper-exchange bingx
```

Override the V21 config explicitly:

```powershell
python obw_platform\meta_strategies\binance_online_copytrading\binance_online_copytrading.py --once --v21-config obw_platform\configs\V21_strict_trend_stable_live_static9p38.yaml
```

State is written under `reports/state.json`; reports are gitignored.

Production-grade paper artifacts are written next to the state file by default:

- `reports/session.sqlite` records run metadata, signal polls, paper events,
  paper positions, shadow order rows, paper-compatible order rows, and equity
  snapshots.
- `reports/shadow_orders.jsonl` records the current desired paper/live-parity
  target state for open public copy-trading signals.

These artifacts are paper/shadow only. The runner has no exchange order
submission path.
