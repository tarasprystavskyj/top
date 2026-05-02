#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/backtest/cl_fee_replay_fast_npz_v3.py

Capacity-aware fast NPZ fee replay tuner for Aerodrome/Uniswap-v3-like CL LP ranges.

Keeps v2 logic:
- load Swap-only NPZ
- time filtering
- static range / periodic rebalance / out-of-range rebalance
- corrected fee share: our_liquidity / (active_liquidity + our_liquidity)
- separate fee accounting: earned / reinvested / uncollected / rebalance costs

v3 additions:
- capital grid / fixed / auto_cap modes
- avg/p95/p99/max liquidity-share metrics
- capacity-aware score
- deployable capital and return-on-total-capital metrics
- summary/best output files

This is a deterministic replay/tuning tool, not a live trading engine.
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import numpy as np
import pandas as pd

SCRIPT_VERSION = "cl_fee_replay_fast_npz_v3_capacity_2026_05_02"


@dataclass(frozen=True)
class Strategy:
    name: str
    lower_pct: float
    upper_pct: float
    rebalance_hours: float = 0.0
    gas_usd: float = 0.0
    swap_cost_bps: float = 0.0
    mode: str = "none"  # none | periodic | oor


def parse_iso_ts(s: str) -> int:
    x = pd.Timestamp(s)
    if x.tzinfo is None:
        x = x.tz_localize("UTC")
    else:
        x = x.tz_convert("UTC")
    return int(x.timestamp())


def parse_float_list(s: str) -> List[float]:
    return [float(x.strip()) for x in str(s).split(",") if x.strip()]


def parse_fee_specs(spec: str) -> List[Tuple[str, float]]:
    out: List[Tuple[str, float]] = []
    for item in str(spec).split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"Bad fee spec: {item}; expected name:rate")
        name, rate = item.split(":", 1)
        out.append((name, float(rate)))
    if not out:
        raise ValueError("No fee specs")
    return out


def parse_rebalance_grid(spec: str) -> List[Tuple[str, float]]:
    """Format: none:0,periodic:168,periodic:336,oor:24"""
    out: List[Tuple[str, float]] = []
    for item in str(spec).split(","):
        item = item.strip()
        if not item:
            continue
        if ":" not in item:
            raise ValueError(f"Bad rebalance item: {item}; expected mode:hours")
        mode, hours = item.split(":", 1)
        mode = mode.strip()
        if mode not in {"none", "periodic", "oor"}:
            raise ValueError(f"Bad rebalance mode: {mode}")
        out.append((mode, float(hours)))
    return out or [("none", 0.0)]


def parse_strategy(spec: str) -> Strategy:
    """name:lower:upper[:rebalance_hours[:gas_usd[:swap_cost_bps[:mode]]]]"""
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(f"Bad strategy spec: {spec}")
    reb = float(parts[3]) if len(parts) > 3 and parts[3] else 0.0
    gas = float(parts[4]) if len(parts) > 4 and parts[4] else 0.0
    swap = float(parts[5]) if len(parts) > 5 and parts[5] else 0.0
    mode = str(parts[6]) if len(parts) > 6 and parts[6] else ("periodic" if reb > 0 else "none")
    return Strategy(parts[0], float(parts[1]), float(parts[2]), reb, gas, swap, mode)


def load_npz(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"NPZ not found: {p}")
    z = np.load(p, allow_pickle=False)
    out: Dict[str, Any] = {k: z[k] for k in z.files}
    meta: Dict[str, Any] = {}
    if "meta_json" in out:
        meta = json.loads(str(out["meta_json"]))
    out["meta"] = meta
    return out


def filter_time(data: Dict[str, Any], time_from: str, time_to: str) -> Dict[str, Any]:
    ts = data["ts"].astype(np.int64)
    mask = np.ones(len(ts), dtype=bool)
    if time_from:
        mask &= ts >= parse_iso_ts(time_from)
    if time_to:
        mask &= ts < parse_iso_ts(time_to)
    if not mask.any():
        raise SystemExit(f"Empty time slice: {time_from} -> {time_to}")
    out = dict(data)
    for k in ["ts", "block", "log_index", "tick", "price", "amount0_h", "amount1_h", "input_usd", "active_liquidity"]:
        if k in out:
            out[k] = out[k][mask]
    return out


