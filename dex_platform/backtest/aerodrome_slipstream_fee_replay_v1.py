#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/backtest/aerodrome_slipstream_fee_replay_v1.py

Event-based fee replay MVP for Aerodrome Slipstream / Velodrome CL-style pools.

This is a real-event backtester step beyond OHLCV proxy:
  - reads Swap events from Aerodrome Slipstream collector v2
  - uses Swap.sqrtPriceX96 / tick / active liquidity
  - computes LP raw liquidity for our synthetic range
  - estimates fee share:
        our_fee = swap_input_value_usd * fee_rate * our_liquidity / active_liquidity
  - values concentrated LP inventory with raw Uniswap-style liquidity math
  - exports equity curves, summary JSON/CSV, and plots

Known limitations:
  - Does not yet reconstruct feeGrowthInside.
  - Does not yet model exact Aerodrome dynamic fee changes per swap.
  - Uses fixed fee-rate scenarios supplied by CLI.
  - Does not yet replay Mint/Burn liquidity deltas to verify active liquidity history.
    It uses Swap.liquidity emitted by pool, which is the practical MVP for active L.
"""

import argparse
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd


DEC0_DEFAULT = 6    # USDC
DEC1_DEFAULT = 18   # CHECK


@dataclass
class StrategyConfig:
    name: str
    lower_pct: float
    upper_pct: float
    rebalance_hours: float = 0.0
    gas_per_rebalance_usd: float = 0.0
    swap_cost_bps: float = 0.0


@dataclass
class Position:
    lower: float
    upper: float
    open_price: float
    liquidity_raw: float
    capital_usd: float


def read_events(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"input not found: {p}")
    if p.suffix.lower() == ".parquet":
        df = pd.read_parquet(p)
    else:
        df = pd.read_csv(p)

    if "event_type" not in df.columns:
        raise SystemExit("input misses event_type column")

    sw = df[df["event_type"].astype(str).str.lower() == "swap"].copy()
    if sw.empty:
        raise SystemExit("no Swap events in input")

    for col in ["timestamp", "blockNumber", "transactionIndex", "logIndex", "tick"]:
        if col in sw.columns:
            sw[col] = pd.to_numeric(sw[col], errors="coerce")

    # Big integers may be stored as string to make parquet safe.
    for col in ["amount0", "amount1", "sqrtPriceX96", "liquidity"]:
        if col not in sw.columns:
            raise SystemExit(f"input misses required Swap column: {col}")
        sw[col] = sw[col].map(parse_int_safe)

    sw = sw.dropna(subset=["timestamp", "amount0", "amount1", "sqrtPriceX96", "liquidity"])
    sw = sw.sort_values(["timestamp", "blockNumber", "transactionIndex", "logIndex"], kind="stable").reset_index(drop=True)
    return sw


def parse_int_safe(x: Any) -> Optional[int]:
    if x is None:
        return None
    try:
        if pd.isna(x):
            return None
    except Exception:
        pass
    if isinstance(x, (int, np.integer)):
        return int(x)
    if isinstance(x, float):
        if not math.isfinite(x):
            return None
        return int(x)
    s = str(x).strip()
    if not s or s.lower() == "nan":
        return None
    # Sometimes pandas writes scientific notation for large ints in CSV.
    # Prefer int(s), fallback to Decimal-like float. For serious work, use parquet from collector v2.
    try:
        return int(s)
    except Exception:
        return int(float(s))


def price_token0_per_token1_from_sqrt(sqrt_price_x96: int, dec0: int, dec1: int) -> float:
    """
    Pool price convention:
      sqrtPriceX96 = sqrt(token1_raw / token0_raw) * 2^96

    Return human token0/token1 price, e.g. USDC per CHECK.
    """
    q_raw_token1_per_token0 = (int(sqrt_price_x96) / (2 ** 96)) ** 2
    return (10 ** (dec1 - dec0)) / q_raw_token1_per_token0


def sqrt_raw_token1_per_token0_from_price(price_token0_per_token1: float, dec0: int, dec1: int) -> float:
    q_raw = (10 ** (dec1 - dec0)) / float(price_token0_per_token1)
    return math.sqrt(q_raw)


def amounts_raw_for_liquidity(
    liquidity_raw: float,
    price: float,
    lower_price: float,
    upper_price: float,
    dec0: int,
    dec1: int,
) -> tuple[float, float]:
    """
    Return raw token0/token1 amounts for a CL position.

    token0 = quote, e.g. USDC
    token1 = base, e.g. CHECK
    price = token0/token1 human, e.g. USDC per CHECK

    Uniswap formulas use raw price token1/token0.
    Because human token0/token1 increases when raw token1/token0 decreases:
      sqrtA = sqrt(raw price at upper human price)
      sqrtB = sqrt(raw price at lower human price)
    """
    p = max(float(price), 1e-30)
    lo = max(float(lower_price), 1e-30)
    up = max(float(upper_price), lo * 1.000001)

    sqrt_p = sqrt_raw_token1_per_token0_from_price(p, dec0, dec1)
    sqrt_a = sqrt_raw_token1_per_token0_from_price(up, dec0, dec1)
    sqrt_b = sqrt_raw_token1_per_token0_from_price(lo, dec0, dec1)

    L = float(liquidity_raw)

    if p <= lo:
        # all token1
        amount0 = 0.0
        amount1 = L * (sqrt_b - sqrt_a)
    elif p >= up:
        # all token0
        amount0 = L * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
        amount1 = 0.0
    else:
        amount0 = L * (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b)
        amount1 = L * (sqrt_p - sqrt_a)

    return float(amount0), float(amount1)


def value_usd_from_raw_amounts(amount0_raw: float, amount1_raw: float, price: float, dec0: int, dec1: int) -> float:
    token0_h = float(amount0_raw) / (10 ** dec0)
    token1_h = float(amount1_raw) / (10 ** dec1)
    return token0_h + token1_h * float(price)


def liquidity_for_capital(capital_usd: float, open_price: float, lower_price: float, upper_price: float, dec0: int, dec1: int) -> float:
    a0, a1 = amounts_raw_for_liquidity(1.0, open_price, lower_price, upper_price, dec0, dec1)
    unit_value = value_usd_from_raw_amounts(a0, a1, open_price, dec0, dec1)
    if unit_value <= 1e-30:
        return 0.0
    return float(capital_usd) / unit_value


def position_value_usd(pos: Position, price: float, dec0: int, dec1: int) -> float:
    a0, a1 = amounts_raw_for_liquidity(pos.liquidity_raw, price, pos.lower, pos.upper, dec0, dec1)
    return value_usd_from_raw_amounts(a0, a1, price, dec0, dec1)


def open_position(capital_usd: float, price: float, lower_pct: float, upper_pct: float, dec0: int, dec1: int) -> Position:
    lower = price * (1.0 - lower_pct / 100.0)
    upper = price * (1.0 + upper_pct / 100.0)
    L = liquidity_for_capital(capital_usd, price, lower, upper, dec0, dec1)
    return Position(lower=lower, upper=upper, open_price=price, liquidity_raw=L, capital_usd=capital_usd)


def swap_input_value_usd(row: pd.Series, price: float, dec0: int, dec1: int) -> float:
    amount0 = int(row["amount0"])
    amount1 = int(row["amount1"])

    # In CL swap event, the positive amount is usually amount paid into the pool.
    if amount0 > 0:
        return amount0 / (10 ** dec0)
    if amount1 > 0:
        return (amount1 / (10 ** dec1)) * float(price)

    # Fallback if signs are unexpected.
    return max(abs(amount0) / (10 ** dec0), abs(amount1) / (10 ** dec1) * float(price))


def max_drawdown_pct(values: List[float]) -> float:
    arr = np.asarray(values, dtype=float)
    if len(arr) == 0:
        return 0.0
    peak = np.maximum.accumulate(arr)
    dd = arr / np.where(peak == 0, np.nan, peak) - 1.0
    return float(np.nanmin(dd) * 100.0)


def simulate_strategy(
    swaps: pd.DataFrame,
    strategy: StrategyConfig,
    *,
    fee_rate: float,
    initial_capital_usd: float,
    dec0: int,
    dec1: int,
) -> pd.DataFrame:
    prices = swaps["sqrtPriceX96"].map(lambda x: price_token0_per_token1_from_sqrt(int(x), dec0, dec1)).astype(float)
    swaps = swaps.copy()
    swaps["price"] = prices

    p0 = float(swaps["price"].iloc[0])
    t0 = int(swaps["timestamp"].iloc[0])

    position = open_position(initial_capital_usd, p0, strategy.lower_pct, strategy.upper_pct, dec0, dec1)

    fees_cum = 0.0
    costs_cum = 0.0
    rebalances = 0
    last_rebalance_ts = t0

    hodl_token0 = initial_capital_usd / 2.0
    hodl_token1 = (initial_capital_usd / 2.0) / p0

    rows: List[Dict[str, Any]] = []

    for _, row in swaps.iterrows():
        ts = int(row["timestamp"])
        price = float(row["price"])

        # Optional periodic rebalance. Reinvest current position value + earned fees, less costs.
        if strategy.rebalance_hours and strategy.rebalance_hours > 0:
            if ts - last_rebalance_ts >= strategy.rebalance_hours * 3600:
                current_value = position_value_usd(position, price, dec0, dec1)
                redeploy = current_value + fees_cum
                cost = strategy.gas_per_rebalance_usd + redeploy * (strategy.swap_cost_bps / 10000.0)
                redeploy = max(0.0, redeploy - cost)
                costs_cum += cost
                fees_cum = 0.0
                position = open_position(redeploy, price, strategy.lower_pct, strategy.upper_pct, dec0, dec1)
                last_rebalance_ts = ts
                rebalances += 1

        active_liquidity = max(float(row["liquidity"]), 1.0)
        in_range = position.lower <= price <= position.upper

        event_fee = 0.0
        fee_total_pool_usd = 0.0
        our_share = 0.0

        if in_range:
            input_usd = swap_input_value_usd(row, price, dec0, dec1)
            fee_total_pool_usd = input_usd * fee_rate
            our_share = min(max(position.liquidity_raw / active_liquidity, 0.0), 1.0)
            event_fee = fee_total_pool_usd * our_share

        fees_cum += event_fee
        position_value = position_value_usd(position, price, dec0, dec1)
        equity = position_value + fees_cum
        hodl50 = hodl_token0 + hodl_token1 * price

        rows.append({
            "timestamp": ts,
            "datetime_utc": pd.to_datetime(ts, unit="s", utc=True),
            "price": price,
            "strategy": strategy.name,
            "equity": equity,
            "position_value": position_value,
            "fees_cum": fees_cum,
            "costs_cum": costs_cum,
            "event_fee": event_fee,
            "pool_fee_total_event_usd": fee_total_pool_usd,
            "our_liquidity": position.liquidity_raw,
            "active_liquidity": active_liquidity,
            "our_liquidity_share": our_share,
            "in_range": int(in_range),
            "lower_price": position.lower,
            "upper_price": position.upper,
            "rebalances": rebalances,
            "hodl50": hodl50,
        })

    return pd.DataFrame(rows)


def summarize_curve(curve: pd.DataFrame, initial_capital_usd: float, fee_rate: float, strategy: str) -> Dict[str, Any]:
    start_ts = int(curve["timestamp"].iloc[0])
    end_ts = int(curve["timestamp"].iloc[-1])
    days = (end_ts - start_ts) / 86400.0

    end_equity = float(curve["equity"].iloc[-1])
    ret = (end_equity / initial_capital_usd - 1.0) * 100.0

    hodl_end = float(curve["hodl50"].iloc[-1])

    return {
        "strategy": strategy,
        "fee_rate": fee_rate,
        "fee_rate_pct": fee_rate * 100.0,
        "initial_capital_usd": initial_capital_usd,
        "equity_end_usd": end_equity,
        "return_pct": ret,
        "mdd_pct": max_drawdown_pct(curve["equity"].tolist()),
        "fees_usd": float(curve["fees_cum"].iloc[-1]),
        "costs_usd": float(curve["costs_cum"].iloc[-1]),
        "position_value_end_usd": float(curve["position_value"].iloc[-1]),
        "hodl50_end_usd": hodl_end,
        "hodl50_return_pct": (hodl_end / initial_capital_usd - 1.0) * 100.0,
        "vs_hodl50_usd": end_equity - hodl_end,
        "time_in_range_pct": float(curve["in_range"].mean() * 100.0),
        "avg_liquidity_share_pct_when_in_range": float(curve.loc[curve["in_range"] == 1, "our_liquidity_share"].mean() * 100.0) if (curve["in_range"] == 1).any() else 0.0,
        "rebalances": int(curve["rebalances"].iloc[-1]),
        "days": days,
        "price_first": float(curve["price"].iloc[0]),
        "price_last": float(curve["price"].iloc[-1]),
        "price_return_pct": (float(curve["price"].iloc[-1]) / float(curve["price"].iloc[0]) - 1.0) * 100.0,
    }


def parse_strategy_spec(spec: str) -> StrategyConfig:
    """
    Format:
      name:lower_pct:upper_pct[:rebalance_hours[:gas_usd[:swap_cost_bps]]]

    Examples:
      wide_80_5:80:5
      adaptive_60_5_6h:60:5:6:0.05:5
    """
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(f"bad strategy spec: {spec}")
    name = parts[0]
    lower = float(parts[1])
    upper = float(parts[2])
    reb = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
    gas = float(parts[4]) if len(parts) > 4 and parts[4] else 0.0
    swap_bps = float(parts[5]) if len(parts) > 5 and parts[5] else 0.0
    return StrategyConfig(name=name, lower_pct=lower, upper_pct=upper, rebalance_hours=reb, gas_per_rebalance_usd=gas, swap_cost_bps=swap_bps)


def plot_results(curves: pd.DataFrame, summary: pd.DataFrame, out_dir: Path) -> List[str]:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plots: List[str] = []
    plot_dir = out_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    # Equity
    fig, ax = plt.subplots(figsize=(12, 5))
    for name, g in curves.groupby("run_name"):
        ax.plot(g["datetime_utc"], g["equity"], label=name)
    # HODL once
    first = curves[curves["run_name"] == curves["run_name"].iloc[0]]
    ax.plot(first["datetime_utc"], first["hodl50"], label="hodl50", linestyle="--")
    ax.set_title("Aerodrome fee replay: equity")
    ax.set_xlabel("Time UTC")
    ax.set_ylabel("USD")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = plot_dir / "equity.png"
    fig.savefig(p, dpi=160, bbox_inches="tight")
    plt.close(fig)
    plots.append(str(p))

    # Fees
    fig, ax = plt.subplots(figsize=(12, 5))
    for name, g in curves.groupby("run_name"):
        ax.plot(g["datetime_utc"], g["fees_cum"], label=name)
    ax.set_title("Aerodrome fee replay: earned fees")
    ax.set_xlabel("Time UTC")
    ax.set_ylabel("USD")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    p = plot_dir / "fees.png"
    fig.savefig(p, dpi=160, bbox_inches="tight")
    plt.close(fig)
    plots.append(str(p))

    # Price
    fig, ax = plt.subplots(figsize=(12, 5))
    first = curves[curves["run_name"] == curves["run_name"].iloc[0]]
    ax.plot(first["datetime_utc"], first["price"])
    ax.set_title("Aerodrome swap price: token0/token1")
    ax.set_xlabel("Time UTC")
    ax.set_ylabel("USDC per CHECK")
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    p = plot_dir / "price.png"
    fig.savefig(p, dpi=160, bbox_inches="tight")
    plt.close(fig)
    plots.append(str(p))

    # Summary return bars
    fig, ax = plt.subplots(figsize=(12, 5))
    s = summary.sort_values("return_pct", ascending=False)
    ax.bar(s["run_name"], s["return_pct"])
    ax.set_title("Return % by run")
    ax.set_ylabel("Return %")
    ax.tick_params(axis="x", rotation=45)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    p = plot_dir / "return_by_run.png"
    fig.savefig(p, dpi=160, bbox_inches="tight")
    plt.close(fig)
    plots.append(str(p))

    return plots


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True, help="events_all.csv/parquet from Aerodrome collector v2")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--initial-capital-usd", type=float, default=1000.0)
    ap.add_argument("--dec0", type=int, default=DEC0_DEFAULT)
    ap.add_argument("--dec1", type=int, default=DEC1_DEFAULT)
    ap.add_argument(
        "--fee-rates",
        default="metadata_0_2685:0.002685,label_2pct:0.02",
        help="Comma-separated name:rate pairs. Example: metadata_0_2685:0.002685,label_2pct:0.02",
    )
    ap.add_argument(
        "--strategies",
        default="wide_80_5:80:5,wide_60_5:60:5,wide_50_10:50:10,oleg_25_18:25:18,adaptive_60_5_6h:60:5:6:0.05:5,adaptive_80_5_12h:80:5:12:0.05:5",
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    swaps = read_events(args.events)

    # Precompute and save normalized swap table for QA.
    swaps_norm = swaps.copy()
    swaps_norm["price"] = swaps_norm["sqrtPriceX96"].map(lambda x: price_token0_per_token1_from_sqrt(int(x), args.dec0, args.dec1)).astype(float)
    swaps_norm["amount0_human"] = swaps_norm["amount0"].map(lambda x: int(x) / (10 ** args.dec0))
    swaps_norm["amount1_human"] = swaps_norm["amount1"].map(lambda x: int(x) / (10 ** args.dec1))
    swaps_norm.to_csv(out_dir / "swaps_normalized.csv", index=False)

    fee_specs = []
    for item in args.fee_rates.split(","):
        if not item.strip():
            continue
        name, rate = item.split(":", 1)
        fee_specs.append((name, float(rate)))

    strategies = [parse_strategy_spec(x) for x in args.strategies.split(",") if x.strip()]

    all_curves: List[pd.DataFrame] = []
    summary_rows: List[Dict[str, Any]] = []

    for fee_name, fee_rate in fee_specs:
        for strat in strategies:
            curve = simulate_strategy(
                swaps,
                strat,
                fee_rate=fee_rate,
                initial_capital_usd=args.initial_capital_usd,
                dec0=args.dec0,
                dec1=args.dec1,
            )
            run_name = f"{fee_name}__{strat.name}"
            curve["fee_scenario"] = fee_name
            curve["fee_rate"] = fee_rate
            curve["run_name"] = run_name
            all_curves.append(curve)

            s = summarize_curve(curve, args.initial_capital_usd, fee_rate, strat.name)
            s["fee_scenario"] = fee_name
            s["run_name"] = run_name
            s["lower_pct"] = strat.lower_pct
            s["upper_pct"] = strat.upper_pct
            s["rebalance_hours"] = strat.rebalance_hours
            summary_rows.append(s)

    curves = pd.concat(all_curves, ignore_index=True)
    summary = pd.DataFrame(summary_rows).sort_values(["fee_scenario", "return_pct"], ascending=[True, False]).reset_index(drop=True)

    curves.to_csv(out_dir / "curves.csv", index=False)
    summary.to_csv(out_dir / "summary.csv", index=False)

    plots = plot_results(curves, summary, out_dir)

    result = {
        "input_events": str(args.events),
        "out_dir": str(out_dir),
        "rows_swaps": int(len(swaps)),
        "initial_capital_usd": float(args.initial_capital_usd),
        "fee_scenarios": [{"name": n, "rate": r, "pct": r * 100.0} for n, r in fee_specs],
        "strategies": [s.__dict__ for s in strategies],
        "plots": plots,
        "best_by_return": summary.sort_values("return_pct", ascending=False).head(10).to_dict(orient="records"),
        "warning": "MVP fee replay. Uses Swap.liquidity for active liquidity and fixed fee-rate scenarios. Does not yet reconstruct feeGrowthInside.",
    }
    (out_dir / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")

    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
