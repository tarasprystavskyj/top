#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/backtest/lp_event_proxy_backtester_v1.py

MVP proxy backtester for Uniswap V3 LP strategies.

This is NOT a final precise Uniswap fee-growth replay.
It is the next practical step after raw event collection.

What it does:
  - Reads swaps.csv/parquet or events_all.csv/parquet.
  - Reconstructs price from tick.
  - Simulates concentrated LP ranges with Uniswap-style inventory value.
  - Estimates fees using a configurable active-liquidity proxy.
  - Compares:
      1) wide_range benchmark
      2) adaptive grid / volatility harvester
      3) 50/50 HODL benchmark
  - Exports curves, JSON summary, and plots.

Known limitations:
  - Does not reconstruct exact feeGrowthInside.
  - Does not reconstruct historical active tick liquidity.
  - Uses --active-liquidity-usd as calibration knob.
  - gas_used from The Graph transaction entity may be 0, so gas is manual.
"""

import argparse
import json
import math
from pathlib import Path
from typing import Dict, List, Any

import numpy as np
import pandas as pd


def read_table(path: str | Path) -> pd.DataFrame:
    p = Path(path)
    if p.suffix.lower() == ".parquet":
        try:
            return pd.read_parquet(p)
        except Exception as e:
            raise SystemExit(f"Failed to read parquet {p}: {e}. Use CSV or install pyarrow.")
    return pd.read_csv(p)


def price_usdc_per_weth_from_tick(tick, token0_decimals: int = 6, token1_decimals: int = 18):
    """
    For USDC/WETH-like pools:
      token0 = USDC
      token1 = WETH

    Uniswap tick gives raw token1/token0.
    Human token1 per token0 = 1.0001^tick * 10^(dec0-dec1)
    We return token0 per token1 = USDC/WETH.
    """
    raw = np.power(1.0001, tick)
    token1_per_token0 = raw * (10 ** (token0_decimals - token1_decimals))
    return 1.0 / token1_per_token0


def amounts_per_liquidity_unit(price: float, lower: float, upper: float):
    """
    Concentrated LP inventory approximation for a pair:
      base = WETH
      quote = USDC
      price = quote/base

    Returns base_per_L, quote_per_L.
    """
    p = max(float(price), 1e-12)
    a = max(float(lower), 1e-12)
    b = max(float(upper), a * 1.000001)

    sqrt_p = math.sqrt(p)
    sqrt_a = math.sqrt(a)
    sqrt_b = math.sqrt(b)

    if p <= a:
        base = (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
        quote = 0.0
    elif p >= b:
        base = 0.0
        quote = sqrt_b - sqrt_a
    else:
        base = (sqrt_b - sqrt_p) / (sqrt_p * sqrt_b)
        quote = sqrt_p - sqrt_a

    return base, quote


def lp_value_for_capital(capital_usd: float, open_price: float, lower: float, upper: float, price_now: float):
    base_per_l, quote_per_l = amounts_per_liquidity_unit(open_price, lower, upper)
    denom = base_per_l * open_price + quote_per_l
    if denom <= 1e-18:
        return 0.0, 0.0, 0.0, 0.0

    liquidity_units = capital_usd / denom
    base_now, quote_now = amounts_per_liquidity_unit(price_now, lower, upper)
    base_amt = liquidity_units * base_now
    quote_amt = liquidity_units * quote_now
    value = base_amt * price_now + quote_amt
    return float(value), float(base_amt), float(quote_amt), float(liquidity_units)


def pct_width(lower: float, upper: float, center: float) -> float:
    return max(1e-9, (float(upper) - float(lower)) / max(float(center), 1e-12) * 100.0)


def load_swaps(path: str | Path) -> pd.DataFrame:
    df = read_table(path)
    if "event_type" in df.columns:
        df = df[df["event_type"].astype(str).str.lower() == "swap"].copy()

    required = ["timestamp", "tick", "amount_usd"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise SystemExit(f"Input misses required columns: {missing}")

    for c in ["timestamp", "block_number", "log_index", "tick", "amount_usd"]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    df = df.dropna(subset=["timestamp", "tick"])
    df["amount_usd"] = pd.to_numeric(df["amount_usd"], errors="coerce").fillna(0.0).abs()
    sort_cols = [c for c in ["timestamp", "block_number", "log_index"] if c in df.columns]
    df = df.sort_values(sort_cols, kind="stable").reset_index(drop=True)
    df["price"] = price_usdc_per_weth_from_tick(df["tick"].astype(float).to_numpy())
    df["datetime_utc"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
    return df


def open_positions(strategy: str, price: float, capital: float, args) -> List[Dict[str, Any]]:
    p = float(price)

    if strategy == "wide":
        return [{
            "name": "wide",
            "lower": p * (1.0 - args.wide_lower_pct / 100.0),
            "upper": p * (1.0 + args.wide_upper_pct / 100.0),
            "capital": float(capital),
            "open_price": p,
        }]

    if strategy == "adaptive":
        positions: List[Dict[str, Any]] = []

        inner_w = args.inner_width_pct / 100.0
        positions.append({
            "name": "inner",
            "lower": p * (1.0 - inner_w / 2.0),
            "upper": p * (1.0 + inner_w / 2.0),
            "capital": float(capital) * args.inner_capital_pct / 100.0,
            "open_price": p,
        })

        off = args.outer_offset_pct / 100.0
        ow = args.outer_width_pct / 100.0

        positions.append({
            "name": "lower",
            "lower": p * (1.0 - off - ow),
            "upper": p * (1.0 - off),
            "capital": float(capital) * args.lower_capital_pct / 100.0,
            "open_price": p,
        })

        positions.append({
            "name": "upper",
            "lower": p * (1.0 + off),
            "upper": p * (1.0 + off + ow),
            "capital": float(capital) * args.upper_capital_pct / 100.0,
            "open_price": p,
        })

        reserve_pct = max(0.0, 100.0 - args.inner_capital_pct - args.lower_capital_pct - args.upper_capital_pct)
        if reserve_pct > 0:
            # Keep reserve as stable cash. It earns no LP fees.
            positions.append({
                "name": "cash_reserve",
                "lower": 0.0,
                "upper": float("inf"),
                "capital": float(capital) * reserve_pct / 100.0,
                "open_price": p,
                "cash": True,
            })

        return positions

    raise ValueError(f"unknown strategy={strategy}")


def position_value(pos: Dict[str, Any], price: float):
    if pos.get("cash"):
        return float(pos["capital"]), 0.0, float(pos["capital"])
    return lp_value_for_capital(
        float(pos["capital"]),
        float(pos["open_price"]),
        float(pos["lower"]),
        float(pos["upper"]),
        float(price),
    )[:3]


def estimate_fee_for_position(pos: Dict[str, Any], price: float, volume_usd: float, fee_rate: float, args) -> float:
    if pos.get("cash"):
        return 0.0

    lower = float(pos["lower"])
    upper = float(pos["upper"])
    if not (lower <= price <= upper):
        return 0.0

    width = pct_width(lower, upper, float(pos["open_price"]))
    concentration_boost = args.reference_width_pct / max(width, 0.01)
    concentration_boost = max(args.min_concentration_boost, min(args.max_concentration_boost, concentration_boost))

    # Proxy:
    # active liquidity is not reconstructed yet.
    # active_liquidity_usd is the calibration knob.
    share = float(pos["capital"]) / max(float(args.active_liquidity_usd), 1e-9)
    return float(volume_usd) * float(fee_rate) * share * concentration_boost


def simulate(swaps: pd.DataFrame, strategy: str, args):
    if swaps.empty:
        raise SystemExit("No swap rows in input.")

    fee_rate = args.fee_tier_bps / 10000.0
    p0 = float(swaps["price"].iloc[0])
    ts0 = int(swaps["timestamp"].iloc[0])

    hodl_quote = args.initial_capital_usd / 2.0
    hodl_base = (args.initial_capital_usd / 2.0) / p0

    positions = open_positions(strategy, p0, args.initial_capital_usd, args)
    last_rebalance_ts = ts0

    cumulative_fees = 0.0
    cumulative_costs = 0.0
    rebalances = 0

    rows = []

    for _, row in swaps.iterrows():
        ts = int(row["timestamp"])
        price = float(row["price"])
        volume_usd = float(row["amount_usd"])

        if strategy == "adaptive":
            if ts - last_rebalance_ts >= args.rebalance_hours * 3600:
                current_position_value = sum(position_value(p, price)[0] for p in positions)
                capital_to_redeploy = current_position_value + cumulative_fees

                # Manual cost model. The Graph may return gas_used=0, so do not trust gas from raw dataset yet.
                swap_cost = capital_to_redeploy * (args.rebalance_swap_cost_bps / 10000.0)
                cost = args.gas_per_rebalance_usd + swap_cost
                cumulative_costs += cost
                capital_to_redeploy = max(0.0, capital_to_redeploy - cost)

                cumulative_fees = 0.0
                positions = open_positions(strategy, price, capital_to_redeploy, args)
                last_rebalance_ts = ts
                rebalances += 1

        event_fees = 0.0
        in_any_range = False
        active_capital = 0.0

        for pos in positions:
            if pos.get("cash"):
                continue
            if float(pos["lower"]) <= price <= float(pos["upper"]):
                in_any_range = True
                active_capital += float(pos["capital"])
                event_fees += estimate_fee_for_position(pos, price, volume_usd, fee_rate, args)

        cumulative_fees += event_fees

        pos_value = 0.0
        base_amt = 0.0
        quote_amt = 0.0
        for pos in positions:
            v, b, q = position_value(pos, price)
            pos_value += v
            base_amt += b
            quote_amt += q

        equity = pos_value + cumulative_fees
        hodl50 = hodl_quote + hodl_base * price

        rows.append({
            "timestamp": ts,
            "datetime_utc": pd.to_datetime(ts, unit="s", utc=True),
            "price": price,
            "volume_usd": volume_usd,
            "equity": equity,
            "position_value": pos_value,
            "fees_uncollected_or_compounded": cumulative_fees,
            "costs_cumulative": cumulative_costs,
            "hodl50": hodl50,
            "base_amt": base_amt,
            "quote_amt": quote_amt,
            "inventory_base_value_usd": base_amt * price,
            "inventory_quote_value_usd": quote_amt,
            "active_capital": active_capital,
            "in_any_range": int(in_any_range),
            "rebalances": rebalances,
        })

    curves = pd.DataFrame(rows)
    return curves


def max_drawdown_pct(equity: pd.Series) -> float:
    x = pd.to_numeric(equity, errors="coerce").fillna(method="ffill").fillna(0.0)
    peak = x.cummax()
    dd = x / peak.replace(0, np.nan) - 1.0
    return float(dd.min() * 100.0)


def summarize(curves: pd.DataFrame, args, strategy: str) -> dict:
    start = float(args.initial_capital_usd)
    end = float(curves["equity"].iloc[-1])
    days = (int(curves["timestamp"].iloc[-1]) - int(curves["timestamp"].iloc[0])) / 86400.0
    ret = end / start - 1.0 if start > 0 else 0.0
    ann = ((end / start) ** (365.0 / days) - 1.0) if days > 0 and start > 0 and end > 0 else 0.0
    hodl_end = float(curves["hodl50"].iloc[-1])

    return {
        "strategy": strategy,
        "initial_capital_usd": start,
        "equity_end_usd": end,
        "pnl_usd": end - start,
        "return_pct": ret * 100.0,
        "annualized_pct": ann * 100.0,
        "days": days,
        "fees_modelled_usd": float(curves["fees_uncollected_or_compounded"].iloc[-1]),
        "costs_modelled_usd": float(curves["costs_cumulative"].iloc[-1]),
        "mdd_pct": max_drawdown_pct(curves["equity"]),
        "hodl50_end_usd": hodl_end,
        "vs_hodl50_usd": end - hodl_end,
        "hodl50_return_pct": (hodl_end / start - 1.0) * 100.0,
        "time_in_any_range_pct": float(curves["in_any_range"].mean() * 100.0),
        "rebalances": int(curves["rebalances"].iloc[-1]),
        "price_first": float(curves["price"].iloc[0]),
        "price_last": float(curves["price"].iloc[-1]),
        "price_return_pct": (float(curves["price"].iloc[-1]) / float(curves["price"].iloc[0]) - 1.0) * 100.0,
        "swap_volume_usd": float(curves["volume_usd"].sum()),
        "active_liquidity_usd_proxy": float(args.active_liquidity_usd),
        "fee_tier_bps": float(args.fee_tier_bps),
        "warning": (
            "Proxy model. Not exact Uniswap v3 feeGrowthInside replay. "
            "Use for strategy shape comparison and calibration, not production capital decisions."
        ),
    }


def save_plot(curves_by_strategy: Dict[str, pd.DataFrame], out_dir: Path, prefix: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    out_dir.mkdir(parents=True, exist_ok=True)

    def save(fig, name):
        p = out_dir / name
        fig.tight_layout()
        fig.savefig(p, dpi=160, bbox_inches="tight")
        plt.close(fig)
        return str(p)

    plots = []

    fig, ax = plt.subplots(figsize=(12, 5))
    for name, df in curves_by_strategy.items():
        ax.plot(df["datetime_utc"], df["equity"], label=f"{name} equity")
    first = next(iter(curves_by_strategy.values()))
    ax.plot(first["datetime_utc"], first["hodl50"], label="hodl50", linestyle="--")
    ax.set_title("LP proxy equity vs HODL 50/50")
    ax.set_xlabel("Time UTC")
    ax.set_ylabel("USD")
    ax.grid(True, alpha=0.3)
    ax.legend()
    plots.append(save(fig, f"{prefix}_equity_vs_hodl.png"))

    fig, ax = plt.subplots(figsize=(12, 5))
    for name, df in curves_by_strategy.items():
        ax.plot(df["datetime_utc"], df["fees_uncollected_or_compounded"], label=f"{name} fees")
    ax.set_title("Modelled fees")
    ax.set_xlabel("Time UTC")
    ax.set_ylabel("USD")
    ax.grid(True, alpha=0.3)
    ax.legend()
    plots.append(save(fig, f"{prefix}_fees.png"))

    fig, ax = plt.subplots(figsize=(12, 5))
    first = next(iter(curves_by_strategy.values()))
    ax.plot(first["datetime_utc"], first["price"], label="price USDC/WETH")
    ax.set_title("Swap-derived price from tick")
    ax.set_xlabel("Time UTC")
    ax.set_ylabel("USDC per WETH")
    ax.grid(True, alpha=0.3)
    ax.legend()
    plots.append(save(fig, f"{prefix}_price.png"))

    return plots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--events", required=True, help="events_all.csv/parquet or swaps.csv/parquet")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--strategy", default="both", choices=["wide", "adaptive", "both"])
    ap.add_argument("--initial-capital-usd", type=float, default=10000.0)
    ap.add_argument("--fee-tier-bps", type=float, default=30.0, help="30 bps = 0.30%")
    ap.add_argument("--active-liquidity-usd", type=float, default=10000000.0, help="Calibration knob for active in-range liquidity.")
    ap.add_argument("--reference-width-pct", type=float, default=40.0)
    ap.add_argument("--min-concentration-boost", type=float, default=0.1)
    ap.add_argument("--max-concentration-boost", type=float, default=50.0)

    # Wide benchmark
    ap.add_argument("--wide-lower-pct", type=float, default=25.0)
    ap.add_argument("--wide-upper-pct", type=float, default=18.0)

    # Adaptive strategy
    ap.add_argument("--inner-width-pct", type=float, default=1.0)
    ap.add_argument("--outer-offset-pct", type=float, default=0.5)
    ap.add_argument("--outer-width-pct", type=float, default=1.5)
    ap.add_argument("--inner-capital-pct", type=float, default=40.0)
    ap.add_argument("--lower-capital-pct", type=float, default=25.0)
    ap.add_argument("--upper-capital-pct", type=float, default=25.0)
    ap.add_argument("--rebalance-hours", type=float, default=12.0)
    ap.add_argument("--gas-per-rebalance-usd", type=float, default=25.0)
    ap.add_argument("--rebalance-swap-cost-bps", type=float, default=5.0)

    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    swaps = load_swaps(args.events)
    strategies = ["wide", "adaptive"] if args.strategy == "both" else [args.strategy]

    curves_by_strategy = {}
    summaries = {}

    for strategy in strategies:
        curves = simulate(swaps, strategy, args)
        curves_by_strategy[strategy] = curves
        summaries[strategy] = summarize(curves, args, strategy)
        curves.to_csv(out_dir / f"{strategy}_curves.csv", index=False)

    plots = save_plot(curves_by_strategy, out_dir, prefix="lp_proxy")

    result = {
        "input_events": str(args.events),
        "out_dir": str(out_dir),
        "summaries": summaries,
        "plots": plots,
        "model_warning": (
            "This is a proxy event backtester. Exact LP fee replay requires historical active tick liquidity "
            "and feeGrowthInside reconstruction."
        ),
    }
    (out_dir / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
