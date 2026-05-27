# Binance Online Copytrading

Research-only paper-live meta-strategy for public Binance copy-trading lead pages.

It never places orders. It polls public Binance copy-trading endpoints, converts
public lead activity into paper signals, and marks paper positions with a
configured slippage.

Default strategies:

- `lead_472867_follow_open`: follow public open positions from lead
  `4728671486012660992`.
- `lead_475183_contrarian_close`: after a lead closed position from
  `4751838302089254401`, open the opposite side for a 72h TTL, with optional
  exit on same-symbol reversal.
- `lead_490601_contrarian_close`: same rule for `4906010685108267264`.

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

State is written under `reports/state.json`; reports are gitignored.
