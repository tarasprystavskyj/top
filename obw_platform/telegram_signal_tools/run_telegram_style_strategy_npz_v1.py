#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Backtest Telegram-derived multi-asset momentum-pullback strategy on NPZ cache.

This is not a proof-of-edge strategy. It is a fast sanity-check runner derived from
observed Telegram signal structure: trend impulse -> pullback -> local turn -> TP ladder.
"""
import argparse
import csv
import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np


@dataclass
class Pos:
    symbol: str
    side: int
    entry_i: int
    entry_t: int
    entry: float
    notional0: float
    sl: float
    tp: Tuple[float, float, float]
    rem: float = 1.0
    tp_hits: int = 0
    meta_score: float = 0.0
    meta_mom: float = 0.0
    meta_pull: float = 0.0
    meta_turn: float = 0.0


def load_universe(path: str) -> List[str]:
    if not path:
        return []
    out = []
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip().upper()
        if not s or s.startswith("#"):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def base_of_symbol(sym: str) -> str:
    s = str(sym).upper()
    if "/" in s:
        return s.split("/", 1)[0]
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q):
            return s[:-len(q)]
    return s


def quote_of_symbol(sym: str) -> str:
    s = str(sym).upper()
    if "/" in s:
        rest = s.split("/", 1)[1]
        return rest.split(":", 1)[0]
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q):
            return q
    return ""


def load_npz(path: str):
    try:
        z = np.load(path, allow_pickle=False)
        symbols = [str(s) for s in z["symbols"]]
    except ValueError:
        # Older project NPZ files sometimes store symbols as object arrays.
        # Newly generated files from fetch_futures_ohlcv_npz_v1.py use unicode arrays and do not need pickle.
        z = np.load(path, allow_pickle=True)
        symbols = [str(s) for s in z["symbols"]]
    offsets = z["offsets"].astype(np.int64)
    ts = z["timestamp_s"].astype(np.int64)
    close = z["close"].astype(np.float64)
    data = {}
    for i, s in enumerate(symbols):
        a, b = int(offsets[i]), int(offsets[i + 1])
        if b > a:
            data[s] = (ts[a:b], close[a:b])
    return data


def signed_ret(side: int, entry: float, exitp: float) -> float:
    return side * (exitp / entry - 1.0)


def run_style_backtest(data: Dict[str, Tuple[np.ndarray, np.ndarray]], params: dict):
    fee_rate = float(params.get("fee_rate", 0.0005))
    slip_rate = float(params.get("slip_rate", 0.00092387))
    cost = fee_rate + slip_rate
    start_equity = float(params.get("start_equity", 1000.0))
    equity = start_equity
    risk_pct = float(params.get("risk_pct", 0.005))
    max_concurrent = int(params.get("max_concurrent", 8))
    max_gross = float(params.get("max_gross", 1.2))
    max_hold = int(params.get("max_hold_bars", 360))
    cooldown = int(params.get("cooldown_bars", 90))
    mom_bars = int(params.get("mom_bars", 180))
    pull_bars = int(params.get("pull_bars", 60))
    turn_bars = int(params.get("turn_bars", 3))
    mom_thr = float(params.get("mom_thr", 0.035))
    min_pull = float(params.get("min_pull", 0.004))
    max_pull = float(params.get("max_pull", 0.055))
    turn_thr = float(params.get("turn_thr", 0.0008))
    stop_pct_long = float(params.get("stop_pct_long", 0.0429))
    stop_pct_short = float(params.get("stop_pct_short", 0.0368))
    tp_long = tuple(float(x) for x in params.get("tp_long", [0.0134, 0.0408, 0.0807]))
    tp_short = tuple(float(x) for x in params.get("tp_short", [0.0139, 0.0381, 0.0759]))
    max_new_per_bar = int(params.get("max_new_per_bar", 2))
    min_rows = int(params.get("min_rows", 1000))

    series = {s: v for s, v in data.items() if len(v[1]) >= min_rows and not base_of_symbol(s).startswith("NC")}
    if not series:
        raise SystemExit("No symbols with enough rows")
    closes = {s: v[1].astype(float) for s, v in series.items()}
    times = {s: v[0].astype(int) for s, v in series.items()}
    syms = sorted(closes.keys())
    n = min(len(closes[s]) for s in syms)
    warm = max(mom_bars, pull_bars) + turn_bars + 5
    if n <= warm + 10:
        raise SystemExit(f"Not enough bars: n={n}, warm={warm}")

    last_exit = {s: -10**9 for s in syms}
    positions: List[Pos] = []
    trades: List[dict] = []
    curve: List[dict] = []

    def mtm_equity(i: int) -> float:
        u = 0.0
        for p in positions:
            cl = closes[p.symbol]
            px = float(cl[min(i, len(cl) - 1)])
            u += p.rem * p.notional0 * signed_ret(p.side, p.entry, px)
        return equity + u

    for i in range(warm, n):
        # exits first
        keep: List[Pos] = []
        for p in positions:
            px = float(closes[p.symbol][min(i, len(closes[p.symbol]) - 1)])
            realized = 0.0
            reason = None
            closed = False
            if i - p.entry_i >= max_hold:
                realized += p.rem * p.notional0 * (signed_ret(p.side, p.entry, px) - cost)
                reason = "timeout"
                closed = True
                p.rem = 0.0
            elif p.side == 1:
                if px <= p.sl:
                    realized += p.rem * p.notional0 * ((px / p.entry - 1.0) - cost)
                    reason = "sl" if p.tp_hits == 0 else "be_sl"
                    closed = True
                    p.rem = 0.0
                else:
                    while p.tp_hits < 3 and px >= p.tp[p.tp_hits] and p.rem > 1e-9:
                        part = 1.0 / 3.0 if p.tp_hits < 2 else p.rem
                        ex = p.tp[p.tp_hits]
                        realized += part * p.notional0 * ((ex / p.entry - 1.0) - cost)
                        p.rem -= part
                        p.tp_hits += 1
                        p.sl = p.entry
                        reason = "tp%d" % p.tp_hits
                    if p.rem <= 1e-9:
                        closed = True
            else:
                if px >= p.sl:
                    realized += p.rem * p.notional0 * ((p.entry / px - 1.0) - cost)
                    reason = "sl" if p.tp_hits == 0 else "be_sl"
                    closed = True
                    p.rem = 0.0
                else:
                    while p.tp_hits < 3 and px <= p.tp[p.tp_hits] and p.rem > 1e-9:
                        part = 1.0 / 3.0 if p.tp_hits < 2 else p.rem
                        ex = p.tp[p.tp_hits]
                        realized += part * p.notional0 * ((p.entry / ex - 1.0) - cost)
                        p.rem -= part
                        p.tp_hits += 1
                        p.sl = p.entry
                        reason = "tp%d" % p.tp_hits
                    if p.rem <= 1e-9:
                        closed = True
            equity += realized
            if closed:
                last_exit[p.symbol] = i
                trades.append({
                    "symbol": p.symbol,
                    "side": "long" if p.side == 1 else "short",
                    "entry_i": p.entry_i,
                    "exit_i": i,
                    "entry_t": dt.datetime.utcfromtimestamp(int(times[p.symbol][p.entry_i])).isoformat(),
                    "exit_t": dt.datetime.utcfromtimestamp(int(times[p.symbol][min(i, len(times[p.symbol]) - 1)])).isoformat(),
                    "entry": p.entry,
                    "exit": px,
                    "notional": p.notional0,
                    "tp_hits": p.tp_hits,
                    "reason": reason or "closed",
                    "pnl": realized,
                    "equity_after": equity,
                    "score": p.meta_score,
                    "mom": p.meta_mom,
                    "pull": p.meta_pull,
                    "turn": p.meta_turn,
                })
            else:
                keep.append(p)
        positions = keep

        # entries
        gross = sum(p.notional0 * p.rem for p in positions)
        slots = max_concurrent - len(positions)
        if slots > 0 and gross < equity * max_gross:
            candidates = []
            open_syms = {p.symbol for p in positions}
            for s in syms:
                if s in open_syms or i - last_exit.get(s, -10**9) < cooldown:
                    continue
                cl = closes[s]
                if i >= len(cl):
                    continue
                px = float(cl[i])
                if px <= 0 or not np.isfinite(px):
                    continue
                r_mom = px / float(cl[i - mom_bars]) - 1.0
                recent_high = float(np.max(cl[i - pull_bars:i + 1]))
                recent_low = float(np.min(cl[i - pull_bars:i + 1]))
                pull_from_high = (recent_high - px) / recent_high if recent_high > 0 else 0.0
                bounce_from_low = (px - recent_low) / recent_low if recent_low > 0 else 0.0
                turn = px / float(cl[i - turn_bars]) - 1.0
                if r_mom > mom_thr and min_pull <= pull_from_high <= max_pull and turn > turn_thr:
                    score = r_mom - pull_from_high * 0.5 + turn * 2.0
                    candidates.append((score, s, 1, stop_pct_long, tp_long, r_mom, pull_from_high, turn))
                if r_mom < -mom_thr and min_pull <= bounce_from_low <= max_pull and turn < -turn_thr:
                    score = (-r_mom) - bounce_from_low * 0.5 + (-turn) * 2.0
                    candidates.append((score, s, -1, stop_pct_short, tp_short, r_mom, bounce_from_low, turn))
            candidates.sort(reverse=True, key=lambda x: x[0])
            for score, s, side, stop_pct, tp_tuple, r_mom, pull, turn in candidates[:min(slots, max_new_per_bar)]:
                if gross >= equity * max_gross:
                    break
                px = float(closes[s][i])
                notional = min(equity * risk_pct / stop_pct, equity * max_gross - gross)
                if notional < 1.0:
                    continue
                equity -= notional * cost
                if side == 1:
                    sl = px * (1.0 - stop_pct)
                    tp = tuple(px * (1.0 + x) for x in tp_tuple)
                else:
                    sl = px * (1.0 + stop_pct)
                    tp = tuple(px * (1.0 - x) for x in tp_tuple)
                positions.append(Pos(
                    symbol=s, side=side, entry_i=i, entry_t=int(times[s][i]), entry=px,
                    notional0=notional, sl=sl, tp=tp, meta_score=score, meta_mom=r_mom,
                    meta_pull=pull, meta_turn=turn,
                ))
                gross += notional
        if i % 5 == 0:
            t_med = int(np.median([times[s][min(i, len(times[s]) - 1)] for s in syms]))
            curve.append({
                "i": i,
                "t": dt.datetime.utcfromtimestamp(t_med).isoformat(),
                "equity_realized": equity,
                "equity_mtm": mtm_equity(i),
                "open_positions": len(positions),
            })

    i = n - 1
    for p in positions:
        px = float(closes[p.symbol][min(i, len(closes[p.symbol]) - 1)])
        realized = p.rem * p.notional0 * (signed_ret(p.side, p.entry, px) - cost)
        equity += realized
        trades.append({
            "symbol": p.symbol,
            "side": "long" if p.side == 1 else "short",
            "entry_i": p.entry_i,
            "exit_i": i,
            "entry_t": dt.datetime.utcfromtimestamp(int(times[p.symbol][p.entry_i])).isoformat(),
            "exit_t": dt.datetime.utcfromtimestamp(int(times[p.symbol][min(i, len(times[p.symbol]) - 1)])).isoformat(),
            "entry": p.entry,
            "exit": px,
            "notional": p.notional0,
            "tp_hits": p.tp_hits,
            "reason": "eod",
            "pnl": realized,
            "equity_after": equity,
            "score": p.meta_score,
            "mom": p.meta_mom,
            "pull": p.meta_pull,
            "turn": p.meta_turn,
        })

    eq = np.asarray([r["equity_mtm"] for r in curve], dtype=float) if curve else np.asarray([start_equity])
    peak = np.maximum.accumulate(eq)
    dd = eq / peak - 1.0
    wins = sum(1 for t in trades if float(t["pnl"]) > 0)
    losses = sum(1 for t in trades if float(t["pnl"]) <= 0)
    gross_profit = sum(float(t["pnl"]) for t in trades if float(t["pnl"]) > 0)
    gross_loss = -sum(float(t["pnl"]) for t in trades if float(t["pnl"]) < 0)
    summary = {
        "start_equity": start_equity,
        "end_equity_realized": float(equity),
        "return_pct": float(equity / start_equity - 1.0),
        "mtm_max_dd_pct": float(dd.min()) if len(dd) else 0.0,
        "symbols": len(syms),
        "bars_min": int(n),
        "date_min": dt.datetime.utcfromtimestamp(int(min(times[s][0] for s in syms))).isoformat(),
        "date_max": dt.datetime.utcfromtimestamp(int(max(times[s][min(n - 1, len(times[s]) - 1)] for s in syms))).isoformat(),
        "trades": len(trades),
        "wins": wins,
        "losses": losses,
        "profit_factor": float(gross_profit / gross_loss) if gross_loss > 0 else None,
        "avg_pnl": float(np.mean([t["pnl"] for t in trades])) if trades else 0.0,
        "params": params,
    }
    return summary, trades, curve


def write_csv(path: str, rows: List[dict]) -> None:
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        Path(path).write_text("", encoding="utf-8")
        return
    keys = list(rows[0].keys())
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        for r in rows:
            w.writerow(r)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--universe-file", default="")
    ap.add_argument("--start-equity", type=float, default=1000.0)
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    ap.add_argument("--slip-rate", type=float, default=0.00092387)
    ap.add_argument("--risk-pct", type=float, default=0.005)
    ap.add_argument("--max-concurrent", type=int, default=8)
    ap.add_argument("--max-gross", type=float, default=1.2)
    ap.add_argument("--max-hold-bars", type=int, default=360)
    ap.add_argument("--mom-bars", type=int, default=180)
    ap.add_argument("--pull-bars", type=int, default=60)
    ap.add_argument("--turn-bars", type=int, default=3)
    ap.add_argument("--mom-thr", type=float, default=0.035)
    ap.add_argument("--min-pull", type=float, default=0.004)
    ap.add_argument("--max-pull", type=float, default=0.055)
    ap.add_argument("--turn-thr", type=float, default=0.0008)
    ap.add_argument("--min-rows", type=int, default=1000)
    ap.add_argument("--quote-filter", default="USDT", help="Comma-separated quotes to keep, default USDT. Use empty string for all.")
    args = ap.parse_args()

    data = load_npz(args.npz)
    quote_filter = {q.strip().upper() for q in args.quote_filter.split(",") if q.strip()}
    if quote_filter:
        data = {s: v for s, v in data.items() if quote_of_symbol(s) in quote_filter}
    wanted = set(load_universe(args.universe_file))
    if wanted:
        data = {s: v for s, v in data.items() if base_of_symbol(s) in wanted or s.upper() in wanted}
    params = {
        "start_equity": args.start_equity,
        "risk_pct": args.risk_pct,
        "max_concurrent": args.max_concurrent,
        "max_gross": args.max_gross,
        "max_hold_bars": args.max_hold_bars,
        "cooldown_bars": 90,
        "mom_bars": args.mom_bars,
        "pull_bars": args.pull_bars,
        "turn_bars": args.turn_bars,
        "mom_thr": args.mom_thr,
        "min_pull": args.min_pull,
        "max_pull": args.max_pull,
        "turn_thr": args.turn_thr,
        "fee_rate": args.fee_rate,
        "slip_rate": args.slip_rate,
        "stop_pct_long": 0.0429,
        "stop_pct_short": 0.0368,
        "tp_long": [0.0134, 0.0408, 0.0807],
        "tp_short": [0.0139, 0.0381, 0.0759],
        "max_new_per_bar": 2,
        "min_rows": args.min_rows,
    }
    summary, trades, curve = run_style_backtest(data, params)
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    (out / "telegram_style_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    write_csv(str(out / "telegram_style_trades.csv"), trades)
    write_csv(str(out / "telegram_style_equity_curve.csv"), curve)

    try:
        import matplotlib.pyplot as plt  # type: ignore
        if curve:
            xs = [dt.datetime.fromisoformat(r["t"]) for r in curve]
            plt.figure(figsize=(11, 5))
            plt.plot(xs, [r["equity_mtm"] for r in curve], label="MTM equity")
            plt.plot(xs, [r["equity_realized"] for r in curve], label="Realized equity")
            plt.title("Telegram-style multi-asset strategy")
            plt.xlabel("UTC")
            plt.ylabel("USDT")
            plt.legend()
            plt.tight_layout()
            plt.savefig(out / "telegram_style_equity.png", dpi=150)
            plt.close()
    except Exception as e:
        (out / "plot_error.txt").write_text(str(e), encoding="utf-8")

    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
