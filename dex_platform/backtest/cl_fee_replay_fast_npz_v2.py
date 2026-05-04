#!/usr/bin/env python3
from __future__ import annotations

"""
dex_platform/backtest/cl_fee_replay_fast_npz_v2.py

Fast NPZ-based DEX LP fee replay and tuner.

v2 fixes:
1. fee_share = our_liquidity / (active_liquidity + our_liquidity)
2. reports max/avg liquidity share
3. supports hard share cap and score penalty
4. separates fees_earned_total / fees_reinvested / fees_uncollected_end / rebalance_costs
5. supports month filtering
6. supports out-of-range-only rebalance mode

Strategy spec:
  name:lower_pct:upper_pct
  name:lower_pct:upper_pct:rebalance_hours:gas_usd:swap_cost_bps[:mode]

Modes:
  none
  periodic
  oor
"""

import argparse
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Tuple

import numpy as np
import pandas as pd


SCRIPT_VERSION = "cl_fee_replay_fast_npz_v2_2026_05_02"


@dataclass
class Strategy:
    name: str
    lower_pct: float
    upper_pct: float
    rebalance_hours: float = 0.0
    gas_usd: float = 0.0
    swap_cost_bps: float = 0.0
    mode: str = "none"


def print_version() -> None:
    print(f"[script_version] {__file__} SCRIPT_VERSION={SCRIPT_VERSION}")


def parse_iso_ts(s: str) -> int:
    if not s:
        return 0
    x = pd.Timestamp(s)
    if x.tzinfo is None:
        x = x.tz_localize("UTC")
    else:
        x = x.tz_convert("UTC")
    return int(x.timestamp())


def load_npz(path: str | Path) -> Dict[str, Any]:
    p = Path(path)
    if not p.exists():
        raise SystemExit(f"npz not found: {p}")
    z = np.load(p, allow_pickle=False)
    out = {k: z[k] for k in z.files}
    meta = {}
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
        raise SystemExit(f"time filter produced empty slice: from={time_from} to={time_to}")

    out = dict(data)
    for k in ["ts", "block", "log_index", "tick", "price", "amount0_h", "amount1_h", "input_usd", "active_liquidity"]:
        if k in out:
            out[k] = out[k][mask]
    return out


def sqrt_raw_token1_per_token0_from_price(price_token0_per_token1: np.ndarray | float, dec0: int, dec1: int) -> np.ndarray:
    p = np.asarray(price_token0_per_token1, dtype=np.float64)
    q_raw = (10 ** (dec1 - dec0)) / np.maximum(p, 1e-300)
    return np.sqrt(q_raw)


def amounts_raw_for_liquidity_vec(
    liquidity_raw: float,
    price: np.ndarray,
    lower_price: float,
    upper_price: float,
    dec0: int,
    dec1: int,
) -> Tuple[np.ndarray, np.ndarray]:
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


def max_drawdown_pct(equity: np.ndarray) -> float:
    if len(equity) == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = equity / np.where(peak == 0, np.nan, peak) - 1.0
    return float(np.nanmin(dd) * 100.0)


def parse_fee_specs(spec: str) -> List[Tuple[str, float]]:
    out = []
    for item in spec.split(","):
        item = item.strip()
        if item:
            name, rate = item.split(":", 1)
            out.append((name, float(rate)))
    return out


def parse_strategy(spec: str) -> Strategy:
    parts = spec.split(":")
    if len(parts) < 3:
        raise ValueError(f"bad strategy spec: {spec}")
    return Strategy(
        name=parts[0],
        lower_pct=float(parts[1]),
        upper_pct=float(parts[2]),
        rebalance_hours=float(parts[3]) if len(parts) > 3 and parts[3] else 0.0,
        gas_usd=float(parts[4]) if len(parts) > 4 and parts[4] else 0.0,
        swap_cost_bps=float(parts[5]) if len(parts) > 5 and parts[5] else 0.0,
        mode=str(parts[6]) if len(parts) > 6 and parts[6] else ("periodic" if len(parts) > 3 else "none"),
    )


