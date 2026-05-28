#!/usr/bin/env python3
"""Bounded HYPE DCA parameter search with MTM risk metrics.

Research-only. Uses local HYPE 1m NPZ and Binance copy closed positions.
It does not place orders, read secrets, or call network APIs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import random
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compare_binance_copy_positions_dca import read_positions  # noqa: E402
from telegram_signal_dca_compare import max_drawdown, ret_for  # noqa: E402


DEFAULT_REPORT_DIR = (
    Path("obw_platform")
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "binance_430051_hype_v21_loop_20260523"
)
DEFAULT_POSITIONS = DEFAULT_REPORT_DIR / "wave_002" / "position_refresh" / "position_history_normalized.csv"
DEFAULT_NPZ = DEFAULT_REPORT_DIR / "binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz"
MIN_ORDER_USD = 2.0
STRICT_FILL_MODE = "close_beyond_skip_boundary"
CANONICAL_RESEARCH_CHAMPION = "t500_b16_s0p25-0p35-0p55_w0p8-1p2-2p2"
GROUNDING_TOL = 1e-8


@dataclass(frozen=True)
class Candidate:
    name: str
    target_notional: float
    base_frac: float
    steps_pct: tuple[float, ...]
    add_weights: tuple[float, ...]
    fee: float = 0.0005
    slippage: float = 0.0009380229915652661


def iso(d: datetime) -> str:
    return d.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def load_npz_arrays(path: Path) -> Dict[str, np.ndarray]:
    z = np.load(path, allow_pickle=True)
    return {
        "t": z["timestamp_s"].astype(np.int64) * 1000,
        "open": z["open"].astype(float),
        "high": z["high"].astype(float),
        "low": z["low"].astype(float),
        "close": z["close"].astype(float),
    }




def apply_entry_source(
    positions: Sequence[Any],
    arrays: Dict[str, np.ndarray],
    entry_source: str,
) -> List[Any]:
    """Return positions with entry anchored to data available at/after signal time.

    avgCost is the historical Binance-copy average cost and may be a hindsight
    artifact for copy execution. The bar-based sources are offline approximations
    using local 1m OHLC only; they do not call private pages or live APIs.
    """
    if entry_source == "avgCost":
        return list(positions)
    t = arrays["t"]
    field_by_source = {
        "first_bar_open": "open",
        "first_bar_close": "close",
        "first_bar_high": "high",
        "first_bar_low": "low",
        "next_bar_open": "open",
    }
    if entry_source not in field_by_source:
        raise ValueError(f"unknown entry_source={entry_source!r}")
    out: List[Any] = []
    for pos in positions:
        i = int(np.searchsorted(t, ms(pos.opened), side="left"))
        if entry_source == "next_bar_open":
            i += 1
        i = min(max(i, 0), len(t) - 1)
        entry = float(arrays[field_by_source[entry_source]][i])
        out.append(replace(pos, entry=entry))
    return out


def apply_candidate_slippage(candidates: Sequence[Candidate], slippage_bp: float | None) -> List[Candidate]:
    if slippage_bp is None:
        return list(candidates)
    slip = float(slippage_bp) / 10000.0
    return [replace(c, slippage=slip) for c in candidates]

def allocations(candidate: Candidate) -> tuple[float, List[float]]:
    base = candidate.target_notional * candidate.base_frac
    remaining = max(candidate.target_notional - base, 0.0)
    total_w = sum(candidate.add_weights)
    adds = [remaining * w / total_w for w in candidate.add_weights] if total_w > 0 else []
    return base, adds


def dca_levels(side: str, entry: float, steps_pct: Sequence[float]) -> List[float]:
    levels: List[float] = []
    last = entry
    for step in steps_pct:
        if side == "LONG":
            last *= 1.0 - step / 100.0
        else:
            last *= 1.0 + step / 100.0
        levels.append(last)
    return levels


def level_crossed(side: str, *, low: float, high: float, close: float, level: float, fill_mode: str) -> bool:
    if fill_mode in {"close_beyond", "close_beyond_skip_boundary"}:
        return close <= level if side == "LONG" else close >= level
    return low <= level if side == "LONG" else high >= level


def min_order_ok(candidate: Candidate) -> tuple[bool, float]:
    base, adds = allocations(candidate)
    legs = [base, *adds]
    min_leg = min(legs) if legs else 0.0
    return min_leg >= MIN_ORDER_USD, min_leg


def research_label(candidate: Candidate, *, position_sizing_mode: str, max_target_notional: float | None) -> str:
    if candidate.name == CANONICAL_RESEARCH_CHAMPION and position_sizing_mode == "compound":
        return "grounded_compound_champion"
    if position_sizing_mode == "compound" and (max_target_notional is None or max_target_notional <= 500.0):
        return "grounded_compound_candidate"
    if position_sizing_mode == "fixed" and candidate.target_notional <= 500.0:
        return "static_500_cap"
    return "high_notional_illusion"


def simulate_position(
    pos: Any,
    candidate: Candidate,
    arrays: Dict[str, np.ndarray],
    *,
    fill_mode: str = "touch",
) -> Dict[str, Any] | None:
    t = arrays["t"]
    start = int(np.searchsorted(t, ms(pos.opened), side="left"))
    end = int(np.searchsorted(t, ms(pos.closed), side="right"))
    if end <= start:
        return None
    high = arrays["high"][start:end]
    low = arrays["low"][start:end]
    close = arrays["close"][start:end]
    ts = t[start:end]

    base_notional, adds = allocations(candidate)
    levels = dca_levels(pos.side, pos.entry, candidate.steps_pct[: len(adds)])
    avg_entry = float(pos.entry)
    notional = float(base_notional)
    fills = 0
    fill_rows: List[Dict[str, Any]] = []
    min_mtm = 0.0
    min_mtm_pct_on_notional = 0.0
    skip_boundary = fill_mode in {"touch_skip_boundary", "close_beyond_skip_boundary"}
    min_ok, min_leg = min_order_ok(candidate)

    for i in range(len(close)):
        can_fill = not (skip_boundary and (i == 0 or i == len(close) - 1))
        fills_this_candle = 0
        while can_fill and fills < len(levels):
            touched = level_crossed(
                pos.side,
                low=float(low[i]),
                high=float(high[i]),
                close=float(close[i]),
                level=float(levels[fills]),
                fill_mode=fill_mode,
            )
            if not touched:
                break
            if fills_this_candle >= 1 and skip_boundary:
                break
            add_notional = float(adds[fills])
            old_qty = notional / max(avg_entry, 1e-12)
            add_qty = add_notional / max(levels[fills], 1e-12)
            notional += add_notional
            avg_entry = notional / max(old_qty + add_qty, 1e-12)
            fill_rows.append({"level": levels[fills], "notional": add_notional, "t": int(ts[i])})
            fills += 1
            fills_this_candle += 1
        if skip_boundary and (i == 0 or i == len(close) - 1):
            continue
        mark = float(low[i] if pos.side == "LONG" else high[i])
        mtm_ret = ret_for(pos.side, avg_entry, mark) - 2 * candidate.fee - 2 * candidate.slippage
        mtm = mtm_ret * notional
        min_mtm = min(min_mtm, mtm)
        min_mtm_pct_on_notional = min(min_mtm_pct_on_notional, 100.0 * mtm / max(notional, 1e-12))

    gross_ret = ret_for(pos.side, avg_entry, pos.exit)
    net_ret = gross_ret - 2 * candidate.fee - 2 * candidate.slippage
    pnl = net_ret * notional
    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "side": pos.side,
        "opened_utc": iso(pos.opened),
        "closed_utc": iso(pos.closed),
        "entry": pos.entry,
        "exit": pos.exit,
        "avg_entry": avg_entry,
        "notional": notional,
        "dca_fills": fills,
        "pnl": pnl,
        "min_mtm": min_mtm,
        "min_mtm_pct_equity": 0.0,
        "min_mtm_pct_on_notional": min_mtm_pct_on_notional,
        "min_order_usd": min_leg,
        "min_order_ok": min_ok,
        "candles": len(close),
        "fill_mode": fill_mode,
        "fills_json": json.dumps(fill_rows, separators=(",", ":")),
    }


def grounding_stats(rows: Sequence[Dict[str, Any]], *, fill_mode: str, min_trade_mtm_pct: float) -> Dict[str, Any]:
    over = [
        r
        for r in rows
        if float(r.get("notional", 0.0)) - float(r.get("equity_before", float("inf"))) > GROUNDING_TOL
    ]
    strict_fill_ok = fill_mode == STRICT_FILL_MODE and all(str(r.get("fill_mode", "")) == STRICT_FILL_MODE for r in rows)
    min_trade = min((float(r.get("min_mtm_pct_equity", 0.0)) for r in rows), default=0.0)
    return {
        "notional_gt_equity_before_count": len(over),
        "notional_gt_equity_before_max": max(
            (float(r.get("notional", 0.0)) - float(r.get("equity_before", 0.0)) for r in over),
            default=0.0,
        ),
        "strict_fill_ok": strict_fill_ok,
        "min_trade_mtm_gate_ok": min_trade >= min_trade_mtm_pct,
        "grounded_compound_gate_ok": len(over) == 0 and strict_fill_ok and min_trade >= min_trade_mtm_pct,
    }


def leverage_stats(rows: Sequence[Dict[str, Any]], *, leverage: float, min_trade_mtm_pct: float) -> Dict[str, Any]:
    margin_calls = [r for r in rows if str(r.get("margin_call", "False")) == "True"]
    margin_over = [
        r
        for r in rows
        if float(r.get("margin_used", 0.0)) - float(r.get("equity_before", float("inf"))) > GROUNDING_TOL
    ]
    min_mtm_margin = min((float(r.get("min_mtm_pct_margin", 0.0)) for r in rows), default=0.0)
    min_trade = min((float(r.get("min_mtm_pct_equity", 0.0)) for r in rows), default=0.0)
    return {
        "leverage": leverage,
        "margin_call_count": len(margin_calls),
        "margin_call_rate_pct": 100.0 * len(margin_calls) / max(1, len(rows)),
        "margin_used_gt_equity_before_count": len(margin_over),
        "max_margin_used": max((float(r.get("margin_used", 0.0)) for r in rows), default=0.0),
        "avg_margin_used": sum(float(r.get("margin_used", 0.0)) for r in rows) / max(1, len(rows)),
        "min_trade_mtm_pct_margin": min_mtm_margin,
        "leveraged_margin_gate_ok": len(margin_calls) == 0 and len(margin_over) == 0 and min_trade >= min_trade_mtm_pct,
    }


def summarize(rows: Sequence[Dict[str, Any]], initial_equity: float) -> Dict[str, Any]:
    equity = initial_equity
    curve = [equity]
    mtm_curve = [equity]
    pnl_values: List[float] = []
    for row in rows:
        mtm_curve.append(equity + float(row["min_mtm"]))
        pnl = float(row["pnl"])
        pnl_values.append(pnl)
        equity += pnl
        curve.append(equity)
    wins = sum(1 for x in pnl_values if x > 0)
    gross_profit = sum(x for x in pnl_values if x > 0)
    gross_loss = sum(x for x in pnl_values if x < 0)
    min_mtm = min((float(r["min_mtm"]) for r in rows), default=0.0)
    min_mtm_pct_notional = min((float(r["min_mtm_pct_on_notional"]) for r in rows), default=0.0)
    return {
        "trades": len(rows),
        "equity_start": initial_equity,
        "equity_end": equity,
        "net_pct": 100.0 * (equity - initial_equity) / max(initial_equity, 1e-12),
        "max_realized_dd_pct": 100.0 * max_drawdown(curve),
        "max_mtm_dd_pct": 100.0 * max_drawdown(mtm_curve),
        "min_trade_mtm_pct_equity": 100.0 * min_mtm / max(initial_equity, 1e-12),
        "min_trade_mtm_pct_notional": min_mtm_pct_notional,
        "win_rate_pct": 100.0 * wins / max(1, len(rows)),
        "pf": gross_profit / abs(gross_loss) if gross_loss < 0 else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "avg_dca_fills": sum(float(r["dca_fills"]) for r in rows) / max(1, len(rows)),
        "avg_notional": sum(float(r["notional"]) for r in rows) / max(1, len(rows)),
        "max_notional": max((float(r["notional"]) for r in rows), default=0.0),
        "min_order_usd": min((float(r.get("min_order_usd", 0.0)) for r in rows), default=0.0),
        "min_order_ok": all(str(r.get("min_order_ok", "True")) == "True" for r in rows),
    }


def candidate_grid(target_scale: float, random_candidates: int, seed: int, max_target_notional: float | None) -> List[Candidate]:
    step_sets = [
        (0.25, 0.35, 0.55),
        (0.35, 0.50, 0.75),
        (0.45, 0.60, 0.90),
        (0.55, 0.80, 1.20),
        (0.35, 0.55, 0.80, 1.20),
        (0.45, 0.70, 1.00, 1.40),
        (0.55, 0.85, 1.25, 1.75),
        (0.35, 0.55, 0.80, 1.15, 1.60),
        (0.45, 0.70, 1.00, 1.40, 1.90),
        (0.60, 0.90, 1.30, 1.80, 2.40),
    ]
    weight_sets = [
        (1.0, 1.0, 1.2),
        (1.0, 1.3, 1.8),
        (0.8, 1.2, 2.2),
        (1.0, 1.0, 1.2, 1.6),
        (0.7, 1.0, 1.5, 2.4),
        (0.6, 0.9, 1.3, 1.8, 2.6),
    ]
    targets = (100.0, 150.0, 200.0, 250.0, 300.0, 400.0, 500.0, 650.0, 800.0, 1000.0, 1200.0)
    out = []
    for target in targets:
        scaled_target = target * target_scale
        if max_target_notional is not None and scaled_target > max_target_notional:
            continue
        out.append(Candidate(f"plain_no_dca_t{int(scaled_target)}", scaled_target, 1.0, (), ()))
        out.append(Candidate(f"current_like_dca3_t{int(scaled_target)}", scaled_target, 0.21739130434782608, (0.45, 0.35, 0.60), (1.1, 1.0, 1.5)))
        for base_frac in (0.16, 0.22, 0.28, 0.35):
            for steps in step_sets:
                valid_weights = [w for w in weight_sets if len(w) == len(steps)]
                for weights in valid_weights:
                    name = "t%03d_b%02d_s%s_w%s" % (
                        int(scaled_target),
                        int(base_frac * 100),
                        "-".join(str(x).replace(".", "p") for x in steps),
                        "-".join(str(x).replace(".", "p") for x in weights),
                    )
                    out.append(Candidate(name, scaled_target, base_frac, steps, weights))
    rng = random.Random(seed)
    random_targets = tuple(x for x in (100.0, 150.0, 200.0, 250.0, 300.0, 400.0, 500.0, 650.0, 800.0, 1000.0, 1200.0, 1500.0) if max_target_notional is None or x * target_scale <= max_target_notional)
    if not random_targets:
        random_targets = (max_target_notional / max(target_scale, 1e-12),) if max_target_notional else (500.0,)
    for i in range(max(0, random_candidates)):
        target = rng.choice(random_targets) * target_scale
        dca_count = rng.choice((3, 4, 5))
        base_frac = rng.choice((0.12, 0.16, 0.20, 0.24, 0.28, 0.35))
        first = rng.uniform(0.35, 0.75)
        steps = []
        last = first
        for _ in range(dca_count):
            last += rng.uniform(0.12, 0.55)
            steps.append(round(last, 3))
        weights = []
        w = rng.uniform(0.5, 1.2)
        for _ in range(dca_count):
            w *= rng.uniform(1.0, 1.65)
            weights.append(round(w, 3))
        name = "rnd%04d_t%d_b%02d_s%s_w%s" % (
            i,
            int(target),
            int(base_frac * 100),
            "-".join(str(x).replace(".", "p") for x in steps),
            "-".join(str(x).replace(".", "p") for x in weights),
        )
        out.append(Candidate(name, target, base_frac, tuple(steps), tuple(weights)))
    return out


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def monthly_splits(rows: Sequence[Dict[str, Any]], initial_equity: float) -> List[Dict[str, Any]]:
    by_month: Dict[str, List[Dict[str, Any]]] = {}
    for row in rows:
        month = str(row["closed_utc"])[:7]
        by_month.setdefault(month, []).append(row)
    out = []
    for month in sorted(by_month):
        s = summarize(by_month[month], initial_equity)
        out.append({"month": month, **s})
    return out


def annotate_trade_equity_metrics(rows: Sequence[Dict[str, Any]], initial_equity: float) -> None:
    for row in rows:
        row["min_mtm_pct_equity"] = 100.0 * float(row.get("min_mtm", 0.0)) / max(initial_equity, 1e-12)


def simulate_candidate_rows(
    positions: Sequence[Any],
    candidate: Candidate,
    arrays: Dict[str, np.ndarray],
    *,
    fill_mode: str,
    initial_equity: float,
    position_sizing_mode: str,
    leverage: float = 1.0,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    equity = float(initial_equity)
    initial_target = float(candidate.target_notional)
    target_frac = initial_target / max(initial_equity, 1e-12)
    leverage = max(float(leverage), 1.0)
    for pos in positions:
        effective = candidate
        if position_sizing_mode == "compound":
            effective_target = min(equity * leverage, equity * target_frac)
            effective = replace(candidate, target_notional=max(effective_target, 0.0))
        row = simulate_position(pos, effective, arrays, fill_mode=fill_mode)
        if row is None:
            continue
        row["initial_target_notional"] = initial_target
        row["effective_target_notional"] = effective.target_notional
        row["equity_before"] = equity
        margin_used = float(row["notional"]) / leverage
        row["leverage"] = leverage
        row["margin_used"] = margin_used
        row["min_mtm_pct_margin"] = 100.0 * float(row["min_mtm"]) / max(margin_used, 1e-12)
        row["margin_call"] = abs(float(row["min_mtm"])) >= margin_used - GROUNDING_TOL
        row["notional_gt_equity_before"] = float(row["notional"]) - equity > GROUNDING_TOL
        row["margin_used_gt_equity_before"] = margin_used - equity > GROUNDING_TOL
        equity += float(row["pnl"])
        row["equity_after"] = equity
        rows.append(row)
    return rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Bounded HYPE DCA parameter search.")
    ap.add_argument("--positions-csv", default=str(DEFAULT_POSITIONS))
    ap.add_argument("--npz", default=str(DEFAULT_NPZ))
    ap.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR / "dca_parameter_search_wave_001"))
    ap.add_argument("--initial-equity", type=float, default=500.0)
    ap.add_argument("--slippage-bp", type=float, default=None, help="Override per-side slippage in basis points, e.g. 4.25 for HYPE calibrated p95+buffer.")
    ap.add_argument(
        "--entry-source",
        default="avgCost",
        choices=("avgCost", "first_bar_open", "first_bar_close", "first_bar_high", "first_bar_low", "next_bar_open"),
        help="Entry anchor. avgCost is historical lead average cost; bar anchors are public OHLC approximations.",
    )
    ap.add_argument("--min-order-usd", type=float, default=MIN_ORDER_USD, help="Minimum allowed base/add leg notional for eligibility gates.")
    ap.add_argument("--target-scale", type=float, default=1.0)
    ap.add_argument("--max-target-notional", type=float, default=500.0)
    ap.add_argument("--position-sizing-mode", choices=("fixed", "compound"), default="compound")
    ap.add_argument("--leverage", type=float, default=1.0)
    ap.add_argument("--candidate-filter", default="")
    ap.add_argument("--random-candidates", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--max-mtm-dd-pct", type=float, default=50.0)
    ap.add_argument("--min-trade-mtm-pct", type=float, default=-50.0)
    ap.add_argument(
        "--fill-mode",
        default=STRICT_FILL_MODE,
        choices=("touch", "touch_skip_boundary", "close_beyond", "close_beyond_skip_boundary"),
    )
    ap.add_argument(
        "--strict-fill-mode",
        default=STRICT_FILL_MODE,
        choices=("touch", "touch_skip_boundary", "close_beyond", "close_beyond_skip_boundary"),
    )
    ap.add_argument("--topn", type=int, default=25)
    args = ap.parse_args()

    globals()["MIN_ORDER_USD"] = float(args.min_order_usd)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    positions = read_positions(Path(args.positions_csv))
    arrays = load_npz_arrays(Path(args.npz))
    positions = apply_entry_source(positions, arrays, args.entry_source)
    summaries: List[Dict[str, Any]] = []
    strict_summaries: List[Dict[str, Any]] = []
    row_cache: Dict[str, List[Dict[str, Any]]] = {}
    strict_row_cache: Dict[str, List[Dict[str, Any]]] = {}

    candidates = apply_candidate_slippage(
        candidate_grid(args.target_scale, args.random_candidates, args.seed, args.max_target_notional),
        args.slippage_bp,
    )
    if args.candidate_filter:
        wanted = {x.strip() for x in args.candidate_filter.split(",") if x.strip()}
        candidates = [c for c in candidates if c.name in wanted]
        missing = wanted.difference(c.name for c in candidates)
        if missing:
            raise SystemExit(f"candidate_filter not found: {sorted(missing)}")

    for candidate in candidates:
        rows = simulate_candidate_rows(
            positions,
            candidate,
            arrays,
            fill_mode=args.fill_mode,
            initial_equity=args.initial_equity,
            position_sizing_mode=args.position_sizing_mode,
            leverage=args.leverage,
        )
        if args.strict_fill_mode == args.fill_mode:
            strict_rows = [dict(r) for r in rows]
        else:
            strict_rows = simulate_candidate_rows(
                positions,
                candidate,
                arrays,
                fill_mode=args.strict_fill_mode,
                initial_equity=args.initial_equity,
                position_sizing_mode=args.position_sizing_mode,
                leverage=args.leverage,
            )
        annotate_trade_equity_metrics(rows, args.initial_equity)
        annotate_trade_equity_metrics(strict_rows, args.initial_equity)
        s = summarize(rows, args.initial_equity)
        s.update(
            {
                "candidate": candidate.name,
                "target_notional": candidate.target_notional,
                "base_frac": candidate.base_frac,
                "steps_pct": json.dumps(candidate.steps_pct),
                "add_weights": json.dumps(candidate.add_weights),
                "fill_mode": args.fill_mode,
                "position_sizing_mode": args.position_sizing_mode,
                "entry_source": args.entry_source,
                "slippage_bp_per_side": (candidate.slippage * 10000.0),
                "min_order_gate_usd": MIN_ORDER_USD,
                "mtm_gate_ok": float(s["max_mtm_dd_pct"]) >= -abs(args.max_mtm_dd_pct),
                "strict_trade_mtm_gate_ok": float(s["min_trade_mtm_pct_equity"]) >= float(args.min_trade_mtm_pct),
                "min_order_gate_ok": bool(s["min_order_ok"]),
                "research_label": research_label(
                    candidate,
                    position_sizing_mode=args.position_sizing_mode,
                    max_target_notional=args.max_target_notional,
                ),
                "target_double_hit": float(s["net_pct"]) >= 285.0,
                **grounding_stats(rows, fill_mode=args.fill_mode, min_trade_mtm_pct=args.min_trade_mtm_pct),
                **leverage_stats(rows, leverage=args.leverage, min_trade_mtm_pct=args.min_trade_mtm_pct),
            }
        )
        strict_s = summarize(strict_rows, args.initial_equity)
        strict_s.update(
            {
                "candidate": candidate.name,
                "target_notional": candidate.target_notional,
                "base_frac": candidate.base_frac,
                "steps_pct": json.dumps(candidate.steps_pct),
                "add_weights": json.dumps(candidate.add_weights),
                "fill_mode": args.strict_fill_mode,
                "position_sizing_mode": args.position_sizing_mode,
                "entry_source": args.entry_source,
                "slippage_bp_per_side": (candidate.slippage * 10000.0),
                "min_order_gate_usd": MIN_ORDER_USD,
                "mtm_gate_ok": float(strict_s["max_mtm_dd_pct"]) >= -abs(args.max_mtm_dd_pct),
                "strict_trade_mtm_gate_ok": float(strict_s["min_trade_mtm_pct_equity"]) >= float(args.min_trade_mtm_pct),
                "min_order_gate_ok": bool(strict_s["min_order_ok"]),
                "research_label": research_label(
                    candidate,
                    position_sizing_mode=args.position_sizing_mode,
                    max_target_notional=args.max_target_notional,
                ),
                "target_double_hit": float(strict_s["net_pct"]) >= 285.0,
                **grounding_stats(strict_rows, fill_mode=args.strict_fill_mode, min_trade_mtm_pct=args.min_trade_mtm_pct),
                **leverage_stats(strict_rows, leverage=args.leverage, min_trade_mtm_pct=args.min_trade_mtm_pct),
            }
        )
        summaries.append(s)
        strict_summaries.append(strict_s)
        row_cache[candidate.name] = rows
        strict_row_cache[candidate.name] = strict_rows

    eligible = [s for s in summaries if s["mtm_gate_ok"] and s["min_order_gate_ok"]]
    strict_eligible = [
        s
        for s in strict_summaries
        if s["mtm_gate_ok"]
        and s["strict_trade_mtm_gate_ok"]
        and s["min_order_gate_ok"]
        and (s["grounded_compound_gate_ok"] if args.leverage <= 1.0 else s["leveraged_margin_gate_ok"])
    ]
    ranked = sorted(eligible, key=lambda s: (float(s["net_pct"]), float(s["max_mtm_dd_pct"])), reverse=True)
    ranked_strict = sorted(strict_eligible, key=lambda s: (float(s["net_pct"]), float(s["max_mtm_dd_pct"])), reverse=True)
    all_ranked = sorted(summaries, key=lambda s: (float(s["net_pct"]), float(s["max_mtm_dd_pct"])), reverse=True)
    write_csv(out_dir / "candidate_summary_all.csv", all_ranked)
    write_csv(out_dir / "candidate_summary_eligible.csv", ranked)
    write_csv(out_dir / "candidate_summary_strict_trade_mtm.csv", ranked_strict)
    for row in ranked[: args.topn]:
        name = str(row["candidate"])
        write_csv(out_dir / "top_variants" / name / "trades.csv", row_cache[name])
        write_csv(out_dir / "top_variants" / name / "monthly_splits.csv", monthly_splits(row_cache[name], args.initial_equity))
    for row in ranked_strict[: args.topn]:
        name = str(row["candidate"])
        write_csv(out_dir / "top_strict_trade_mtm_variants" / name / "trades.csv", strict_row_cache[name])
        write_csv(
            out_dir / "top_strict_trade_mtm_variants" / name / "monthly_splits.csv",
            monthly_splits(strict_row_cache[name], args.initial_equity),
        )

    stress_rows: List[Dict[str, Any]] = []
    candidates_by_name = {
        c.name: c
        for c in apply_candidate_slippage(
            candidate_grid(args.target_scale, args.random_candidates, args.seed, args.max_target_notional),
            args.slippage_bp,
        )
    }
    for row in ranked_strict[: min(args.topn, 10)]:
        base_candidate = candidates_by_name[str(row["candidate"])]
        for mult in (1.0, 2.0, 3.0):
            stressed = replace(base_candidate, slippage=base_candidate.slippage * mult)
            stressed_rows = simulate_candidate_rows(
                positions,
                stressed,
                arrays,
                fill_mode=args.strict_fill_mode,
                initial_equity=args.initial_equity,
                position_sizing_mode=args.position_sizing_mode,
                leverage=args.leverage,
            )
            annotate_trade_equity_metrics(stressed_rows, args.initial_equity)
            ss = summarize(stressed_rows, args.initial_equity)
            ss.update(
                {
                    "candidate": stressed.name,
                    "slippage_mult": mult,
                    "slippage_per_side": stressed.slippage,
                    "target_notional": stressed.target_notional,
                    "fill_mode": args.strict_fill_mode,
                    "position_sizing_mode": args.position_sizing_mode,
                    "entry_source": args.entry_source,
                    "mtm_gate_ok": float(ss["max_mtm_dd_pct"]) >= -abs(args.max_mtm_dd_pct),
                    "strict_trade_mtm_gate_ok": float(ss["min_trade_mtm_pct_equity"]) >= float(args.min_trade_mtm_pct),
                    **grounding_stats(stressed_rows, fill_mode=args.strict_fill_mode, min_trade_mtm_pct=args.min_trade_mtm_pct),
                    **leverage_stats(stressed_rows, leverage=args.leverage, min_trade_mtm_pct=args.min_trade_mtm_pct),
                }
            )
            stress_rows.append(ss)
    write_csv(out_dir / "stress_slippage.csv", stress_rows)

    md = [
        "# HYPE DCA Parameter Search Wave 001",
        "",
        f"Candidates tested: {len(summaries)}. MTM DD gate: >= -{abs(args.max_mtm_dd_pct):.1f}%.",
        f"Strict trade MTM gate: >= {args.min_trade_mtm_pct:.1f}% of initial equity.",
        f"Initial equity: {args.initial_equity:.2f}. Target scale: {args.target_scale:.4g}.",
        f"Max target notional: {args.max_target_notional}.",
        f"Position sizing mode: `{args.position_sizing_mode}`.",
        f"Entry source: `{args.entry_source}`.",
        f"Slippage per side: `{(args.slippage_bp if args.slippage_bp is not None else Candidate('tmp',0,0,(),()).slippage*10000.0):.6g} bp`.",
        f"Leverage diagnostic: `{args.leverage:g}x`; margin_used = notional / leverage, margin_call when intratrade MTM loss >= margin_used.",
        "Target notional is planned max position notional, not account equity.",
        f"Baseline fill mode: `{args.fill_mode}`. Strict output fill mode: `{args.strict_fill_mode}`.",
        f"Minimum order gate: every base/add leg must be >= ${MIN_ORDER_USD:.2f}.",
        f"Random candidates: {args.random_candidates}. Seed: {args.seed}.",
        f"Canonical research champion: `{CANONICAL_RESEARCH_CHAMPION}` as `grounded_compound_champion`.",
        "Reporting labels: `grounded_compound_champion`, `static_500_cap`, and rejected `high_notional_illusion`.",
        "",
        "| rank | label | candidate | target notional | net % | max MTM DD % | max realized DD % | min trade MTM % eq | margin calls | PF | avg/max notional | notional>equity | avg fills | double? |",
        "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, row in enumerate(ranked[: args.topn], 1):
        md.append(
            f"| {i} | {row['research_label']} | {row['candidate']} | {row['target_notional']:.1f} | {row['net_pct']:.2f} | {row['max_mtm_dd_pct']:.2f} | "
            f"{row['max_realized_dd_pct']:.2f} | {row['min_trade_mtm_pct_equity']:.2f} | {row['margin_call_count']} | {row['pf']:.2f} | "
            f"{row['avg_notional']:.1f}/{row['max_notional']:.1f} | {row['notional_gt_equity_before_count']} | "
            f"{row['avg_dca_fills']:.2f} | {row['target_double_hit']} |"
        )
    md.extend(
        [
            "",
            "## Strict Trade MTM Top",
            "",
            "| rank | label | candidate | target notional | net % | max MTM DD % | min trade MTM % eq | margin calls | PF | avg/max notional | notional>equity | avg fills |",
            "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
        ]
    )
    for i, row in enumerate(ranked_strict[: args.topn], 1):
        md.append(
            f"| {i} | {row['research_label']} | {row['candidate']} | {row['target_notional']:.1f} | {row['net_pct']:.2f} | {row['max_mtm_dd_pct']:.2f} | "
            f"{row['min_trade_mtm_pct_equity']:.2f} | {row['margin_call_count']} | {row['pf']:.2f} | "
            f"{row['avg_notional']:.1f}/{row['max_notional']:.1f} | {row['notional_gt_equity_before_count']} | "
            f"{row['avg_dca_fills']:.2f} |"
        )
    md.extend(
        [
            "",
            "Notes:",
            "- MTM uses worst intratrade candle adverse mark after DCA fills, with entry and exit fee/slippage costs, as percent of initial equity.",
            "- `candidate_summary_strict_trade_mtm.csv` is generated from the strict fill mode and must pass both portfolio MTM DD and min trade MTM gates.",
            "- `stress_slippage.csv` reruns strict top candidates at 1x/2x/3x slippage.",
            "- Grounded compound candidates cannot pass strict ranking if final trade notional exceeds equity before the trade, beyond floating tolerance.",
            "- High-notional fixed-cap results are labeled as `high_notional_illusion` and are not promotion-grade.",
            "- This is closed-position replay; open-position companion risk is still required before promotion.",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    (out_dir / "journal.json").write_text(
        json.dumps(
            {
                "ranked_top": ranked[: args.topn],
                "ranked_strict_trade_mtm_top": ranked_strict[: args.topn],
                "all_count": len(summaries),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps({"out_dir": str(out_dir), "top": ranked[:5], "strict_top": ranked_strict[:5]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
