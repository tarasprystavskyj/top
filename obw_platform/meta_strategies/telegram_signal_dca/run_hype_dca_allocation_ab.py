#!/usr/bin/env python3
"""A/B test HYPE DCA allocation hypotheses.

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
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Sequence

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compare_binance_copy_positions_dca import read_positions  # noqa: E402
from run_hype_dca_parameter_search import (  # noqa: E402
    DEFAULT_NPZ,
    DEFAULT_POSITIONS,
    DEFAULT_REPORT_DIR,
    GROUNDING_TOL,
    MIN_ORDER_USD,
    STRICT_FILL_MODE,
    dca_levels,
    iso,
    level_crossed,
    load_npz_arrays,
    apply_entry_source,
    ms,
    summarize,
)
from telegram_signal_dca_compare import ret_for  # noqa: E402


PHI_INV = 0.6180339887498948


@dataclass(frozen=True)
class AllocationCase:
    name: str
    allocation_mode: str
    initial_frac: float
    add_notional_frac: float
    steps_pct: tuple[float, ...]
    add_weights: tuple[float, ...]
    fee: float = 0.0005
    slippage: float = 0.0009380229915652661


def clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def window_slice(arrays: Dict[str, np.ndarray], start_ms: int, end_ms: int) -> slice:
    t = arrays["t"]
    left = int(np.searchsorted(t, start_ms, side="left"))
    right = int(np.searchsorted(t, end_ms, side="left"))
    return slice(left, max(left, right))


def range_initial_frac(
    arrays: Dict[str, np.ndarray],
    *,
    end: datetime,
    lookback_days: float,
    price: float,
    fallback: float,
) -> tuple[float, Dict[str, Any]]:
    start = end - timedelta(days=lookback_days)
    sl = window_slice(arrays, ms(start), ms(end))
    lows = arrays["low"][sl]
    highs = arrays["high"][sl]
    if len(lows) == 0 or len(highs) == 0:
        return fallback, {"warmup_reason": "no_warmup_rows"}
    lo = float(np.nanmin(lows))
    hi = float(np.nanmax(highs))
    span = hi - lo
    if not math.isfinite(span) or span <= 0:
        return fallback, {"warmup_reason": "flat_warmup_range", "warmup_low": lo, "warmup_high": hi}
    # Long-side reserve logic: buy more coin near the lower end, keep more USDT near the upper end.
    frac = clamp((hi - price) / span, 0.05, 0.95)
    return frac, {
        "warmup_reason": "ok",
        "warmup_low": lo,
        "warmup_high": hi,
        "warmup_span_pct": 100.0 * span / max(price, 1e-12),
        "warmup_to_high_pct_of_range": 100.0 * (hi - price) / span,
        "warmup_to_low_pct_of_range": 100.0 * (price - lo) / span,
    }


def golden_initial_frac(
    arrays: Dict[str, np.ndarray],
    *,
    end: datetime,
    price: float,
    fallback: float,
) -> tuple[float, Dict[str, Any]]:
    sl = window_slice(arrays, int(arrays["t"][0]), ms(end))
    lows = arrays["low"][sl]
    highs = arrays["high"][sl]
    if len(lows) == 0 or len(highs) == 0:
        return fallback, {"warmup_reason": "no_history_rows"}
    lo = float(np.nanmin(lows))
    hi = float(np.nanmax(highs))
    span = hi - lo
    if not math.isfinite(span) or span <= 0:
        return fallback, {"warmup_reason": "flat_history_range", "history_low": lo, "history_high": hi}
    fib_low = lo + (1.0 - PHI_INV) * span
    fib_high = lo + PHI_INV * span
    fib_span = fib_high - fib_low
    frac = clamp((fib_high - price) / max(fib_span, 1e-12), 0.05, 0.95)
    return frac, {
        "warmup_reason": "ok",
        "history_low": lo,
        "history_high": hi,
        "golden_low": fib_low,
        "golden_high": fib_high,
        "golden_price_pos_pct": 100.0 * (price - fib_low) / max(fib_span, 1e-12),
    }


def leg_plan(
    case: AllocationCase,
    arrays: Dict[str, np.ndarray],
    pos: Any,
    equity: float,
    lookback_days: float,
) -> tuple[float, List[float], Dict[str, Any]]:
    meta: Dict[str, Any] = {}
    if case.allocation_mode == "fixed":
        frac = case.initial_frac
        meta["warmup_reason"] = "fixed"
    elif case.allocation_mode == "range_lookback":
        frac, meta = range_initial_frac(
            arrays,
            end=pos.opened,
            lookback_days=lookback_days,
            price=float(pos.entry),
            fallback=case.initial_frac,
        )
    elif case.allocation_mode == "golden_history":
        frac, meta = golden_initial_frac(arrays, end=pos.opened, price=float(pos.entry), fallback=case.initial_frac)
    else:
        raise ValueError(f"unknown allocation_mode={case.allocation_mode}")

    initial = equity * clamp(frac, 0.0, 1.0)
    baseline_add = equity * case.add_notional_frac
    adds = [baseline_add * float(w) for w in case.add_weights]
    planned = initial + sum(adds)
    if planned > equity:
        scale = max((equity - initial) / max(sum(adds), 1e-12), 0.0)
        adds = [x * scale for x in adds]
    meta.update(
        {
            "initial_frac": frac,
            "initial_notional": initial,
            "baseline_add_notional": baseline_add,
            "planned_notional": initial + sum(adds),
        }
    )
    return initial, adds, meta


def simulate_position(
    pos: Any,
    case: AllocationCase,
    arrays: Dict[str, np.ndarray],
    *,
    equity: float,
    lookback_days: float,
    fill_mode: str,
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

    base_notional, adds, alloc_meta = leg_plan(case, arrays, pos, equity, lookback_days)
    levels = dca_levels(pos.side, pos.entry, case.steps_pct[: len(adds)])
    avg_entry = float(pos.entry)
    notional = float(base_notional)
    fills = 0
    fill_rows: List[Dict[str, Any]] = []
    min_mtm = 0.0
    min_mtm_pct_on_notional = 0.0
    skip_boundary = fill_mode in {"touch_skip_boundary", "close_beyond_skip_boundary"}
    min_leg = min([base_notional, *adds]) if adds else base_notional
    min_ok = min_leg >= MIN_ORDER_USD

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
        mtm_ret = ret_for(pos.side, avg_entry, mark) - 2 * case.fee - 2 * case.slippage
        mtm = mtm_ret * notional
        min_mtm = min(min_mtm, mtm)
        min_mtm_pct_on_notional = min(min_mtm_pct_on_notional, 100.0 * mtm / max(notional, 1e-12))

    gross_ret = ret_for(pos.side, avg_entry, pos.exit)
    net_ret = gross_ret - 2 * case.fee - 2 * case.slippage
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
        **alloc_meta,
    }


def simulate_case_rows(
    positions: Sequence[Any],
    case: AllocationCase,
    arrays: Dict[str, np.ndarray],
    *,
    initial_equity: float,
    lookback_days: float,
    fill_mode: str,
) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    equity = float(initial_equity)
    for pos in positions:
        row = simulate_position(pos, case, arrays, equity=equity, lookback_days=lookback_days, fill_mode=fill_mode)
        if row is None:
            continue
        row["equity_before"] = equity
        row["notional_gt_equity_before"] = float(row["notional"]) - equity > GROUNDING_TOL
        equity += float(row["pnl"])
        row["equity_after"] = equity
        rows.append(row)
    for row in rows:
        row["min_mtm_pct_equity"] = 100.0 * float(row.get("min_mtm", 0.0)) / max(initial_equity, 1e-12)
    return rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def grounding_summary(rows: Sequence[Dict[str, Any]], *, min_trade_mtm_pct: float) -> Dict[str, Any]:
    over = [
        float(r.get("notional", 0.0)) - float(r.get("equity_before", 0.0))
        for r in rows
        if float(r.get("notional", 0.0)) - float(r.get("equity_before", 0.0)) > GROUNDING_TOL
    ]
    min_trade = min((float(r.get("min_mtm_pct_equity", 0.0)) for r in rows), default=0.0)
    return {
        "notional_gt_equity_before_count": len(over),
        "notional_gt_equity_before_max": max(over, default=0.0),
        "strict_fill_ok": all(str(r.get("fill_mode", "")) == STRICT_FILL_MODE for r in rows),
        "min_trade_mtm_gate_ok": min_trade >= min_trade_mtm_pct,
        "grounded_gate_ok": len(over) == 0 and min_trade >= min_trade_mtm_pct,
    }


def base_cases() -> List[AllocationCase]:
    champion_steps = (0.25, 0.35, 0.55)
    champion_weights = (0.8, 1.2, 2.2)
    return [
        AllocationCase(
            "control_current_champion_initial16_add100_weighted",
            "fixed",
            0.16,
            0.20,
            champion_steps,
            champion_weights,
        ),
        AllocationCase(
            "a_fixed_50pct_initial_add50_weighted",
            "fixed",
            0.50,
            0.10,
            champion_steps,
            champion_weights,
        ),
        AllocationCase(
            "b_warmup_7d_range_dynamic_initial_add50_weighted",
            "range_lookback",
            0.50,
            0.10,
            champion_steps,
            champion_weights,
        ),
        AllocationCase(
            "c_golden_history_dynamic_initial_add50_weighted",
            "golden_history",
            0.50,
            0.10,
            champion_steps,
            champion_weights,
        ),
    ]


def optimization_cases(*, include_modes: set[str], random_candidates: int, seed: int) -> List[AllocationCase]:
    out = [c for c in base_cases() if c.allocation_mode in include_modes or c.name.startswith("control_")]
    step_sets = [
        (0.20, 0.30, 0.45),
        (0.25, 0.35, 0.55),
        (0.30, 0.45, 0.70),
        (0.35, 0.55, 0.85),
        (0.25, 0.35, 0.55, 0.90),
        (0.30, 0.45, 0.70, 1.10),
        (0.35, 0.55, 0.85, 1.30),
    ]
    weight_sets = [
        (0.8, 1.2, 2.2),
        (1.0, 1.0, 1.2),
        (1.0, 1.3, 1.8),
        (0.7, 1.0, 1.5, 2.4),
        (0.6, 0.9, 1.3, 1.8),
    ]
    initial_fracs = (0.20, 0.35, 0.50, 0.65, 0.80)
    add_fracs = (0.04, 0.06, 0.08, 0.10, 0.14, 0.20)
    for mode in sorted(include_modes):
        if mode == "fixed":
            continue
        for initial_frac in initial_fracs:
            for add_frac in add_fracs:
                for steps in step_sets:
                    valid_weights = [w for w in weight_sets if len(w) == len(steps)]
                    for weights in valid_weights:
                        out.append(
                            AllocationCase(
                                "%s_i%02d_a%03d_s%s_w%s"
                                % (
                                    mode,
                                    int(initial_frac * 100),
                                    int(add_frac * 1000),
                                    "-".join(str(x).replace(".", "p") for x in steps),
                                    "-".join(str(x).replace(".", "p") for x in weights),
                                ),
                                mode,
                                initial_frac,
                                add_frac,
                                steps,
                                weights,
                            )
                        )
    rng = random.Random(seed)
    for i in range(max(0, random_candidates)):
        mode_pool = tuple(m for m in include_modes if m != "fixed")
        if not mode_pool:
            break
        mode = rng.choice(mode_pool)
        dca_count = rng.choice((3, 4, 5))
        initial_frac = rng.uniform(0.12, 0.85)
        add_frac = rng.uniform(0.025, 0.22)
        steps = []
        last = rng.uniform(0.15, 0.45)
        for _ in range(dca_count):
            last += rng.uniform(0.08, 0.45)
            steps.append(round(last, 3))
        weights = []
        w = rng.uniform(0.5, 1.4)
        for _ in range(dca_count):
            w *= rng.uniform(0.9, 1.7)
            weights.append(round(w, 3))
        out.append(
            AllocationCase(
                "rnd%04d_%s_i%02d_a%03d_s%s_w%s"
                % (
                    i,
                    mode,
                    int(initial_frac * 100),
                    int(add_frac * 1000),
                    "-".join(str(x).replace(".", "p") for x in steps),
                    "-".join(str(x).replace(".", "p") for x in weights),
                ),
                mode,
                initial_frac,
                add_frac,
                tuple(steps),
                tuple(weights),
            )
        )
    unique: Dict[str, AllocationCase] = {}
    for case in out:
        unique[case.name] = case
    return list(unique.values())


def main() -> None:
    ap = argparse.ArgumentParser(description="A/B test separated initial/add DCA allocations.")
    ap.add_argument("--positions-csv", default=str(DEFAULT_POSITIONS))
    ap.add_argument("--npz", default=str(DEFAULT_NPZ))
    ap.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR / "dca_allocation_ab_wave_001"))
    ap.add_argument("--initial-equity", type=float, default=500.0)
    ap.add_argument("--slippage-bp", type=float, default=None, help="Override per-side slippage in basis points.")
    ap.add_argument(
        "--entry-source",
        default="avgCost",
        choices=("avgCost", "first_bar_open", "first_bar_close", "first_bar_high", "first_bar_low", "next_bar_open"),
        help="Entry anchor. avgCost is historical lead average cost; bar anchors are public OHLC approximations.",
    )
    ap.add_argument("--min-order-usd", type=float, default=MIN_ORDER_USD, help="Minimum allowed base/add leg notional for eligibility gates.")
    ap.add_argument("--lookback-days", type=float, default=7.0)
    ap.add_argument("--min-trade-mtm-pct", type=float, default=-50.0)
    ap.add_argument("--fill-mode", default=STRICT_FILL_MODE, choices=("touch", "touch_skip_boundary", "close_beyond", "close_beyond_skip_boundary"))
    ap.add_argument("--include-modes", default="fixed,range_lookback,golden_history")
    ap.add_argument("--case-filter", default="", help="Comma-separated allocation case names to run after grid construction.")
    ap.add_argument("--random-candidates", type=int, default=0)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--topn", type=int, default=30)
    args = ap.parse_args()

    globals()["MIN_ORDER_USD"] = float(args.min_order_usd)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    positions = read_positions(Path(args.positions_csv))
    arrays = load_npz_arrays(Path(args.npz))
    positions = apply_entry_source(positions, arrays, args.entry_source)
    summaries: List[Dict[str, Any]] = []

    include_modes = {x.strip() for x in args.include_modes.split(",") if x.strip()}
    cases = optimization_cases(include_modes=include_modes, random_candidates=args.random_candidates, seed=args.seed)
    if args.case_filter:
        wanted = {x.strip() for x in args.case_filter.split(",") if x.strip()}
        cases = [c for c in cases if c.name in wanted]
        missing = wanted.difference(c.name for c in cases)
        if missing:
            raise SystemExit(f"case_filter not found: {sorted(missing)}")
    for case in cases:
        if args.slippage_bp is not None:
            case = replace(case, slippage=float(args.slippage_bp) / 10000.0)
        rows = simulate_case_rows(
            positions,
            case,
            arrays,
            initial_equity=args.initial_equity,
            lookback_days=args.lookback_days,
            fill_mode=args.fill_mode,
        )
        s = summarize(rows, args.initial_equity)
        s.update(
            {
                "case": case.name,
                "allocation_mode": case.allocation_mode,
                "initial_frac_default": case.initial_frac,
                "add_notional_frac": case.add_notional_frac,
                "steps_pct": json.dumps(case.steps_pct),
                "add_weights": json.dumps(case.add_weights),
                "fill_mode": args.fill_mode,
                "entry_source": args.entry_source,
                "slippage_bp_per_side": case.slippage * 10000.0,
                "min_order_gate_usd": MIN_ORDER_USD,
                "min_order_gate_ok": bool(s["min_order_ok"]),
                **grounding_summary(rows, min_trade_mtm_pct=args.min_trade_mtm_pct),
            }
        )
        summaries.append(s)
        write_csv(out_dir / case.name / "trades.csv", rows)

    ranked = sorted(
        [
            row
            for row in summaries
            if row["strict_fill_ok"] and row["grounded_gate_ok"] and row["min_order_gate_ok"]
        ],
        key=lambda x: (float(x["net_pct"]), float(x["max_mtm_dd_pct"])),
        reverse=True,
    )
    all_ranked = sorted(summaries, key=lambda x: (float(x["net_pct"]), float(x["max_mtm_dd_pct"])), reverse=True)
    write_csv(out_dir / "allocation_ab_summary_all.csv", all_ranked)
    write_csv(out_dir / "allocation_ab_summary.csv", ranked)
    md = [
        "# HYPE DCA Allocation A/B Wave 001",
        "",
        f"Initial equity: ${args.initial_equity:.2f}. Fill mode: `{args.fill_mode}`. Entry source: `{args.entry_source}`.",
        f"Slippage per side: `{(args.slippage_bp if args.slippage_bp is not None else AllocationCase('tmp','fixed',0,0,(),()).slippage*10000.0):.6g} bp`. Min order gate: `${MIN_ORDER_USD:.2f}`.",
        f"Lookback range hypothesis window: {args.lookback_days:g} days.",
        f"Modes: `{','.join(sorted(include_modes))}`. Cases tested: {len(summaries)}. Random candidates: {args.random_candidates}. Seed: {args.seed}. Case filter: `{args.case_filter}`.",
        "All cases separate first buy from add baseline: first buy is an equity fraction; add baseline is 10% of current equity, so $50 at $500.",
        "Warmup range fraction uses long reserve logic: initial coin fraction = `(range_high - entry) / (range_high - range_low)`.",
        "Golden history fraction uses the historical 38.2%-61.8% corridor before each signal and maps current price through that corridor.",
        "",
        "| rank | case | net % | final equity | max MTM DD % | min trade MTM % eq | PF | avg/max notional | min order | over equity | avg fills |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for i, row in enumerate(ranked[: args.topn], 1):
        md.append(
            f"| {i} | {row['case']} | {row['net_pct']:.2f} | {row['equity_end']:.2f} | {row['max_mtm_dd_pct']:.2f} | "
            f"{row['min_trade_mtm_pct_equity']:.2f} | {row['pf']:.2f} | {row['avg_notional']:.1f}/{row['max_notional']:.1f} | "
            f"{row['min_order_usd']:.2f} | {row['notional_gt_equity_before_count']} | {row['avg_dca_fills']:.2f} |"
        )
    md.extend(
        [
            "",
            "Notes:",
            "- This is still Binance lead closed-position replay, not TP-aware autonomous exit replay.",
            f"- Promotion gates remain: strict fill, no notional over equity before trade, min trade MTM >= -50% of initial equity, min leg >= ${MIN_ORDER_USD:.2f}.",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), "ranked": ranked}, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
