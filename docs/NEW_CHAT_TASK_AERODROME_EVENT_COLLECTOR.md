# Task: Aerodrome Slipstream event collection and CHECK 2% real-fee backtest

## Context

We have OHLCV for:
- CHECK/USDC Aerodrome 2%
- CHECK/USDC Aerodrome 0.25%
- MEZO/MUSD
- MUSD/USDC benchmarks

OHLCV proxy selected CHECK/USDC Aerodrome 2% as the first serious L2 active-strategy candidate.

Pool:
```text
network: Base
dex: Aerodrome Slipstream
pair: CHECK/USDC
fee tier: 2%
address: 0x5a7b4970b2610aee4776a6944d9f2171ee6060b0
```

## New files

```text
dex_platform/data_collectors/fetch_aerodrome_slipstream_events_v1.py
dex_platform/data_collectors/inspect_aerodrome_events.py
dex_platform/data_collectors/requirements_aerodrome.txt
dex_platform/scripts/fetch_check_2pct_aerodrome_recent_events.sh
dex_platform/scripts/fetch_check_2pct_aerodrome_quarter_events.sh
dex_platform/configs/pools/base_check_usdc_aerodrome_2pct.yaml
```

## Commands

Install:
```bash
python3 -m pip install -r dex_platform/data_collectors/requirements_aerodrome.txt
```

Set RPC:
```bash
export BASE_RPC_URL="https://mainnet.base.org"
```

Smoke-test one day:
```bash
bash dex_platform/scripts/fetch_check_2pct_aerodrome_recent_events.sh
```

Full quarter:
```bash
bash dex_platform/scripts/fetch_check_2pct_aerodrome_quarter_events.sh
```

## Next backtester task

After events are collected:
1. Parse Swap events with tick, sqrtPriceX96, liquidity.
2. Parse Mint/Burn/Collect events by tickLower/tickUpper.
3. Reconstruct active liquidity enough to estimate fee share.
4. Run wide benchmark and adaptive grid on CHECK 2%.
5. Compare against OHLCV proxy and HODL 50/50.

## Warning

This collector gets raw events. It is not yet a complete fee-growth replay.