def liquidity_share(our_liq: float, active_liq: np.ndarray | float) -> np.ndarray:
    return float(our_liq) / (np.asarray(active_liq, dtype=np.float64) + float(our_liq))


def static_backtest(price, input_usd, active_liq, ts, dec0, dec1, initial_capital, strategy, fee_rate, want_curve):
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

    share_in = share[in_range]
    summary = {
        "strategy": strategy.name,
        "lower_pct": strategy.lower_pct,
        "upper_pct": strategy.upper_pct,
        "rebalance_hours": strategy.rebalance_hours,
        "rebalance_mode": strategy.mode,
        "initial_capital_usd": initial_capital,
        "equity_end_usd": float(equity[-1]),
        "return_pct": float((equity[-1] / initial_capital - 1.0) * 100.0),
        "mdd_pct": max_drawdown_pct(equity),
        "fees_earned_total": float(fees_cum[-1]),
        "fees_reinvested": 0.0,
        "fees_uncollected_end": float(fees_cum[-1]),
        "rebalance_costs": 0.0,
        "position_value_end_usd": float(pos_value[-1]),
        "time_in_range_pct": float(in_range.mean() * 100.0),
        "avg_liquidity_share_pct_when_in_range": float(share_in.mean() * 100.0) if len(share_in) else 0.0,
        "max_liquidity_share_pct_when_in_range": float(share_in.max() * 100.0) if len(share_in) else 0.0,
        "rebalances": 0,
        "hodl50_return_pct": float((hodl50[-1] / initial_capital - 1.0) * 100.0),
        "vs_hodl50_usd": float(equity[-1] - hodl50[-1]),
        "price_start": p0,
        "price_end": float(price[-1]),
        "price_return_pct": float((price[-1] / p0 - 1.0) * 100.0),
    }

    curve = None
    if want_curve:
        curve = pd.DataFrame({
            "timestamp": ts,
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
        curve["datetime_utc"] = pd.to_datetime(curve["timestamp"], unit="s", utc=True)

    return summary, curve


def periodic_backtest(price, input_usd, active_liq, ts, dec0, dec1, initial_capital, strategy, fee_rate, want_curve):
    p0 = float(price[0])
    lower = p0 * (1.0 - strategy.lower_pct / 100.0)
    upper = p0 * (1.0 + strategy.upper_pct / 100.0)
    capital = initial_capital
    our_liq = liquidity_for_capital(capital, p0, lower, upper, dec0, dec1)

    last_reb_ts = int(ts[0])
    fees_uncollected = 0.0
    fees_earned_total = 0.0
    fees_reinvested = 0.0
    costs_cum = 0.0
    rebalances = 0

    equity_arr = np.empty_like(price, dtype=np.float64)
    fees_total_arr = np.empty_like(price, dtype=np.float64)
    fees_uncol_arr = np.empty_like(price, dtype=np.float64)
    pos_arr = np.empty_like(price, dtype=np.float64)
    in_arr = np.zeros_like(price, dtype=np.int8)
    share_arr = np.zeros_like(price, dtype=np.float64)

    hodl50 = initial_capital / 2.0 + (initial_capital / 2.0 / p0) * price

    for i in range(len(price)):
        p = float(price[i])
        t = int(ts[i])
        in_range_now = lower <= p <= upper

        should_rebalance = False
        if strategy.rebalance_hours > 0 and t - last_reb_ts >= strategy.rebalance_hours * 3600:
            if strategy.mode == "oor":
                should_rebalance = not in_range_now
            else:
                should_rebalance = True

        if should_rebalance:
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
            last_reb_ts = t
            rebalances += 1
            in_range_now = True

        sh = float(liquidity_share(our_liq, active_liq[i]))
        share_arr[i] = sh

        if in_range_now:
            earned = input_usd[i] * fee_rate * sh
            fees_uncollected += earned
            fees_earned_total += earned
            in_arr[i] = 1

        a0, a1 = amounts_raw_for_liquidity_vec(our_liq, np.array([p]), lower, upper, dec0, dec1)
        pos_val = float(value_usd_from_raw(a0, a1, np.array([p]), dec0, dec1)[0])
        equity_arr[i] = pos_val + fees_uncollected
        pos_arr[i] = pos_val
        fees_total_arr[i] = fees_earned_total
        fees_uncol_arr[i] = fees_uncollected

    share_in = share_arr[in_arr == 1]
    summary = {
        "strategy": strategy.name,
        "lower_pct": strategy.lower_pct,
        "upper_pct": strategy.upper_pct,
        "rebalance_hours": strategy.rebalance_hours,
        "rebalance_mode": strategy.mode,
        "initial_capital_usd": initial_capital,
        "equity_end_usd": float(equity_arr[-1]),
        "return_pct": float((equity_arr[-1] / initial_capital - 1.0) * 100.0),
        "mdd_pct": max_drawdown_pct(equity_arr),
        "fees_earned_total": float(fees_earned_total),
        "fees_reinvested": float(fees_reinvested),
        "fees_uncollected_end": float(fees_uncollected),
        "rebalance_costs": float(costs_cum),
        "position_value_end_usd": float(pos_arr[-1]),
        "time_in_range_pct": float(in_arr.mean() * 100.0),
        "avg_liquidity_share_pct_when_in_range": float(share_in.mean() * 100.0) if len(share_in) else 0.0,
        "max_liquidity_share_pct_when_in_range": float(share_in.max() * 100.0) if len(share_in) else 0.0,
        "rebalances": int(rebalances),
        "hodl50_return_pct": float((hodl50[-1] / initial_capital - 1.0) * 100.0),
        "vs_hodl50_usd": float(equity_arr[-1] - hodl50[-1]),
        "price_start": p0,
        "price_end": float(price[-1]),
        "price_return_pct": float((price[-1] / p0 - 1.0) * 100.0),
    }

    curve = None
    if want_curve:
        curve = pd.DataFrame({
            "timestamp": ts,
            "price": price,
            "equity": equity_arr,
            "position_value": pos_arr,
            "fees_earned_total": fees_total_arr,
            "fees_uncollected": fees_uncol_arr,
            "in_range": in_arr,
            "liquidity_share_pct": share_arr * 100.0,
            "hodl50": hodl50,
        })
        curve["datetime_utc"] = pd.to_datetime(curve["timestamp"], unit="s", utc=True)

    return summary, curve


def parse_float_list(s: str) -> List[float]:
    return [float(x.strip()) for x in s.split(",") if x.strip()]


def score_row(row: Dict[str, Any], args: argparse.Namespace) -> float:
    ret = float(row["return_pct"])
    mdd_abs = abs(float(row["mdd_pct"]))
    avg_share = float(row["avg_liquidity_share_pct_when_in_range"])
    max_share = float(row["max_liquidity_share_pct_when_in_range"])
    rebalances = float(row.get("rebalances", 0))

    score = ret
    score -= args.w_mdd * max(0.0, mdd_abs - args.target_mdd_pct)
    score -= args.w_avg_share * max(0.0, avg_share - args.max_avg_liquidity_share_pct)
    score -= args.w_max_share * max(0.0, max_share - args.max_liquidity_share_pct)
    score -= args.w_rebalance * rebalances

    if args.hard_max_liquidity_share and max_share > args.max_liquidity_share_pct:
        score = -1e12 + score

    return float(score)


def make_plots(curves: pd.DataFrame, summary: pd.DataFrame, out_dir: Path) -> None:
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
    ax.set_title("Fast NPZ fee replay v2 equity")
    ax.set_ylabel("USD")
    ax.grid(True, alpha=0.3)
    ax.legend(fontsize=8)
    fig.tight_layout()
    fig.savefig(plot_dir / "equity.png", dpi=160, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(12, 5))
    s = summary.sort_values("score", ascending=False)
    ax.bar(s["run_name"], s["score"])
    ax.set_title("Score by run")
    ax.set_ylabel("Score")
    ax.tick_params(axis="x", rotation=45)
    ax.grid(True, axis="y", alpha=0.3)
    fig.tight_layout()
    fig.savefig(plot_dir / "score_by_run.png", dpi=160, bbox_inches="tight")
    plt.close(fig)


def main() -> None:
    print_version()

    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--initial-capital-usd", type=float, default=1000.0)
    ap.add_argument("--fee-rates", default="metadata_0_2515:0.002515")
    ap.add_argument("--strategies", default="")
    ap.add_argument("--grid-lower", default="")
    ap.add_argument("--grid-upper", default="")
    ap.add_argument("--time-from", default="")
    ap.add_argument("--time-to", default="")
    ap.add_argument("--dec0", type=int, default=0)
    ap.add_argument("--dec1", type=int, default=0)
    ap.add_argument("--plots", action="store_true")

    ap.add_argument("--target-mdd-pct", type=float, default=25.0)
    ap.add_argument("--max-liquidity-share-pct", type=float, default=5.0)
    ap.add_argument("--max-avg-liquidity-share-pct", type=float, default=3.0)
    ap.add_argument("--hard-max-liquidity-share", action="store_true")
    ap.add_argument("--w-mdd", type=float, default=2.0)
    ap.add_argument("--w-max-share", type=float, default=10.0)
    ap.add_argument("--w-avg-share", type=float, default=5.0)
    ap.add_argument("--w-rebalance", type=float, default=0.02)

    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    data = load_npz(args.npz)
    data = filter_time(data, args.time_from, args.time_to)

    meta = data.get("meta", {})
    dec0 = args.dec0 or int(meta.get("dec0", 6))
    dec1 = args.dec1 or int(meta.get("dec1", 18))

    ts = data["ts"].astype(np.int64)
    price = data["price"].astype(np.float64)
    input_usd = data["input_usd"].astype(np.float64)
    active_liq = data["active_liquidity"].astype(np.float64)

    fee_specs = parse_fee_specs(args.fee_rates)

    strategies: List[Strategy] = []
    if args.strategies:
        strategies.extend(parse_strategy(x) for x in args.strategies.split(",") if x.strip())

    if args.grid_lower and args.grid_upper:
        for lo in parse_float_list(args.grid_lower):
            for up in parse_float_list(args.grid_upper):
                strategies.append(Strategy(name=f"grid_{lo:g}_{up:g}", lower_pct=lo, upper_pct=up))

    if not strategies:
        raise SystemExit("No strategies. Use --strategies or --grid-lower/--grid-upper.")

    rows: List[Dict[str, Any]] = []
    curves: List[pd.DataFrame] = []

    for fee_name, fee_rate in fee_specs:
        for st in strategies:
            want_curve = args.plots and len(strategies) <= 30
            if st.rebalance_hours > 0:
                s, c = periodic_backtest(price, input_usd, active_liq, ts, dec0, dec1, args.initial_capital_usd, st, fee_rate, want_curve)
            else:
                s, c = static_backtest(price, input_usd, active_liq, ts, dec0, dec1, args.initial_capital_usd, st, fee_rate, want_curve)

            run_name = f"{fee_name}__{st.name}"
            s["fee_scenario"] = fee_name
            s["fee_rate"] = fee_rate
            s["run_name"] = run_name
            s["script_version"] = SCRIPT_VERSION
            s["score"] = score_row(s, args)
            rows.append(s)

            if c is not None:
                c["run_name"] = run_name
                c["fee_scenario"] = fee_name
                curves.append(c)

    summary = pd.DataFrame(rows).sort_values(["fee_scenario", "score"], ascending=[True, False]).reset_index(drop=True)
    summary.to_csv(out_dir / "summary.csv", index=False)

    if curves:
        curves_df = pd.concat(curves, ignore_index=True)
        curves_df.to_csv(out_dir / "curves.csv", index=False)
        make_plots(curves_df, summary, out_dir)

    result = {
        "script_version": SCRIPT_VERSION,
        "npz": str(args.npz),
        "time_from": args.time_from,
        "time_to": args.time_to,
        "meta": meta,
        "rows": int(len(price)),
        "summary_csv": str(out_dir / "summary.csv"),
        "best_by_score": summary.sort_values("score", ascending=False).head(30).to_dict(orient="records"),
        "best_by_return": summary.sort_values("return_pct", ascending=False).head(30).to_dict(orient="records"),
    }
    (out_dir / "summary.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
