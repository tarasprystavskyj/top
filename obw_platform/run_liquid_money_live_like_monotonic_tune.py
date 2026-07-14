#!/usr/bin/env python3
"""Live-like liquid-money tuner with fixed minimum order notional.

Research-only. Reads local OHLCV NPZ files and writes reports. It does not call
exchanges, read secrets, or place orders.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import random
import time
from pathlib import Path
from typing import Dict, Iterable, List, Tuple

import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from eval_stubborn_champion_cross_npz import load_slice, npz_symbols
from strategies.liquid_money_interval_ema_numba import (
    build_prior_interval,
    simulate_liquid_money_interval_stubborn_only_win_live_like,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_NPZ = ROOT.parent / "DB" / "fast_cache_1m_liquid_money_profitable_1y_bingx_20260708.npz"
DEFAULT_PYTH = (
    ROOT
    / "_reports"
    / "liquid_money_sei_pyth_priority_narrow_tune"
    / "run_20260708_101232"
    / "PYTH_USDT_USDT"
    / "best_so_far.json"
)
LOCKED_LONG_LEVERAGE = 1.0
LOCKED_SHORT_LEVERAGE = 1.0


def write_csv(path: Path, rows: List[Dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields: List[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def load_npz_symbol_map(npz_paths: List[Path]) -> Dict[str, Tuple[Path, int, int]]:
    out: Dict[str, Tuple[Path, int, int]] = {}
    for path in npz_paths:
        for sym, start, end in npz_symbols(path):
            if end - start < 5000:
                continue
            out.setdefault(sym, (path, start, end))
    return out


def safe_symbol(symbol: str) -> str:
    return symbol.replace("/", "_").replace(":", "_")


def load_json_if_exists(path: Path) -> Dict[str, float]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def load_champion_map(path: Path) -> Dict[str, Path]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    out: Dict[str, Path] = {}
    for key, value in raw.items():
        out[str(key)] = Path(str(value))
    return out


def candidate_rows(rng: random.Random, n_random: int, base: Dict[str, float], grid_limit: int = 0) -> Iterable[Tuple[int, int, int, float, float, float, int, float]]:
    base_lb = int(base.get("lookback", 360))
    lookbacks = sorted({120, 180, 240, 360, 480, 720, 960, 1440, max(60, base_lb // 2), base_lb, base_lb * 2})
    fast_lens = sorted({3, 5, 7, 9, 12, 16, 21, int(base.get("fast_len", 7))})
    slow_lens = sorted({34, 45, 55, 89, 120, 144, 180, 240, int(base.get("slow_len", 89))})
    caps = sorted({25.0, 35.0, 45.0, 55.0, 65.0, 75.0, 90.0, float(base.get("interval_cap_pct", 65.0))})
    gammas = sorted({0.75, 1.0, 1.25, 1.5, 2.0, float(base.get("gamma", 1.0))})
    min_profits = sorted({0.03, 0.05, 0.08, 0.12, 0.18, 0.25, float(base.get("min_profit_pct", 0.08))})
    max_holds = sorted({720, 1440, 2160, 2880, 4320, 7200, 10080, int(base.get("max_hold_bars", 2880))})
    loss_cuts = sorted({-18.0, -25.0, -35.0, -50.0, -65.0, float(base.get("loss_cut_mtm_pct", -50.0))})

    grid_count = 0
    for lb, fast, slow, cap, gamma, mp, mh in itertools.product(
        [240, 360, 480, 720, 960],
        [5, 7, 9, 12, 16],
        [55, 89, 144, 180],
        [35.0, 45.0, 55.0, 65.0, 75.0],
        [1.0, 1.25, 1.5],
        [0.05, 0.08, 0.12, 0.18],
        [1440, 2880, 4320],
    ):
        if fast >= slow:
            continue
        if grid_limit > 0 and grid_count >= grid_limit:
            break
        grid_count += 1
        yield lb, fast, slow, cap, gamma, mp, mh, -50.0

    for _ in range(n_random):
        fast = rng.choice(fast_lens)
        slow = rng.choice(slow_lens)
        if fast >= slow:
            fast, slow = min(fast, slow), max(fast, slow)
            if fast == slow:
                slow += 5
        yield (
            rng.choice(lookbacks),
            fast,
            slow,
            rng.choice(caps),
            rng.choice(gammas),
            rng.choice(min_profits),
            rng.choice(max_holds),
            rng.choice(loss_cuts),
        )


def score_row(row: Dict[str, float], min_trades: int, max_dd_soft: float, stagnation_soft_days: float) -> float:
    if row["trades"] < min_trades:
        return -1e12 - (min_trades - row["trades"]) * 100000.0
    # Primary objective: realized + unrealized mark-to-market PnL.
    net = row.get("final_total_mtm_pct", row["net_pct"])
    excess = row["excess_vs_buy_hold_pct"]
    dd = abs(row.get("mtm_mdd_pct", row["max_dd_pct"]))
    pf = min(row["profit_factor"], 10.0)
    win = row["win_rate"]
    forced_penalty = row["forced_losses"] * 0.18
    frozen_penalty = max(0.0, row["open_exposure_pct"] - 60.0) * 4.0
    dd_penalty = max(0.0, dd - max_dd_soft) * 95.0
    bh_penalty = max(0.0, row["buy_hold_pct"] - net) * 750.0
    stagnation_penalty = max(0.0, row["max_stagnation_days"] - stagnation_soft_days) * 1200.0
    stagnation_drag = row["max_stagnation_days"] * 8.0
    return (
        net * 850.0
        + excess * 450.0
        + pf * 80.0
        + win * 5.0
        - dd * 35.0
        - dd_penalty
        - forced_penalty
        - frozen_penalty
        - bh_penalty
        - stagnation_penalty
        - stagnation_drag
    )


def _ema_next(prev: float, value: float, length: int) -> float:
    return (2.0 / (length + 1.0)) * value + (1.0 - 2.0 / (length + 1.0)) * prev


def replay_live_like(
    d: Dict[str, np.ndarray],
    cfg: Dict[str, float],
    initial_equity: float,
    fee: float,
    slip: float,
    min_step_notional: float,
    long_leverage: float,
    short_leverage: float,
) -> pd.DataFrame:
    close = d["close"]
    ts = d["timestamp_s"]
    prior_hi, prior_lo = build_prior_interval(d["high"], d["low"], int(cfg["lookback"]))
    fast_len = int(cfg["fast_len"])
    slow_len = int(cfg["slow_len"])
    cap = float(cfg["interval_cap_pct"])
    gamma = float(cfg["gamma"])
    min_profit = float(cfg["min_profit_pct"])
    max_hold = int(cfg["max_hold_bars"])
    loss_cut = float(cfg["loss_cut_mtm_pct"])

    fast = float(close[0])
    slow = float(close[0])
    side = 0
    exposure = 0.0
    avg_entry = 0.0
    entry_i = 0
    realized = 0.0
    rows = []

    for i in range(1, len(close)):
        px = float(close[i])
        fast_prev, slow_prev = fast, slow
        fast = _ema_next(fast, px, fast_len)
        slow = _ema_next(slow, px, slow_len)
        buy = fast_prev <= slow_prev and fast > slow
        sell = fast_prev >= slow_prev and fast < slow
        rng = max(float(prior_hi[i] - prior_lo[i]), max(abs(px) * 1e-6, 1e-12))
        long_target = min(100.0, cap * (max(0.0, (float(prior_hi[i]) - px) / rng) ** gamma))
        short_target = min(100.0, cap * (max(0.0, (px - float(prior_lo[i])) / rng) ** gamma))

        def lev_for(s: int) -> float:
            return long_leverage if s > 0 else short_leverage

        def min_step_pct_for(s: int) -> float:
            return 100.0 * min_step_notional / max(initial_equity * lev_for(s), 1e-12)

        def current_unreal() -> float:
            if side > 0 and exposure > 0:
                return initial_equity * long_leverage * exposure / 100.0 * (px / max(avg_entry, 1e-12) - 1.0 - fee - slip)
            if side < 0 and exposure > 0:
                return initial_equity * short_leverage * exposure / 100.0 * (avg_entry / max(px, 1e-12) - 1.0 - fee - slip)
            return 0.0

        mtm_pct_now = (realized + current_unreal()) / initial_equity * 100.0
        if side != 0 and exposure > 0 and ((max_hold > 0 and i - entry_i >= max_hold) or (loss_cut < 0 and mtm_pct_now <= loss_cut)):
            notional = initial_equity * lev_for(side) * exposure / 100.0
            ret = px / max(avg_entry, 1e-12) - 1.0 if side > 0 else avg_entry / max(px, 1e-12) - 1.0
            realized += notional * (ret - fee - slip)
            side, exposure, avg_entry = 0, 0.0, 0.0

        if buy:
            if side < 0:
                if px < avg_entry * (1.0 - min_profit / 100.0):
                    reduce_pct = min(exposure, min_step_pct_for(-1))
                    notional = initial_equity * short_leverage * reduce_pct / 100.0
                    realized += notional * (avg_entry / max(px, 1e-12) - 1.0 - fee - slip)
                    exposure -= reduce_pct
                    if exposure <= 1e-9:
                        side, exposure, avg_entry = 0, 0.0, 0.0
                else:
                    add_pct = min(100.0 - exposure, max(short_target - exposure, min_step_pct_for(-1)))
                    if add_pct > 0:
                        avg_entry = (avg_entry * exposure + px * add_pct) / max(exposure + add_pct, 1e-12)
                        exposure += add_pct
            else:
                add_pct = min(100.0 - exposure, max(long_target - exposure, min_step_pct_for(1)))
                if add_pct > 0:
                    if side == 0:
                        avg_entry = px
                        side = 1
                        entry_i = i
                    else:
                        avg_entry = (avg_entry * exposure + px * add_pct) / max(exposure + add_pct, 1e-12)
                    exposure += add_pct

        if sell:
            if side > 0:
                if px > avg_entry * (1.0 + min_profit / 100.0):
                    reduce_pct = min(exposure, min_step_pct_for(1))
                    notional = initial_equity * long_leverage * reduce_pct / 100.0
                    realized += notional * (px / max(avg_entry, 1e-12) - 1.0 - fee - slip)
                    exposure -= reduce_pct
                    if exposure <= 1e-9:
                        side, exposure, avg_entry = 0, 0.0, 0.0
                else:
                    add_pct = min(100.0 - exposure, max(long_target - exposure, min_step_pct_for(1)))
                    if add_pct > 0:
                        avg_entry = (avg_entry * exposure + px * add_pct) / max(exposure + add_pct, 1e-12)
                        exposure += add_pct
            else:
                add_pct = min(100.0 - exposure, max(short_target - exposure, min_step_pct_for(-1)))
                if add_pct > 0:
                    if side == 0:
                        avg_entry = px
                        side = -1
                        entry_i = i
                    else:
                        avg_entry = (avg_entry * exposure + px * add_pct) / max(exposure + add_pct, 1e-12)
                    exposure += add_pct

        unreal = current_unreal()
        rows.append((int(ts[i]), realized / initial_equity * 100.0, unreal / initial_equity * 100.0, (realized + unreal) / initial_equity * 100.0, exposure, side))

    return pd.DataFrame(rows, columns=["timestamp_s", "realized_pct", "unrealized_pct", "total_pct", "exposure_pct", "side"])


def curve_stats(df: pd.DataFrame) -> Dict[str, float]:
    total = df["total_pct"].to_numpy(np.float64)
    eq = 100.0 + total
    peak = np.maximum.accumulate(eq)
    dd = (eq - peak) / np.maximum(peak, 1e-12) * 100.0
    peak_i = 0
    max_stag = 0
    best = eq[0] if len(eq) else 100.0
    for i, v in enumerate(eq):
        if v > best:
            best = v
            peak_i = i
        else:
            max_stag = max(max_stag, i - peak_i)
    return {
        "final_total_pct": float(total[-1]) if len(total) else 0.0,
        "mtm_mdd_pct": float(dd.min()) if len(dd) else 0.0,
        "max_stagnation_days": float(max_stag / 1440.0),
        "worst_unrealized_pct": float(df["unrealized_pct"].min()) if len(df) else 0.0,
    }


def plot_curve(name: str, df: pd.DataFrame, d: Dict[str, np.ndarray], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    step = max(1, len(df) // 10000)
    ds = df.iloc[::step].copy()
    close = d["close"][-len(df):]
    price = pd.DataFrame({
        "timestamp_s": df["timestamp_s"].to_numpy(),
        "buy_hold_pct": (close / max(close[0], 1e-12) - 1.0) * 100.0,
        "price_norm": close / max(close[0], 1e-12) * 100.0,
    }).iloc[::step].copy()
    x = pd.to_datetime(ds["timestamp_s"], unit="s", utc=True)
    xp = pd.to_datetime(price["timestamp_s"], unit="s", utc=True)
    fig, ax = plt.subplots(figsize=(15, 7))
    ax.plot(x, ds["realized_pct"], label="realized %", linewidth=1.1)
    ax.plot(x, ds["unrealized_pct"], label="unrealized %", linewidth=0.85)
    ax.plot(x, ds["total_pct"], label="total MTM %", linewidth=1.35)
    ax.plot(xp, price["buy_hold_pct"], label="buy-and-hold %", linewidth=1.0, alpha=0.8)
    ax.fill_between(x, ds["unrealized_pct"], 0, alpha=0.10)
    ax.grid(True, alpha=0.25)
    ax.legend(loc="best")
    ax.set_title(name)
    ax.set_ylabel("%")
    ax2 = ax.twinx()
    ax2.plot(xp, price["price_norm"], color="black", alpha=0.22, linewidth=0.8)
    ax2.set_ylabel("price, start=100")
    fig.tight_layout()
    fig.savefig(out_dir / f"{name.replace('/', '_').replace(':', '_')}.png", dpi=150)
    plt.close(fig)


def tune_symbol(
    symbol: str,
    d: Dict[str, np.ndarray],
    base: Dict[str, float],
    out_dir: Path,
    random_n: int,
    seed: int,
    initial_equity: float,
    fee: float,
    slippage: float,
    min_step_notional: float,
    long_leverage: float,
    short_leverage: float,
    min_trades: int,
    max_dd_soft: float,
    stagnation_soft_days: float,
    flush_every: int,
    grid_limit: int = 0,
) -> Dict[str, object]:
    safe = safe_symbol(symbol)
    sym_dir = out_dir / safe
    sym_dir.mkdir(parents=True, exist_ok=True)
    close = d["close"]
    buy_hold_pct = float((close[-1] / max(close[0], 1e-12) - 1.0) * 100.0)
    stable_symbol_seed = sum((i + 1) * ord(ch) for i, ch in enumerate(symbol))
    rng = random.Random(seed + stable_symbol_seed)
    lookback_cache: Dict[int, Tuple[np.ndarray, np.ndarray]] = {}
    rows: List[Dict[str, float]] = []
    best: Dict[str, float] | None = None
    started = time.time()

    for idx, cand in enumerate(candidate_rows(rng, random_n, base, grid_limit), 1):
        lookback, fast, slow, cap, gamma, min_profit, max_hold, loss_cut = cand
        if lookback not in lookback_cache:
            lookback_cache[lookback] = build_prior_interval(d["high"], d["low"], int(lookback))
        prior_hi, prior_lo = lookback_cache[lookback]
        res = simulate_liquid_money_interval_stubborn_only_win_live_like(
            d["high"],
            d["low"],
            d["close"],
            prior_hi,
            prior_lo,
            int(fast),
            int(slow),
            float(cap),
            float(min_step_notional),
            float(gamma),
            float(min_profit),
            int(max_hold),
            float(loss_cut),
            float(fee),
            float(slippage),
            float(initial_equity),
            float(long_leverage),
            float(short_leverage),
        )
        row = {
            "rank_tmp": 0,
            "symbol": symbol,
            "lookback": lookback,
            "fast_len": fast,
            "slow_len": slow,
            "interval_cap_pct": cap,
            "min_step_notional_usdt": min_step_notional,
            "gamma": gamma,
            "min_profit_pct": min_profit,
            "max_hold_bars": max_hold,
            "loss_cut_mtm_pct": loss_cut,
            "leverage_long": long_leverage,
            "leverage_short": short_leverage,
            "net_pct": float(res[2]),
            "final_total_mtm_pct": float(res[2]),
            "realized_pct": float(res[0] / initial_equity * 100.0),
            "unrealized_pct": float(res[1] / initial_equity * 100.0),
            "max_dd_pct": float(res[3]),
            "mtm_mdd_pct": float(res[3]),
            "min_mtm_pct": float(res[4]),
            "trades": float(res[5]),
            "win_rate": float(res[7]),
            "profit_factor": float(res[8]),
            "max_exposure_pct": float(res[9]),
            "open_exposure_pct": float(res[10]),
            "forced_losses": float(res[13]),
            "stubborn_adds": float(res[14]),
            "max_stagnation_bars": float(res[22]),
            "max_stagnation_days": float(res[22] / 1440.0),
            "buy_hold_pct": buy_hold_pct,
            "excess_vs_buy_hold_pct": float(res[2] - buy_hold_pct),
            "score": 0.0,
        }
        row["score"] = score_row(row, min_trades, max_dd_soft, stagnation_soft_days)
        rows.append(row)
        if best is None or row["score"] > best["score"]:
            best = dict(row)
            best["candidate_index"] = idx
            (sym_dir / "best_so_far.json").write_text(json.dumps(best, indent=2), encoding="utf-8")
            print("[best]", symbol, json.dumps(best, default=str))
        if idx % flush_every == 0:
            top = sorted(rows, key=lambda x: x["score"], reverse=True)[:200]
            for rank, r in enumerate(top, 1):
                r["rank_tmp"] = rank
            write_csv(sym_dir / "top_candidates.csv", top)
            (sym_dir / "heartbeat.json").write_text(
                json.dumps({"idx": idx, "elapsed_sec": time.time() - started, "best": best}, indent=2),
                encoding="utf-8",
            )
            print("[progress]", symbol, idx, "best_net", None if best is None else best["net_pct"], "best_stag", None if best is None else best["max_stagnation_days"])

    top = sorted(rows, key=lambda x: x["score"], reverse=True)[:500]
    for rank, r in enumerate(top, 1):
        r["rank_tmp"] = rank
    write_csv(sym_dir / "top_candidates.csv", top)
    if top:
        (sym_dir / "best_so_far.json").write_text(json.dumps(top[0], indent=2), encoding="utf-8")
        df = replay_live_like(d, top[0], initial_equity, fee, slippage, min_step_notional, long_leverage, short_leverage)
        df.to_csv(sym_dir / f"{safe}_champion_live_like_curve.csv", index=False)
        plot_curve(f"{safe}_champion_live_like", df, d, sym_dir)
    summary = {
        "symbol": symbol,
        "bars": int(len(close)),
        "from_utc": str(pd.to_datetime(d["timestamp_s"][0], unit="s", utc=True)),
        "to_utc": str(pd.to_datetime(d["timestamp_s"][-1], unit="s", utc=True)),
        "buy_hold_pct": buy_hold_pct,
        "best": top[0] if top else None,
        "elapsed_sec": time.time() - started,
        "candidates": len(rows),
    }
    (sym_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    return summary


def pair_report(
    run_dir: Path,
    anchor_symbol: str,
    anchor_cfg_path: Path,
    symbol_map: Dict[str, Tuple[Path, int, int]],
    summaries: List[Dict[str, object]],
    initial_equity: float,
    fee: float,
    slippage: float,
    min_step_notional: float,
    long_leverage: float,
    short_leverage: float,
    stagnation_soft_days: float,
) -> None:
    if anchor_symbol not in symbol_map or not anchor_cfg_path.exists():
        return
    anchor_path, anchor_start, anchor_end = symbol_map[anchor_symbol]
    anchor_d = load_slice(anchor_path, anchor_start, anchor_end)
    anchor_cfg = json.loads(anchor_cfg_path.read_text(encoding="utf-8"))
    anchor_df = replay_live_like(anchor_d, anchor_cfg, initial_equity, fee, slippage, min_step_notional, long_leverage, short_leverage)
    rows: List[Dict[str, object]] = []
    pair_dir = run_dir / "pair_vs_anchor"
    pair_dir.mkdir(exist_ok=True)
    anchor_name = anchor_symbol.replace("/", "_").replace(":", "_")
    anchor_df.to_csv(pair_dir / f"{anchor_name}_anchor_live_like_curve.csv", index=False)
    for item in summaries:
        sym = str(item.get("symbol", ""))
        if not sym or sym == anchor_symbol:
            continue
        safe = sym.replace("/", "_").replace(":", "_")
        curve_path = run_dir / safe / f"{safe}_champion_live_like_curve.csv"
        if not curve_path.exists():
            continue
        df = pd.read_csv(curve_path)
        merged = anchor_df[["timestamp_s", "total_pct", "realized_pct", "unrealized_pct"]].merge(
            df[["timestamp_s", "total_pct", "realized_pct", "unrealized_pct"]],
            on="timestamp_s",
            how="inner",
            suffixes=("_anchor", "_candidate"),
        )
        if len(merged) < 1000:
            continue
        port = pd.DataFrame({"timestamp_s": merged["timestamp_s"]})
        for col in ["total_pct", "realized_pct", "unrealized_pct"]:
            port[col] = (merged[f"{col}_anchor"] + merged[f"{col}_candidate"]) / 2.0
        stats = curve_stats(port)
        a_delta = merged["total_pct_anchor"].diff().fillna(0.0).to_numpy(np.float64)
        c_delta = merged["total_pct_candidate"].diff().fillna(0.0).to_numpy(np.float64)
        delta_corr = float(np.corrcoef(a_delta, c_delta)[0, 1]) if np.std(a_delta) > 1e-12 and np.std(c_delta) > 1e-12 else 0.0
        level_corr = float(np.corrcoef(merged["total_pct_anchor"], merged["total_pct_candidate"])[0, 1])
        pair_score = (
            stats["final_total_pct"] * 700.0
            - abs(stats["mtm_mdd_pct"]) * 55.0
            - max(0.0, stats["max_stagnation_days"] - stagnation_soft_days) * 1300.0
            - max(0.0, delta_corr) * 20000.0
            + max(0.0, -delta_corr) * 5000.0
        )
        rows.append({
            "anchor_symbol": anchor_symbol,
            "candidate_symbol": sym,
            "portfolio_final_total_pct": stats["final_total_pct"],
            "portfolio_mtm_mdd_pct": stats["mtm_mdd_pct"],
            "portfolio_max_stagnation_days": stats["max_stagnation_days"],
            "portfolio_worst_unrealized_pct": stats["worst_unrealized_pct"],
            "delta_corr": delta_corr,
            "level_corr": level_corr,
            "pair_score": pair_score,
            "candidate_net_pct": float((item.get("best") or {}).get("net_pct", 0.0)),
            "candidate_stagnation_days": float((item.get("best") or {}).get("max_stagnation_days", 0.0)),
        })
        if len(rows) <= 3:
            port.to_csv(pair_dir / f"{anchor_name}__{safe}_portfolio_curve.csv", index=False)
    rows.sort(key=lambda r: float(r["pair_score"]), reverse=True)
    write_csv(pair_dir / f"pair_candidates_vs_{anchor_name}.csv", rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", action="append", default=[])
    ap.add_argument("--symbols", default="")
    ap.add_argument("--champion", default=str(DEFAULT_PYTH))
    ap.add_argument("--champion-map", default="")
    ap.add_argument("--out", default=str(ROOT / "_reports" / "liquid_money_live_like_monotonic_tune"))
    ap.add_argument("--random", type=int, default=12000)
    ap.add_argument("--seed", type=int, default=2026071101)
    ap.add_argument("--limit-bars", type=int, default=0)
    ap.add_argument("--initial-equity", type=float, default=100.0)
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slippage", type=float, default=0.0009380229915652661)
    ap.add_argument("--min-step-notional-usdt", type=float, default=2.5)
    ap.add_argument("--long-leverage", type=float, default=LOCKED_LONG_LEVERAGE)
    ap.add_argument("--short-leverage", type=float, default=LOCKED_SHORT_LEVERAGE)
    ap.add_argument("--min-trades", type=int, default=200)
    ap.add_argument("--max-dd-soft", type=float, default=45.0)
    ap.add_argument("--stagnation-soft-days", type=float, default=90.0)
    ap.add_argument("--flush-every", type=int, default=500)
    ap.add_argument("--grid-limit", type=int, default=0)
    ap.add_argument("--pair-anchor-symbol", default="PYTH/USDT:USDT")
    ap.add_argument("--pair-anchor-config", default=str(DEFAULT_PYTH))
    args = ap.parse_args()

    if abs(float(args.long_leverage) - LOCKED_LONG_LEVERAGE) > 1e-12 or abs(float(args.short_leverage) - LOCKED_SHORT_LEVERAGE) > 1e-12:
        raise SystemExit(
            f"Leverage is locked for this pipeline: long={LOCKED_LONG_LEVERAGE}x, short={LOCKED_SHORT_LEVERAGE}x. "
            "Do not increase leverage to improve backtest profit."
        )

    if not args.npz:
        args.npz = [str(DEFAULT_NPZ)]

    run_dir = Path(args.out) / time.strftime("run_%Y%m%d_%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=False)
    default_base = load_json_if_exists(Path(args.champion))
    champion_map = load_champion_map(Path(args.champion_map)) if args.champion_map else {}
    npz_paths = [Path(p) for p in args.npz if Path(p).exists()]
    symbol_map = load_npz_symbol_map(npz_paths)
    wanted = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not wanted:
        wanted = sorted(symbol_map)

    (run_dir / "run_config.json").write_text(json.dumps(vars(args), indent=2), encoding="utf-8")
    summaries = []
    for sym in wanted:
        if sym not in symbol_map:
            print("[skip]", sym, "not found in NPZ")
            continue
        path, start, end = symbol_map[sym]
        d = load_slice(path, start, end)
        if args.limit_bars > 0:
            for k in ["timestamp_s", "high", "low", "close"]:
                d[k] = d[k][-args.limit_bars :]
        base_path = champion_map.get(sym) or champion_map.get(safe_symbol(sym))
        base = load_json_if_exists(base_path) if base_path else default_base
        summaries.append(
            tune_symbol(
                sym,
                d,
                base,
                run_dir,
                args.random,
                args.seed,
                args.initial_equity,
                args.fee,
                args.slippage,
                args.min_step_notional_usdt,
                args.long_leverage,
                args.short_leverage,
                args.min_trades,
                args.max_dd_soft,
                args.stagnation_soft_days,
                args.flush_every,
                args.grid_limit,
            )
        )
        summaries.sort(key=lambda x: float((x.get("best") or {}).get("score", -1e99)), reverse=True)
        (run_dir / "summary.json").write_text(json.dumps({"run_dir": str(run_dir), "summaries": summaries}, indent=2), encoding="utf-8")
        flat = []
        for item in summaries:
            best = item.get("best") or {}
            row = {k: v for k, v in item.items() if k != "best"}
            for k, v in best.items():
                row[f"best_{k}"] = v
            flat.append(row)
        write_csv(run_dir / "summary.csv", flat)

    pair_report(
        run_dir,
        args.pair_anchor_symbol,
        Path(args.pair_anchor_config),
        symbol_map,
        summaries,
        args.initial_equity,
        args.fee,
        args.slippage,
        args.min_step_notional_usdt,
        args.long_leverage,
        args.short_leverage,
        args.stagnation_soft_days,
    )
    print(json.dumps({"run_dir": str(run_dir), "symbols": [s["symbol"] for s in summaries]}, indent=2))


if __name__ == "__main__":
    main()
