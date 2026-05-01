# DEX data collectors

Collect event-level Uniswap V3 data for concentrated-liquidity LP backtesting.

OHLCV is not enough for this strategy. We need swaps, mints, burns, collects, tick/sqrtPriceX96, gas metadata and pool metadata.

## Install

From `obw_platform` root:

```bash
python3 -m pip install -r dex_data_collectors/requirements.txt
```

## API key

Do not put your key into Git.

```bash
export THEGRAPH_API_KEY='YOUR_KEY'
```

## Smoke test

```bash
bash scripts/fetch_uniswap_v3_weth_usdc_005_smoke.sh
```

## Month collection

```bash
bash scripts/fetch_uniswap_v3_weth_usdc_005_month.sh 2026-03-01T00:00:00Z 2026-04-01T00:00:00Z
```

Default pool: WETH/USDC 0.05% Uniswap V3 mainnet, address `0x88e6A0c2dDD26FEEb64F039a2c41296FcB3f5640`.
