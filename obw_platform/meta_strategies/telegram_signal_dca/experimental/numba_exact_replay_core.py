#!/usr/bin/env python3
"""Experimental Numba-compatible exact replay core for HYPE DCA search.

Prototype constraints:
- No YAML parsing in hot loop.
- No dict/dataclass rows in hot loop.
- Exact Python engine remains the reference; this module must pass equivalence
  before being used for search or ranking.
- This is not a live/paper-live runner and never places orders.

The implementation intentionally returns a compact summary vector, not trade rows.
The equivalence checker compares this vector against the existing Python engine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Mapping, Sequence

import numpy as np

try:  # pragma: no cover - local environments may not have numba installed yet.
    from numba import njit
except Exception:  # pragma: no cover
    def njit(*args, **kwargs):  # type: ignore
        if args and callable(args[0]):
            return args[0]
        def deco(fn):
            return fn
        return deco

FILL_TOUCH = 0
FILL_TOUCH_SKIP_BOUNDARY = 1
FILL_CLOSE_BEYOND = 2
FILL_CLOSE_BEYOND_SKIP_BOUNDARY = 3
SIZING_FIXED = 0
SIZING_COMPOUND = 1
SIDE_LONG = 1
SIDE_SHORT = -1
GROUNDING_TOL = 1e-8

SUMMARY_FIELDS = (
    "trades",
    "equity_start",
    "equity_end",
    "net_pct",
    "max_realized_dd_pct",
    "max_mtm_dd_pct",
    "min_trade_mtm_pct_equity",
    "min_trade_mtm_pct_notional",
    "win_rate_pct",
    "pf",
    "gross_profit",
    "gross_loss",
    "avg_dca_fills",
    "avg_notional",
    "max_notional",
    "min_order_usd",
    "min_order_ok",
    "notional_gt_equity_before_count",
    "margin_call_count",
)


@dataclass(frozen=True)
class ReplayInputs:
    t_ms: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    opened_ms: np.ndarray
    closed_ms: np.ndarray
    entry: np.ndarray
    exit: np.ndarray
    side: np.ndarray


@dataclass(frozen=True)
class CandidateArrays:
    target_notional: float
    base_frac: float
    steps_pct: np.ndarray
    add_weights: np.ndarray
    fee: float
    slippage: float


def fill_mode_code(fill_mode: str) -> int:
    table = {
        "touch": FILL_TOUCH,
        "touch_skip_boundary": FILL_TOUCH_SKIP_BOUNDARY,
        "close_beyond": FILL_CLOSE_BEYOND,
        "close_beyond_skip_boundary": FILL_CLOSE_BEYOND_SKIP_BOUNDARY,
    }
    return table[fill_mode]


def sizing_mode_code(position_sizing_mode: str) -> int:
    table = {"fixed": SIZING_FIXED, "compound": SIZING_COMPOUND}
    return table[position_sizing_mode]


def side_code(side: str) -> int:
    return SIDE_LONG if str(side).upper() == "LONG" else SIDE_SHORT


def summary_array_to_dict(values: np.ndarray) -> Dict[str, float | bool]:
    out: Dict[str, float | bool] = {}
    for i, name in enumerate(SUMMARY_FIELDS):
        v = float(values[i])
        if name in {"trades", "notional_gt_equity_before_count", "margin_call_count"}:
            out[name] = int(round(v))
        elif name == "min_order_ok":
            out[name] = bool(v >= 0.5)
        else:
            out[name] = v
    return out


@njit(cache=True)
def _ret_for(side: int, entry: float, price: float) -> float:
    if side == SIDE_LONG:
        return price / max(entry, 1e-12) - 1.0
    return entry / max(price, 1e-12) - 1.0


@njit(cache=True)
def _drawdown_from_equity_curve(values: np.ndarray, n: int) -> float:
    if n <= 0:
        return 0.0
    peak = values[0]
    min_dd = 0.0
    for i in range(n):
        v = values[i]
        if v > peak:
            peak = v
        if peak > 1e-12:
            dd = v / peak - 1.0
            if dd < min_dd:
                min_dd = dd
    return min_dd


@njit(cache=True)
def _level_crossed(side: int, low: float, high: float, close: float, level: float, fill_mode: int) -> bool:
    if fill_mode == FILL_CLOSE_BEYOND or fill_mode == FILL_CLOSE_BEYOND_SKIP_BOUNDARY:
        if side == SIDE_LONG:
            return close <= level
        return close >= level
    if side == SIDE_LONG:
        return low <= level
    return high >= level


@njit(cache=True)
def _search_left(t: np.ndarray, value: int) -> int:
    lo = 0
    hi = len(t)
    while lo < hi:
        mid = (lo + hi) // 2
        if t[mid] < value:
            lo = mid + 1
        else:
            hi = mid
    return lo


@njit(cache=True)
def _search_right(t: np.ndarray, value: int) -> int:
    lo = 0
    hi = len(t)
    while lo < hi:
        mid = (lo + hi) // 2
        if t[mid] <= value:
            lo = mid + 1
        else:
            hi = mid
    return lo


@njit(cache=True)
def _min_order_for_candidate(target_notional: float, base_frac: float, weights: np.ndarray) -> float:
    base = target_notional * base_frac
    remaining = max(target_notional - base, 0.0)
    total_w = 0.0
    for i in range(len(weights)):
        total_w += weights[i]
    min_leg = base
    if total_w > 0.0:
        for i in range(len(weights)):
            add = remaining * weights[i] / total_w
            if add < min_leg:
                min_leg = add
    return min_leg


@njit(cache=True)
def _simulate_one_position(
    t_ms: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    opened_ms: int,
    closed_ms: int,
    entry: float,
    exit_price: float,
    side: int,
    target_notional: float,
    base_frac: float,
    steps_pct: np.ndarray,
    add_weights: np.ndarray,
    fee: float,
    slippage: float,
    fill_mode: int,
) -> np.ndarray:
    # Output fields:
    # 0 has_row, 1 pnl, 2 min_mtm, 3 min_mtm_pct_on_notional, 4 notional,
    # 5 fills, 6 min_order_usd
    out = np.zeros(7, dtype=np.float64)
    start = _search_left(t_ms, opened_ms)
    end = _search_right(t_ms, closed_ms)
    if end <= start:
        return out

    base_notional = target_notional * base_frac
    remaining = max(target_notional - base_notional, 0.0)
    total_w = 0.0
    for i in range(len(add_weights)):
        total_w += add_weights[i]

    n_adds = len(add_weights)
    adds = np.zeros(n_adds, dtype=np.float64)
    for i in range(n_adds):
        adds[i] = remaining * add_weights[i] / total_w if total_w > 0.0 else 0.0

    n_levels = min(len(steps_pct), n_adds)
    levels = np.zeros(n_levels, dtype=np.float64)
    last = entry
    for i in range(n_levels):
        if side == SIDE_LONG:
            last *= 1.0 - steps_pct[i] / 100.0
        else:
            last *= 1.0 + steps_pct[i] / 100.0
        levels[i] = last

    avg_entry = entry
    notional = base_notional
    fills = 0
    min_mtm = 0.0
    min_mtm_pct_on_notional = 0.0
    skip_boundary = fill_mode == FILL_TOUCH_SKIP_BOUNDARY or fill_mode == FILL_CLOSE_BEYOND_SKIP_BOUNDARY

    for absolute_i in range(start, end):
        rel_i = absolute_i - start
        can_fill = not (skip_boundary and (rel_i == 0 or absolute_i == end - 1))
        fills_this_candle = 0
        while can_fill and fills < n_levels:
            touched = _level_crossed(side, low[absolute_i], high[absolute_i], close[absolute_i], levels[fills], fill_mode)
            if not touched:
                break
            if fills_this_candle >= 1 and skip_boundary:
                break
            add_notional = adds[fills]
            old_qty = notional / max(avg_entry, 1e-12)
            add_qty = add_notional / max(levels[fills], 1e-12)
            notional += add_notional
            avg_entry = notional / max(old_qty + add_qty, 1e-12)
            fills += 1
            fills_this_candle += 1
        if skip_boundary and (rel_i == 0 or absolute_i == end - 1):
            continue
        mark = low[absolute_i] if side == SIDE_LONG else high[absolute_i]
        mtm_ret = _ret_for(side, avg_entry, mark) - 2.0 * fee - 2.0 * slippage
        mtm = mtm_ret * notional
        if mtm < min_mtm:
            min_mtm = mtm
        pct_notional = 100.0 * mtm / max(notional, 1e-12)
        if pct_notional < min_mtm_pct_on_notional:
            min_mtm_pct_on_notional = pct_notional

    gross_ret = _ret_for(side, avg_entry, exit_price)
    net_ret = gross_ret - 2.0 * fee - 2.0 * slippage
    pnl = net_ret * notional
    min_order = _min_order_for_candidate(target_notional, base_frac, add_weights)

    out[0] = 1.0
    out[1] = pnl
    out[2] = min_mtm
    out[3] = min_mtm_pct_on_notional
    out[4] = notional
    out[5] = fills
    out[6] = min_order
    return out


@njit(cache=True)
def simulate_candidate_summary_numba(
    t_ms: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    opened_ms: np.ndarray,
    closed_ms: np.ndarray,
    entry: np.ndarray,
    exit_price: np.ndarray,
    side: np.ndarray,
    initial_equity: float,
    target_notional: float,
    base_frac: float,
    steps_pct: np.ndarray,
    add_weights: np.ndarray,
    fee: float,
    slippage: float,
    fill_mode: int,
    position_sizing_mode: int,
    leverage: float,
    min_order_usd_gate: float,
) -> np.ndarray:
    leverage = max(leverage, 1.0)
    n_pos = len(opened_ms)
    realized_curve = np.empty(n_pos + 1, dtype=np.float64)
    mtm_curve = np.empty(n_pos + 1, dtype=np.float64)
    equity = initial_equity
    realized_curve[0] = equity
    mtm_curve[0] = equity
    curve_n = 1
    target_frac = target_notional / max(initial_equity, 1e-12)

    trades = 0
    wins = 0
    gross_profit = 0.0
    gross_loss = 0.0
    min_mtm = 0.0
    min_mtm_pct_notional = 0.0
    sum_dca_fills = 0.0
    sum_notional = 0.0
    max_notional = 0.0
    min_order_seen = 1e300
    min_order_ok = 1.0
    notional_gt_equity_before_count = 0
    margin_call_count = 0

    for j in range(n_pos):
        effective_target = target_notional
        if position_sizing_mode == SIZING_COMPOUND:
            a = equity * leverage
            b = equity * target_frac
            effective_target = a if a < b else b
            if effective_target < 0.0:
                effective_target = 0.0
        row = _simulate_one_position(
            t_ms, high, low, close,
            int(opened_ms[j]), int(closed_ms[j]), float(entry[j]), float(exit_price[j]), int(side[j]),
            effective_target, base_frac, steps_pct, add_weights, fee, slippage, fill_mode,
        )
        if row[0] < 0.5:
            continue
        pnl = row[1]
        row_min_mtm = row[2]
        row_min_mtm_pct_notional = row[3]
        notional = row[4]
        fills = row[5]
        min_order = row[6]
        equity_before = equity
        margin_used = notional / leverage
        if abs(row_min_mtm) >= margin_used - GROUNDING_TOL:
            margin_call_count += 1
        if notional - equity_before > GROUNDING_TOL:
            notional_gt_equity_before_count += 1
        if min_order < min_order_usd_gate:
            min_order_ok = 0.0
        if min_order < min_order_seen:
            min_order_seen = min_order
        if row_min_mtm < min_mtm:
            min_mtm = row_min_mtm
        if row_min_mtm_pct_notional < min_mtm_pct_notional:
            min_mtm_pct_notional = row_min_mtm_pct_notional
        if pnl > 0.0:
            wins += 1
            gross_profit += pnl
        elif pnl < 0.0:
            gross_loss += pnl
        sum_dca_fills += fills
        sum_notional += notional
        if notional > max_notional:
            max_notional = notional
        mtm_curve[curve_n] = equity + row_min_mtm
        equity += pnl
        realized_curve[curve_n] = equity
        curve_n += 1
        trades += 1

    out = np.zeros(len(SUMMARY_FIELDS), dtype=np.float64)
    out[0] = trades
    out[1] = initial_equity
    out[2] = equity
    out[3] = 100.0 * (equity - initial_equity) / max(initial_equity, 1e-12)
    out[4] = 100.0 * _drawdown_from_equity_curve(realized_curve, curve_n)
    out[5] = 100.0 * _drawdown_from_equity_curve(mtm_curve, curve_n)
    out[6] = 100.0 * min_mtm / max(initial_equity, 1e-12)
    out[7] = min_mtm_pct_notional
    out[8] = 100.0 * wins / max(trades, 1)
    out[9] = gross_profit / abs(gross_loss) if gross_loss < 0.0 else 0.0
    out[10] = gross_profit
    out[11] = gross_loss
    out[12] = sum_dca_fills / max(trades, 1)
    out[13] = sum_notional / max(trades, 1)
    out[14] = max_notional
    out[15] = 0.0 if min_order_seen == 1e300 else min_order_seen
    out[16] = min_order_ok
    out[17] = notional_gt_equity_before_count
    out[18] = margin_call_count
    return out


def build_replay_inputs_from_python(positions: Sequence[object], arrays: Mapping[str, np.ndarray], ms_fn) -> ReplayInputs:
    opened_ms = np.asarray([ms_fn(p.opened) for p in positions], dtype=np.int64)
    closed_ms = np.asarray([ms_fn(p.closed) for p in positions], dtype=np.int64)
    entry = np.asarray([float(p.entry) for p in positions], dtype=np.float64)
    exit_price = np.asarray([float(p.exit) for p in positions], dtype=np.float64)
    side = np.asarray([side_code(str(p.side)) for p in positions], dtype=np.int8)
    return ReplayInputs(
        t_ms=np.asarray(arrays["t"], dtype=np.int64),
        high=np.asarray(arrays["high"], dtype=np.float64),
        low=np.asarray(arrays["low"], dtype=np.float64),
        close=np.asarray(arrays["close"], dtype=np.float64),
        opened_ms=opened_ms,
        closed_ms=closed_ms,
        entry=entry,
        exit=exit_price,
        side=side,
    )


def candidate_to_arrays(candidate: object) -> CandidateArrays:
    return CandidateArrays(
        target_notional=float(candidate.target_notional),
        base_frac=float(candidate.base_frac),
        steps_pct=np.asarray(tuple(candidate.steps_pct), dtype=np.float64),
        add_weights=np.asarray(tuple(candidate.add_weights), dtype=np.float64),
        fee=float(candidate.fee),
        slippage=float(candidate.slippage),
    )


def simulate_candidate_summary(inputs: ReplayInputs, candidate: CandidateArrays, *, initial_equity: float, fill_mode: str, position_sizing_mode: str, leverage: float, min_order_usd_gate: float) -> Dict[str, float | bool]:
    values = simulate_candidate_summary_numba(
        inputs.t_ms, inputs.high, inputs.low, inputs.close,
        inputs.opened_ms, inputs.closed_ms, inputs.entry, inputs.exit, inputs.side,
        float(initial_equity), candidate.target_notional, candidate.base_frac,
        candidate.steps_pct, candidate.add_weights, candidate.fee, candidate.slippage,
        fill_mode_code(fill_mode), sizing_mode_code(position_sizing_mode), float(leverage), float(min_order_usd_gate),
    )
    return summary_array_to_dict(values)