def sqrt_raw_token1_per_token0_from_price(price_token0_per_token1: np.ndarray | float, dec0: int, dec1: int) -> np.ndarray:
    p = np.asarray(price_token0_per_token1, dtype=np.float64)
    q_raw = (10 ** (dec1 - dec0)) / np.maximum(p, 1e-300)
    return np.sqrt(q_raw)


def amounts_raw_for_liquidity_vec(liquidity_raw: float, price: np.ndarray, lower_price: float, upper_price: float, dec0: int, dec1: int) -> Tuple[np.ndarray, np.ndarray]:
    p = np.asarray(price, dtype=np.float64)
    lo = max(float(lower_price), 1e-300)
    up = max(float(upper_price), lo * 1.000001)

    sqrt_p = sqrt_raw_token1_per_token0_from_price(p, dec0, dec1)
    sqrt_a = float(sqrt_raw_token1_per_token0_from_price(up, dec0, dec1))
    sqrt_b = float(sqrt_raw_token1_per_token0_from_price(lo, dec0, dec1))

    L = float(liquidity_raw)
    amount0 = np.zeros_like(p, dtype=np.float64)
    amount1 = np.zeros_like(p, dtype=np.float64)

    below = p <= lo
    above = p >= up
    mid = ~(below | above)

    amount0[below] = 0.0
    amount1[below] = L * (sqrt_b - sqrt_a)

    amount0[above] = L * (sqrt_b - sqrt_a) / (sqrt_a * sqrt_b)
    amount1[above] = 0.0

    amount0[mid] = L * (sqrt_b - sqrt_p[mid]) / (sqrt_p[mid] * sqrt_b)
    amount1[mid] = L * (sqrt_p[mid] - sqrt_a)
    return amount0, amount1


def value_usd_from_raw(amount0_raw: np.ndarray, amount1_raw: np.ndarray, price: np.ndarray, dec0: int, dec1: int) -> np.ndarray:
    return amount0_raw / (10 ** dec0) + amount1_raw / (10 ** dec1) * price


def liquidity_for_capital(capital_usd: float, open_price: float, lower_price: float, upper_price: float, dec0: int, dec1: int) -> float:
    p = np.array([open_price], dtype=np.float64)
    a0, a1 = amounts_raw_for_liquidity_vec(1.0, p, lower_price, upper_price, dec0, dec1)
    unit_value = float(value_usd_from_raw(a0, a1, p, dec0, dec1)[0])
    if unit_value <= 1e-300:
        return 0.0
    return float(capital_usd) / unit_value


def liquidity_share(our_liq: float, active_liq: np.ndarray | float) -> np.ndarray:
    return float(our_liq) / (np.asarray(active_liq, dtype=np.float64) + float(our_liq))


def max_drawdown_pct(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = equity / np.where(peak == 0, np.nan, peak) - 1.0
    return float(np.nanmin(dd) * 100.0)


def share_stats_pct(share_in: np.ndarray) -> Tuple[float, float, float, float]:
    if len(share_in) == 0:
        return 0.0, 0.0, 0.0, 0.0
    s = np.asarray(share_in, dtype=np.float64) * 100.0
    return float(np.mean(s)), float(np.percentile(s, 95)), float(np.percentile(s, 99)), float(np.max(s))


def base_summary(strategy: Strategy, initial_capital: float) -> Dict[str, Any]:
    return {
        "strategy": strategy.name,
        "lower_pct": float(strategy.lower_pct),
        "upper_pct": float(strategy.upper_pct),
        "rebalance_hours": float(strategy.rebalance_hours),
        "rebalance_mode": strategy.mode,
        "capital_usd": float(initial_capital),
        "initial_capital_usd": float(initial_capital),
    }


def finish_summary(row: Dict[str, Any], equity: np.ndarray, pos_value: np.ndarray, fee_total: float, fees_reinvested: float, fees_uncollected_end: float, rebalance_costs: float, in_range: np.ndarray, share: np.ndarray, hodl50: np.ndarray, price: np.ndarray, initial_capital: float, rebalances: int, total_capital_usd: float) -> Dict[str, Any]:
    share_in = share[in_range.astype(bool)]
    avg_s, p95_s, p99_s, max_s = share_stats_pct(share_in)
    profit = float(equity[-1] - initial_capital)
    row.update({
        "equity_end_usd": float(equity[-1]),
        "return_pct": float((equity[-1] / initial_capital - 1.0) * 100.0),
        "mdd_pct": max_drawdown_pct(equity),
        "fees_earned_total": float(fee_total),
        "fees_reinvested": float(fees_reinvested),
        "fees_uncollected_end": float(fees_uncollected_end),
        "rebalance_costs": float(rebalance_costs),
        "position_value_end_usd": float(pos_value[-1]),
        "time_in_range_pct": float(np.asarray(in_range, dtype=bool).mean() * 100.0),
        "avg_liquidity_share_pct_when_in_range": avg_s,
        "p95_liquidity_share_pct_when_in_range": p95_s,
        "p99_liquidity_share_pct_when_in_range": p99_s,
        "max_liquidity_share_pct_when_in_range": max_s,
        "rebalances": int(rebalances),
        "hodl50_return_pct": float((hodl50[-1] / initial_capital - 1.0) * 100.0),
        "vs_hodl50_usd": float(equity[-1] - hodl50[-1]),
        "price_start": float(price[0]),
        "price_end": float(price[-1]),
        "price_return_pct": float((price[-1] / price[0] - 1.0) * 100.0),
        "profit_usd": profit,
        "deployable_capital_usd": float(initial_capital),
        "unused_capital_usd": float(max(0.0, total_capital_usd - initial_capital)),
        "return_on_deployed_capital_pct": float((profit / initial_capital) * 100.0),
        "return_on_total_capital_pct": float((profit / total_capital_usd) * 100.0) if total_capital_usd else np.nan,
    })
    return row


def static_backtest(price: np.ndarray, input_usd: np.ndarray, active_liq: np.ndarray, ts: np.ndarray, dec0: int, dec1: int, initial_capital: float, strategy: Strategy, fee_rate: float, total_capital_usd: float, want_curve: bool = False) -> Tuple[Dict[str, Any], pd.DataFrame | None]:
    p0 = float(price[0])
    lower = p0 * (1.0 - strategy.lower_pct / 100.0)
    upper = p0 * (1.0 + strategy.upper_pct / 100.0)
    our_liq = liquidity_for_capital(initial_capital, p0, lower, upper, dec0, dec1)

    in_range = (price >= lower) & (price <= upper)
    share = liquidity_share(our_liq, active_liq)
    fee_events = np.zeros_like(price, dtype=np.float64)
    fee_events[in_range] = input_usd[in_range] * fee_rate * share[in_range]
    fees_cum = np.cumsum(fee_events)

    a0, a1 = amounts_raw_for_liquidity_vec(our_liq, price, lower, upper, dec0, dec1)
    pos_value = value_usd_from_raw(a0, a1, price, dec0, dec1)
    equity = pos_value + fees_cum
    hodl50 = initial_capital / 2.0 + (initial_capital / 2.0 / p0) * price

    row = finish_summary(base_summary(strategy, initial_capital), equity, pos_value, float(fees_cum[-1]), 0.0, float(fees_cum[-1]), 0.0, in_range.astype(np.int8), share, hodl50, price, initial_capital, 0, total_capital_usd)
    curve = None
    if want_curve:
        curve = pd.DataFrame({
            "timestamp": ts,
            "datetime_utc": pd.to_datetime(ts, unit="s", utc=True),
            "price": price,
            "equity": equity,
            "position_value": pos_value,
            "fees_earned_total": fees_cum,
            "fees_uncollected": fees_cum,
            "in_range": in_range.astype(np.int8),
            "liquidity_share_pct": share * 100.0,
            "hodl50": hodl50,
            "lower_price": lower,
            "upper_price": upper,
        })
    return row, curve


def periodic_backtest(price: np.ndarray, input_usd: np.ndarray, active_liq: np.ndarray, ts: np.ndarray, dec0: int, dec1: int, initial_capital: float, strategy: Strategy, fee_rate: float, total_capital_usd: float, want_curve: bool = False) -> Tuple[Dict[str, Any], pd.DataFrame | None]:
    n = len(price)
    p0 = float(price[0])
    lower = p0 * (1.0 - strategy.lower_pct / 100.0)
    upper = p0 * (1.0 + strategy.upper_pct / 100.0)
    capital = float(initial_capital)
    our_liq = liquidity_for_capital(capital, p0, lower, upper, dec0, dec1)

    last_reb_ts = int(ts[0])
    interval = int(strategy.rebalance_hours * 3600)
    fees_uncollected = 0.0
    fees_earned_total = 0.0
    fees_reinvested = 0.0
    costs_cum = 0.0
    rebalances = 0

    equity_arr = np.empty(n, dtype=np.float64)
    pos_arr = np.empty(n, dtype=np.float64)
    fees_total_arr = np.empty(n, dtype=np.float64)
    fees_uncol_arr = np.empty(n, dtype=np.float64)
    in_arr = np.zeros(n, dtype=np.int8)
    share_arr = np.zeros(n, dtype=np.float64)
    hodl50 = initial_capital / 2.0 + (initial_capital / 2.0 / p0) * price

    idx = 0
    while idx < n:
        if interval <= 0:
            next_idx = n
        else:
            start_reb = int(np.searchsorted(ts, last_reb_ts + interval, side="left"))
            if start_reb >= n:
                next_idx = n
            elif strategy.mode == "oor":
                out_mask = (price[start_reb:] < lower) | (price[start_reb:] > upper)
                next_idx = start_reb + int(np.argmax(out_mask)) if out_mask.any() else n
            else:
                next_idx = start_reb

        if next_idx > idx:
            sl = slice(idx, next_idx)
            pseg = price[sl]
            in_range = (pseg >= lower) & (pseg <= upper)
            share = liquidity_share(our_liq, active_liq[sl])
            fee_events = np.zeros_like(pseg, dtype=np.float64)
            fee_events[in_range] = input_usd[sl][in_range] * fee_rate * share[in_range]
            fees_cum = np.cumsum(fee_events)
            a0, a1 = amounts_raw_for_liquidity_vec(our_liq, pseg, lower, upper, dec0, dec1)
            pos_value = value_usd_from_raw(a0, a1, pseg, dec0, dec1)

            equity_arr[sl] = pos_value + fees_uncollected + fees_cum
            pos_arr[sl] = pos_value
            fees_total_arr[sl] = fees_earned_total + fees_cum
            fees_uncol_arr[sl] = fees_uncollected + fees_cum
            in_arr[sl] = in_range.astype(np.int8)
            share_arr[sl] = share

            segment_fees = float(fees_cum[-1]) if len(fees_cum) else 0.0
            fees_uncollected += segment_fees
            fees_earned_total += segment_fees
            idx = next_idx

        if idx >= n:
            break

        # Rebalance before processing current swap event, matching v2 behavior.
        p = float(price[idx])
        a0, a1 = amounts_raw_for_liquidity_vec(our_liq, np.array([p]), lower, upper, dec0, dec1)
        pos_val = float(value_usd_from_raw(a0, a1, np.array([p]), dec0, dec1)[0])
        redeploy = pos_val + fees_uncollected
        cost = strategy.gas_usd + redeploy * (strategy.swap_cost_bps / 10000.0)
        fees_reinvested += fees_uncollected
        fees_uncollected = 0.0
        costs_cum += cost
        redeploy = max(0.0, redeploy - cost)

        capital = redeploy
        lower = p * (1.0 - strategy.lower_pct / 100.0)
        upper = p * (1.0 + strategy.upper_pct / 100.0)
        our_liq = liquidity_for_capital(capital, p, lower, upper, dec0, dec1)
        last_reb_ts = int(ts[idx])
        rebalances += 1

    row = finish_summary(base_summary(strategy, initial_capital), equity_arr, pos_arr, fees_earned_total, fees_reinvested, fees_uncollected, costs_cum, in_arr, share_arr, hodl50, price, initial_capital, rebalances, total_capital_usd)
    curve = None
    if want_curve:
        curve = pd.DataFrame({
            "timestamp": ts,
            "datetime_utc": pd.to_datetime(ts, unit="s", utc=True),
            "price": price,
            "equity": equity_arr,
            "position_value": pos_arr,
            "fees_earned_total": fees_total_arr,
            "fees_uncollected": fees_uncol_arr,
            "in_range": in_arr,
            "liquidity_share_pct": share_arr * 100.0,
            "hodl50": hodl50,
        })
    return row, curve


def score_row(row: Dict[str, Any], args: argparse.Namespace) -> float:
    ret = float(row["return_pct"])
    mdd_abs = abs(float(row["mdd_pct"]))
    avg_s = float(row["avg_liquidity_share_pct_when_in_range"])
    p95_s = float(row["p95_liquidity_share_pct_when_in_range"])
    p99_s = float(row["p99_liquidity_share_pct_when_in_range"])
    max_s = float(row["max_liquidity_share_pct_when_in_range"])
    rebalances = float(row.get("rebalances", 0))

    score = ret
    score -= args.w_mdd * max(0.0, mdd_abs - args.target_mdd_pct)
    score -= args.w_avg_share * max(0.0, avg_s - args.max_avg_liquidity_share_pct)
    score -= args.w_p95_share * max(0.0, p95_s - args.max_p95_liquidity_share_pct)
    score -= args.w_p99_share * max(0.0, p99_s - args.max_p99_liquidity_share_pct)
    score -= args.w_max_share * max(0.0, max_s - args.max_liquidity_share_pct)
    score -= args.w_rebalance * rebalances
    return float(score)


def is_valid_capacity(row: Dict[str, Any], args: argparse.Namespace) -> bool:
    return (
        float(row["avg_liquidity_share_pct_when_in_range"]) <= args.max_avg_liquidity_share_pct
        and float(row["p95_liquidity_share_pct_when_in_range"]) <= args.max_p95_liquidity_share_pct
        and float(row["p99_liquidity_share_pct_when_in_range"]) <= args.max_p99_liquidity_share_pct
    )


def make_strategies(args: argparse.Namespace) -> List[Strategy]:
    strategies: List[Strategy] = []
    if args.strategies:
        strategies.extend(parse_strategy(x) for x in args.strategies.split(",") if x.strip())
    if args.grid_lower and args.grid_upper:
        for lo in parse_float_list(args.grid_lower):
            for up in parse_float_list(args.grid_upper):
                for mode, reb_h in parse_rebalance_grid(args.rebalance_grid):
                    name = f"wide_{lo:g}_{up:g}" if mode == "none" else f"{mode}_{lo:g}_{up:g}_{reb_h:g}h"
                    strategies.append(Strategy(name=name, lower_pct=lo, upper_pct=up, rebalance_hours=reb_h, gas_usd=args.gas_usd, swap_cost_bps=args.swap_cost_bps, mode=mode))
    # de-dupe
    seen = set()
    out = []
    for s in strategies:
        key = (s.name, s.lower_pct, s.upper_pct, s.rebalance_hours, s.mode, s.gas_usd, s.swap_cost_bps)
        if key not in seen:
            seen.add(key)
            out.append(s)
    if not out:
        raise SystemExit("No strategies. Use --strategies or --grid-lower/--grid-upper.")
    return out


def make_capitals(args: argparse.Namespace) -> List[float]:
    if args.capital_mode == "fixed":
        return [float(args.initial_capital_usd)]
    vals = parse_float_list(args.capital_grid)
    if not vals:
        raise SystemExit("Empty --capital-grid")
    return vals


def run_once(data: Dict[str, Any], args: argparse.Namespace, out_dir: Path, time_from: str, time_to: str, suffix: str = "") -> pd.DataFrame:
    d = filter_time(data, time_from, time_to)
    meta = d.get("meta", {})
    dec0 = args.dec0 or int(meta.get("dec0", 6))
    dec1 = args.dec1 or int(meta.get("dec1", 18))

    ts = d["ts"].astype(np.int64)
    price = d["price"].astype(np.float64)
    input_usd = d["input_usd"].astype(np.float64)
    active_liq = d["active_liquidity"].astype(np.float64)

    strategies = make_strategies(args)
    capitals = make_capitals(args)
    fee_specs = parse_fee_specs(args.fee_rates)

    rows: List[Dict[str, Any]] = []
    curves: List[pd.DataFrame] = []
    want_any_curve = bool(args.plots)

    for fee_name, fee_rate in fee_specs:
        for st in strategies:
            for cap in capitals:
                want_curve = want_any_curve and len(strategies) * len(capitals) <= args.max_plot_runs
                if st.mode == "none" or st.rebalance_hours <= 0:
                    row, curve = static_backtest(price, input_usd, active_liq, ts, dec0, dec1, cap, st, fee_rate, args.total_capital_usd, want_curve)
                else:
                    row, curve = periodic_backtest(price, input_usd, active_liq, ts, dec0, dec1, cap, st, fee_rate, args.total_capital_usd, want_curve)
                row.update({
                    "period_suffix": suffix,
                    "time_from": time_from,
                    "time_to": time_to,
                    "fee_scenario": fee_name,
                    "fee_rate": fee_rate,
                    "run_name": f"{fee_name}__{st.name}__cap_{cap:g}",
                    "script_version": SCRIPT_VERSION,
                })
                row["valid_capacity_avg_p95_p99"] = is_valid_capacity(row, args)
                row["valid_capacity_plus_max"] = bool(row["valid_capacity_avg_p95_p99"] and row["max_liquidity_share_pct_when_in_range"] <= args.max_liquidity_share_pct)
                row["score"] = score_row(row, args)
                rows.append(row)
                if curve is not None:
                    curve["run_name"] = row["run_name"]
                    curve["fee_scenario"] = fee_name
                    curves.append(curve)

    all_df = pd.DataFrame(rows)
    if args.capital_mode == "auto_cap":
        all_df.to_csv(out_dir / f"capital_grid_all{suffix}.csv", index=False)
        valid = all_df[all_df["valid_capacity_avg_p95_p99"]].copy()
        if len(valid):
            # Maximum deployable capital per strategy; tie-break by score.
            sort_cols = ["capital_usd", "score"]
            chosen = valid.sort_values(sort_cols, ascending=[False, False]).groupby(["fee_scenario", "strategy", "lower_pct", "upper_pct", "rebalance_mode", "rebalance_hours"], as_index=False).head(1)
            summary = chosen.sort_values("score", ascending=False).reset_index(drop=True)
        else:
            summary = all_df.sort_values("score", ascending=False).reset_index(drop=True)
    else:
        summary = all_df.sort_values("score", ascending=False).reset_index(drop=True)

    summary.to_csv(out_dir / f"summary{suffix}.csv", index=False)
    summary.sort_values("score", ascending=False).head(args.top_n).to_csv(out_dir / f"best_by_score{suffix}.csv", index=False)
    summary.sort_values("return_pct", ascending=False).head(args.top_n).to_csv(out_dir / f"best_by_return{suffix}.csv", index=False)

    valid_summary = summary[summary["valid_capacity_avg_p95_p99"]].copy()
    if len(valid_summary):
        best_capacity = valid_summary.sort_values("score", ascending=False).groupby(["strategy", "lower_pct", "upper_pct", "rebalance_mode", "rebalance_hours"], as_index=False).head(1)
        best_capacity.sort_values("score", ascending=False).to_csv(out_dir / f"best_capacity_by_strategy{suffix}.csv", index=False)

    if curves:
        curves_df = pd.concat(curves, ignore_index=True)
        curves_df.to_csv(out_dir / f"curves{suffix}.csv", index=False)
        make_plots(curves_df, summary, out_dir, suffix)

    result = {
        "script_version": SCRIPT_VERSION,
        "npz": str(args.npz),
        "time_from": time_from,
        "time_to": time_to,
        "rows": int(len(price)),
        "capital_mode": args.capital_mode,
        "summary_csv": str(out_dir / f"summary{suffix}.csv"),
        "best_by_score": summary.sort_values("score", ascending=False).head(20).to_dict(orient="records"),
        "best_by_return": summary.sort_values("return_pct", ascending=False).head(20).to_dict(orient="records"),
    }
    (out_dir / f"summary{suffix}.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def make_plots(curves: pd.DataFrame, summary: pd.DataFrame, out_dir: Path, suffix: str) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plot_dir = out_dir / "plots"
    plot_dir.mkdir(parents=True, exist_ok=True)

    fig, ax = plt.subplots(figsize=(12, 5))
    for run_name, g in curves.groupby("run_name"):
        ax.plot(g["datetime_utc"], g["equity"], label=run_name)
    first = curves[curves["run_name"] == curves["run_name"].iloc[0]]
    ax.plot(first["datetime_utc"], first["hodl50"], label="hodl50", linestyle="--")
    ax.set_title("Fast NPZ fee replay v3 equity")
    ax.set_ylabel("USD")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(plot_dir / f"equity_top_score{suffix}.png", dpi=160, bbox_inches="tight")
    plt.close(fig)

    if "capital_usd" in summary.columns:
        fig, ax = plt.subplots(figsize=(10, 5))
        ax.scatter(summary["capital_usd"], summary["return_pct"])
        ax.set_title("Return vs capital")
        ax.set_xlabel("capital_usd")
        ax.set_ylabel("return_pct")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(plot_dir / f"return_vs_capital{suffix}.png", dpi=160, bbox_inches="tight")
        plt.close(fig)

        fig, ax = plt.subplots(figsize=(10, 5))
        ax.scatter(summary["capital_usd"], summary["p95_liquidity_share_pct_when_in_range"])
        ax.set_title("p95 share vs capital")
        ax.set_xlabel("capital_usd")
        ax.set_ylabel("p95 liquidity share %")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(plot_dir / f"p95_share_vs_capital{suffix}.png", dpi=160, bbox_inches="tight")
        plt.close(fig)


def run_walkforward(data: Dict[str, Any], args: argparse.Namespace, out_dir: Path) -> None:
    periods = [
        ("_feb", "2026-02-01T00:00:00Z", "2026-03-01T00:00:00Z"),
        ("_mar", "2026-03-01T00:00:00Z", "2026-04-01T00:00:00Z"),
        ("_apr", "2026-04-01T00:00:00Z", "2026-05-01T00:00:00Z"),
        ("_quarter", "2026-02-01T00:00:00Z", "2026-05-01T00:00:00Z"),
        ("_febmar", "2026-02-01T00:00:00Z", "2026-04-01T00:00:00Z"),
    ]
    outputs = {}
    for suffix, fr, to in periods:
        outputs[suffix] = run_once(data, args, out_dir, fr, to, suffix=suffix)

    # Basic walk-forward: take top score on train, evaluate same strategy/capital on test summaries.
    train_test = [("_feb", "_mar", "tune_feb_eval_mar"), ("_mar", "_apr", "tune_mar_eval_apr"), ("_febmar", "_apr", "tune_febmar_eval_apr")]
    wf_rows = []
    for train_key, test_key, label in train_test:
        train = outputs[train_key].sort_values("score", ascending=False)
        test = outputs[test_key]
        if train.empty:
            continue
        champ = train.iloc[0]
        mask = (
            (test["strategy"] == champ["strategy"])
            & (test["capital_usd"] == champ["capital_usd"])
            & (test["fee_scenario"] == champ["fee_scenario"])
        )
        if mask.any():
            ev = test[mask].iloc[0]
            wf_rows.append({
                "wf_case": label,
                "train_strategy": champ["strategy"],
                "capital_usd": champ["capital_usd"],
                "train_return_pct": champ["return_pct"],
                "train_mdd_pct": champ["mdd_pct"],
                "train_score": champ["score"],
                "test_return_pct": ev["return_pct"],
                "test_mdd_pct": ev["mdd_pct"],
                "test_score": ev["score"],
                "test_p95_share": ev["p95_liquidity_share_pct_when_in_range"],
                "test_p99_share": ev["p99_liquidity_share_pct_when_in_range"],
                "test_max_share": ev["max_liquidity_share_pct_when_in_range"],
            })
    pd.DataFrame(wf_rows).to_csv(out_dir / "walkforward.csv", index=False)

    # Monthly breakdown for top quarter configs.
    top_q = outputs["_quarter"].sort_values("score", ascending=False).head(20)
    monthly_rows = []
    for _, champ in top_q.iterrows():
        for suffix in ["_feb", "_mar", "_apr", "_quarter"]:
            df = outputs[suffix]
            mask = (df["strategy"] == champ["strategy"]) & (df["capital_usd"] == champ["capital_usd"])
            if mask.any():
                r = df[mask].iloc[0].to_dict()
                r["source_period"] = suffix.strip("_")
                monthly_rows.append(r)
    pd.DataFrame(monthly_rows).to_csv(out_dir / "monthly_breakdown.csv", index=False)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--fee-rates", default="metadata_0_2515:0.002515")
    ap.add_argument("--time-from", default="")
    ap.add_argument("--time-to", default="")
    ap.add_argument("--dec0", type=int, default=0)
    ap.add_argument("--dec1", type=int, default=0)
    ap.add_argument("--strategies", default="")
    ap.add_argument("--grid-lower", default="")
    ap.add_argument("--grid-upper", default="")
    ap.add_argument("--rebalance-grid", default="none:0,periodic:168,periodic:336,oor:24")
    ap.add_argument("--gas-usd", type=float, default=0.0)
    ap.add_argument("--swap-cost-bps", type=float, default=0.0)

    ap.add_argument("--initial-capital-usd", type=float, default=1000.0)
    ap.add_argument("--total-capital-usd", type=float, default=1000.0)
    ap.add_argument("--capital-grid", default="25,50,75,100,125,150,175,200,250,300,500,1000")
    ap.add_argument("--capital-mode", choices=["fixed", "grid", "auto_cap"], default="fixed")
    ap.add_argument("--target-share-pct", type=float, default=5.0)
    ap.add_argument("--share-cap-metric", choices=["avg", "p95", "p99", "max"], default="p95")
    ap.add_argument("--max-avg-liquidity-share-pct", type=float, default=3.0)
    ap.add_argument("--max-p95-liquidity-share-pct", type=float, default=5.0)
    ap.add_argument("--max-p99-liquidity-share-pct", type=float, default=10.0)
    ap.add_argument("--max-liquidity-share-pct", type=float, default=25.0)

    ap.add_argument("--target-mdd-pct", type=float, default=25.0)
    ap.add_argument("--w-mdd", type=float, default=2.0)
    ap.add_argument("--w-avg-share", type=float, default=5.0)
    ap.add_argument("--w-p95-share", type=float, default=10.0)
    ap.add_argument("--w-p99-share", type=float, default=3.0)
    ap.add_argument("--w-max-share", type=float, default=0.5)
    ap.add_argument("--w-rebalance", type=float, default=0.02)

    ap.add_argument("--walkforward", action="store_true")
    ap.add_argument("--plots", action="store_true")
    ap.add_argument("--max-plot-runs", type=int, default=30)
    ap.add_argument("--top-n", type=int, default=50)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    data = load_npz(args.npz)

    if args.walkforward:
        run_walkforward(data, args, out_dir)
    else:
        if not args.time_from or not args.time_to:
            raise SystemExit("Use --time-from and --time-to, or --walkforward")
        run_once(data, args, out_dir, args.time_from, args.time_to)

    print(json.dumps({"script_version": SCRIPT_VERSION, "out_dir": str(out_dir)}, indent=2))


if __name__ == "__main__":
    main()
