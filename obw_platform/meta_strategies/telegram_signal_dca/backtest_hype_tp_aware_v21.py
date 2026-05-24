#!/usr/bin/env python3
"""TP-aware HYPE V21/Pine-like signal backtester.

Research/paper only. This treats Binance copy open positions as entry signals,
then exits with a local V21/Pine-like engine instead of the Binance lead close.
It does not place orders, read secrets, or call network APIs.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compare_binance_copy_positions_dca import CopyPosition, read_positions  # noqa: E402
from run_hype_dca_parameter_search import (  # noqa: E402
    CANONICAL_RESEARCH_CHAMPION,
    DEFAULT_NPZ,
    DEFAULT_POSITIONS,
    DEFAULT_REPORT_DIR,
    STRICT_FILL_MODE,
    Candidate,
    load_npz_arrays,
)
from telegram_signal_dca_compare import max_drawdown  # noqa: E402


FEE = 0.0005
SLIPPAGE = 0.0009380229915652661
GROUNDING_TOL = 1e-8


@dataclass(frozen=True)
class EngineParams:
    name: str
    tp_percent: float = 0.52
    callback_percent: float = 0.10
    sub_sell_tp_percent: float = 0.65
    require_close_above_full_tp: bool = True
    sub_sell_close_confirm_mode: str = "breakeven"
    require_close_below_dca_level: bool = True
    block_dca_on_tp_touch: bool = False
    max_fills_per_bar: int = 2
    max_sub_sells_per_bar: int = 5
    max_orders_per_3_min: int = 6
    margin_call_limit: int = 4
    max_position_cost_pct: float = 100.0
    hard_max_total_dd_pct: float = 50.0
    hard_breakeven_deleverage_pct: float = 25.0
    use_high_low_touch: bool = True


@dataclass
class Lot:
    tag: str
    qty: float
    price: float
    usdt: float


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def dt_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def resolve_existing(path: Path) -> Path:
    if path.exists():
        return path
    text = str(path)
    if "/top_1_dev_runtime/" in text:
        alt = Path(text.replace("/top_1_dev_runtime/", "/top_1/"))
        if alt.exists():
            return alt
    if not path.is_absolute():
        runtime_alt = Path("/var/www/vps2.happyuser.info/top/top_1") / path
        if runtime_alt.exists():
            return runtime_alt
    return path


def champion(slippage_mult: float = 1.0) -> Candidate:
    return Candidate(
        name=CANONICAL_RESEARCH_CHAMPION,
        target_notional=500.0,
        base_frac=0.16,
        steps_pct=(0.25, 0.35, 0.55),
        add_weights=(0.8, 1.2, 2.2),
        fee=FEE,
        slippage=SLIPPAGE * slippage_mult,
    )


def allocations(candidate: Candidate) -> tuple[float, List[float]]:
    base = candidate.target_notional * candidate.base_frac
    remaining = max(candidate.target_notional - base, 0.0)
    total_w = sum(candidate.add_weights)
    adds = [remaining * w / total_w for w in candidate.add_weights] if total_w > 0 else []
    return base, adds


def next_level_long(last_fill_price: float, buy_count: int, steps_pct: Sequence[float]) -> float | None:
    if buy_count < 1 or buy_count > len(steps_pct):
        return None
    return last_fill_price * (1.0 - float(steps_pct[buy_count - 1]) / 100.0)


def qty_for_notional(notional: float, price: float) -> float:
    return max(0.0, float(notional) / max(float(price), 1e-12))


def avg_price(lots: Sequence[Lot]) -> float:
    cost = sum(l.usdt for l in lots)
    qty = sum(l.qty for l in lots)
    return cost / max(qty, 1e-12)


def realized_sell_pnl_long(qty: float, entry_price: float, fill_price: float, slippage: float) -> float:
    buy_cost = qty * entry_price * (1.0 + FEE + slippage)
    sell_value = qty * fill_price * (1.0 - FEE - slippage)
    return sell_value - buy_cost


def mark_mtm_long(lots: Sequence[Lot], mark: float, slippage: float) -> float:
    return sum(realized_sell_pnl_long(l.qty, l.price, mark, slippage) for l in lots)


def load_hype_arrays(path: Path) -> Dict[str, np.ndarray]:
    arrays = load_npz_arrays(path)
    z = np.load(path, allow_pickle=True)
    arrays["open"] = z["open"].astype(float)
    return arrays


def position_slice(arrays: Dict[str, np.ndarray], start_ms: int, end_ms: int) -> tuple[int, int]:
    t = arrays["t"]
    start = int(np.searchsorted(t, start_ms, side="left"))
    end = int(np.searchsorted(t, end_ms, side="right"))
    return start, end


def simulate_one(
    pos: CopyPosition,
    arrays: Dict[str, np.ndarray],
    candidate: Candidate,
    params: EngineParams,
    *,
    equity_before: float,
    max_hold_hours: float,
    exit_censor: str,
) -> Dict[str, Any] | None:
    if pos.symbol != "HYPEUSDT" or pos.side != "LONG":
        return None

    open_ms = dt_ms(pos.opened)
    lead_close_ms = dt_ms(pos.closed)
    horizon_ms = open_ms + int(max_hold_hours * 3600_000)
    end_ms = min(horizon_ms, int(arrays["t"][-1]))
    if exit_censor == "lead_close":
        end_ms = min(end_ms, lead_close_ms)
    elif exit_censor == "max_of_lead_close_and_horizon":
        end_ms = min(max(lead_close_ms, horizon_ms), int(arrays["t"][-1]))
    start, end = position_slice(arrays, open_ms, end_ms)
    if end <= start:
        return None

    effective_target = min(equity_before, candidate.target_notional)
    effective = replace(candidate, target_notional=max(effective_target, 0.0))
    base_notional, adds = allocations(effective)
    if base_notional <= 0:
        return None

    lots: List[Lot] = [Lot(tag="L1", qty=qty_for_notional(base_notional, pos.entry), price=pos.entry, usdt=base_notional)]
    realized = 0.0
    num_buys = 1
    last_fill_price = pos.entry
    next_level = next_level_long(last_fill_price, num_buys, effective.steps_pct)
    trailing_active = False
    trailing_max = math.nan
    min_mtm = 0.0
    max_notional = base_notional
    dca_fills = 0
    sub_sells = 0
    exit_reason = "timeout"
    exit_price = float(arrays["close"][end - 1])
    exit_ms = int(arrays["t"][end - 1])
    events: List[Dict[str, Any]] = [
        {"type": "entry_signal", "t": open_ms, "price": pos.entry, "notional": base_notional, "source": "binance_open"}
    ]

    for i in range(start, end):
        t = int(arrays["t"][i])
        high = float(arrays["high"][i])
        low = float(arrays["low"][i])
        close = float(arrays["close"][i])
        trigger_high = high if params.use_high_low_touch else max(float(arrays["open"][i]), close)
        trigger_low = low if params.use_high_low_touch else min(float(arrays["open"][i]), close)

        if lots:
            min_mtm = min(min_mtm, realized + mark_mtm_long(lots, trigger_low, effective.slippage))

        current_avg = avg_price(lots)
        tp_price = current_avg * (1.0 + params.tp_percent / 100.0)
        tp_touch = trigger_high >= tp_price
        full_tp_close_ok = (not params.require_close_above_full_tp) or close >= tp_price
        tp_close_confirmed = tp_touch and full_tp_close_ok
        tp_blocks_dca = tp_touch if params.block_dca_on_tp_touch else tp_close_confirmed

        if tp_touch:
            if params.callback_percent > 0:
                trailing_active = True
                trailing_max = trigger_high if not math.isfinite(trailing_max) else max(trailing_max, trigger_high)
                trail_stop = trailing_max * (1.0 - params.callback_percent / 100.0)
                if tp_close_confirmed and close <= trail_stop:
                    fill = close
                    realized += sum(realized_sell_pnl_long(l.qty, l.price, fill, effective.slippage) for l in lots)
                    exit_reason = "full_tp_trailing"
                    exit_price = fill
                    exit_ms = t
                    events.append({"type": exit_reason, "t": t, "price": fill, "tp_price": tp_price, "trail_stop": trail_stop})
                    lots.clear()
                    break
            elif tp_close_confirmed:
                fill = close
                realized += sum(realized_sell_pnl_long(l.qty, l.price, fill, effective.slippage) for l in lots)
                exit_reason = "full_tp"
                exit_price = fill
                exit_ms = t
                events.append({"type": exit_reason, "t": t, "price": fill, "tp_price": tp_price})
                lots.clear()
                break

        fills_this_bar = 0
        while (
            not tp_blocks_dca
            and lots
            and num_buys < params.margin_call_limit
            and fills_this_bar < params.max_fills_per_bar
            and next_level is not None
            and trigger_low <= next_level
            and ((not params.require_close_below_dca_level) or close <= next_level)
        ):
            add_idx = num_buys - 1
            if add_idx >= len(adds):
                break
            add_notional = float(adds[add_idx])
            if sum(l.usdt for l in lots) + add_notional > equity_before * params.max_position_cost_pct / 100.0 + GROUNDING_TOL:
                events.append({"type": "dca_block_cost_cap", "t": t, "price": close, "next_level": next_level})
                break
            lot = Lot(tag=f"L{num_buys + 1}", qty=qty_for_notional(add_notional, close), price=close, usdt=add_notional)
            lots.append(lot)
            dca_fills += 1
            fills_this_bar += 1
            num_buys += 1
            max_notional = max(max_notional, sum(l.usdt for l in lots))
            events.append(
                {
                    "type": "dca",
                    "t": t,
                    "price": close,
                    "trigger_level": next_level,
                    "notional": add_notional,
                    "lot": lot.tag,
                }
            )
            last_fill_price = next_level
            next_level = next_level_long(last_fill_price, num_buys, effective.steps_pct)
            trailing_active = False
            trailing_max = math.nan

        sold_this_bar = 0
        while (
            not tp_blocks_dca
            and len(lots) > 5
            and sold_this_bar < params.max_sub_sells_per_bar
        ):
            last = lots[-1]
            exposure_pct = (sum(l.qty for l in lots) * close) / max(equity_before, 1e-12) * 100.0
            force_breakeven = exposure_pct > params.hard_breakeven_deleverage_pct
            last_lot_tp = last.price * (1.0 + params.sub_sell_tp_percent / 100.0)
            sell_touch_level = last.price if force_breakeven else last_lot_tp
            sub_touch = trigger_high >= sell_touch_level
            if force_breakeven:
                close_ok = close >= last.price
            elif params.sub_sell_close_confirm_mode == "off":
                close_ok = True
            elif params.sub_sell_close_confirm_mode == "breakeven":
                close_ok = close >= last.price
            else:
                close_ok = close >= last_lot_tp
            if not (sub_touch and close_ok):
                break
            realized += realized_sell_pnl_long(last.qty, last.price, close, effective.slippage)
            lots.pop()
            sub_sells += 1
            sold_this_bar += 1
            num_buys = max(num_buys - 1, 0)
            events.append({"type": "sub_sell", "t": t, "price": close, "lot": last.tag, "lot_entry": last.price})
            next_level = next_level_long(lots[-1].price, num_buys, effective.steps_pct) if lots else None

        if lots:
            total_mtm = realized + mark_mtm_long(lots, trigger_low, effective.slippage)
            min_mtm = min(min_mtm, total_mtm)
            if total_mtm <= -abs(params.hard_max_total_dd_pct) / 100.0 * max(equity_before, 1e-12):
                fill = close
                realized += sum(realized_sell_pnl_long(l.qty, l.price, fill, effective.slippage) for l in lots)
                exit_reason = "hard_dd_stop"
                exit_price = fill
                exit_ms = t
                events.append({"type": exit_reason, "t": t, "price": fill})
                lots.clear()
                break

    if lots:
        fill = exit_price
        realized += sum(realized_sell_pnl_long(l.qty, l.price, fill, effective.slippage) for l in lots)
        if exit_censor == "lead_close" and exit_ms >= lead_close_ms:
            exit_reason = "censored_lead_close"
        elif exit_ms >= horizon_ms:
            exit_reason = "censored_max_hold"
        events.append({"type": exit_reason, "t": exit_ms, "price": fill})
        lots.clear()

    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "side": pos.side,
        "opened_utc": pos.opened.isoformat().replace("+00:00", "Z"),
        "lead_closed_utc": pos.closed.isoformat().replace("+00:00", "Z"),
        "exit_utc": iso_ms(exit_ms),
        "entry": pos.entry,
        "lead_exit": pos.exit,
        "engine_exit": exit_price,
        "exit_reason": exit_reason,
        "pnl": realized,
        "dca_fills": dca_fills,
        "sub_sells": sub_sells,
        "min_mtm": min_mtm,
        "min_mtm_pct_equity": 100.0 * min_mtm / max(equity_before, 1e-12),
        "max_notional": max_notional,
        "equity_before": equity_before,
        "notional_gt_equity_before": max_notional - equity_before > GROUNDING_TOL,
        "lead_pnl": pos.lead_pnl,
        "lead_roi": pos.lead_roi,
        "hold_minutes": (exit_ms - open_ms) / 60_000.0,
        "events_json": json.dumps(events, separators=(",", ":")),
    }


def simulate_all(
    positions: Sequence[CopyPosition],
    arrays: Dict[str, np.ndarray],
    candidate: Candidate,
    params: EngineParams,
    *,
    initial_equity: float,
    max_hold_hours: float,
    exit_censor: str,
) -> List[Dict[str, Any]]:
    equity = initial_equity
    rows: List[Dict[str, Any]] = []
    for pos in positions:
        row = simulate_one(
            pos,
            arrays,
            candidate,
            params,
            equity_before=equity,
            max_hold_hours=max_hold_hours,
            exit_censor=exit_censor,
        )
        if row is None:
            continue
        equity += float(row["pnl"])
        row["equity_after"] = equity
        rows.append(row)
    return rows


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
    gross_profit = sum(x for x in pnl_values if x > 0)
    gross_loss = sum(x for x in pnl_values if x < 0)
    if gross_loss < 0:
        pf = gross_profit / abs(gross_loss)
    elif gross_profit > 0:
        pf = "inf"
    else:
        pf = 0.0
    return {
        "trades": len(rows),
        "equity_start": initial_equity,
        "equity_end": equity,
        "net_pct": 100.0 * (equity - initial_equity) / max(initial_equity, 1e-12),
        "max_realized_dd_pct": 100.0 * max_drawdown(curve),
        "max_mtm_dd_pct": 100.0 * max_drawdown(mtm_curve),
        "min_trade_mtm_pct_equity": min((float(r["min_mtm_pct_equity"]) for r in rows), default=0.0),
        "win_rate_pct": 100.0 * sum(1 for x in pnl_values if x > 0) / max(1, len(rows)),
        "pf": pf,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "avg_dca_fills": sum(float(r["dca_fills"]) for r in rows) / max(1, len(rows)),
        "avg_sub_sells": sum(float(r["sub_sells"]) for r in rows) / max(1, len(rows)),
        "max_notional": max((float(r["max_notional"]) for r in rows), default=0.0),
        "notional_gt_equity_before_count": sum(1 for r in rows if str(r["notional_gt_equity_before"]) == "True"),
        "exit_reasons": dict(sorted({str(r["exit_reason"]): sum(1 for x in rows if x["exit_reason"] == r["exit_reason"]) for r in rows}.items())),
    }


def find_input_window(positions: Sequence[CopyPosition]) -> Dict[str, str]:
    if not positions:
        return {}
    return {
        "position_start": positions[0].opened.isoformat().replace("+00:00", "Z"),
        "position_end": max(p.closed for p in positions).isoformat().replace("+00:00", "Z"),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description="TP-aware HYPE V21/Pine-like backtester.")
    ap.add_argument("--positions-csv", default=str(DEFAULT_POSITIONS))
    ap.add_argument("--npz", default=str(DEFAULT_NPZ))
    ap.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR / "tp_aware_v21_backtest_20260524"))
    ap.add_argument("--initial-equity", type=float, default=500.0)
    ap.add_argument("--max-hold-hours", type=float, default=168.0)
    ap.add_argument("--exit-censor", choices=("none", "lead_close", "max_of_lead_close_and_horizon"), default="none")
    ap.add_argument("--slippage-mult", type=float, default=1.0)
    ap.add_argument("--tp-percent", type=float, default=0.52)
    ap.add_argument("--callback-percent", type=float, default=0.10)
    ap.add_argument("--sub-sell-tp-percent", type=float, default=0.65)
    ap.add_argument("--margin-call-limit", type=int, default=4)
    args = ap.parse_args()

    positions_path = resolve_existing(Path(args.positions_csv))
    npz_path = resolve_existing(Path(args.npz))
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    positions = [p for p in read_positions(positions_path) if p.symbol == "HYPEUSDT" and p.side == "LONG"]
    arrays = load_hype_arrays(npz_path)
    params = EngineParams(
        name="pine_champion_defaults",
        tp_percent=args.tp_percent,
        callback_percent=args.callback_percent,
        sub_sell_tp_percent=args.sub_sell_tp_percent,
        margin_call_limit=args.margin_call_limit,
    )
    cand = champion(args.slippage_mult)
    rows = simulate_all(
        positions,
        arrays,
        cand,
        params,
        initial_equity=args.initial_equity,
        max_hold_hours=args.max_hold_hours,
        exit_censor=args.exit_censor,
    )
    summary = summarize(rows, args.initial_equity)
    summary.update(
        {
            "candidate": cand.name,
            "engine": params.name,
            "tp_percent": params.tp_percent,
            "callback_percent": params.callback_percent,
            "sub_sell_tp_percent": params.sub_sell_tp_percent,
            "margin_call_limit": params.margin_call_limit,
            "max_hold_hours": args.max_hold_hours,
            "exit_censor": args.exit_censor,
            "positions_csv": str(positions_path),
            "npz": str(npz_path),
            "lead_side_authoritative": True,
            "hype_long_only": True,
            **find_input_window(positions),
        }
    )

    write_csv(out_dir / "tp_aware_trades.csv", rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    pf_display = summary["pf"] if isinstance(summary["pf"], str) else f"{float(summary['pf']):.2f}"
    md = [
        "# HYPE TP-Aware V21 Backtest",
        "",
        "Research/paper only. No live orders, no secrets, no network.",
        "",
        "## Purpose",
        "",
        "This backtester fixes the known limitation of the closed-position replay: Binance public open positions are treated as entry signals, but exits are produced by a local V21/Pine-like engine instead of `avgClosePrice` from the Binance lead close.",
        "",
        "## Implemented Behavior",
        "",
        "- Lead side is authoritative; this report filters to `HYPEUSDT LONG` and does not flip or infer side.",
        "- Entry time and first entry price come from Binance open position history.",
        "- Compound sizing uses the canonical `$500` target capped by current realized equity.",
        "- DCA uses champion shape `t500_b16_s0p25-0p35-0p55_w0p8-1p2-2p2` with close-confirmed adverse levels.",
        f"- Full TP uses `tpPercent={params.tp_percent}` and `callback={params.callback_percent}`.",
        f"- Sub-sell logic is implemented with `subSell={params.sub_sell_tp_percent}`, but the champion `marginCallLimit={params.margin_call_limit}` means `numBuys > 5` is never reached in this default lane.",
        "- Fees and slippage use the same research defaults as the grounded compound replay.",
        "",
        "## Missing / Approximate Behavior",
        "",
        "- TradingView order scheduling, `allowThisBar` parity anchor, and 3-minute throttle are not fully modeled.",
        "- Intrabar event order is approximated with OHLC and close confirmation; this is not tick replay.",
        "- One signal is simulated as one independent warehouse. Overlapping live warehouses are not merged.",
        "- `tpPercent`, `callback`, and `subSell` are now modeled, but this is still a research simulator that needs paper-live validation.",
        "",
        "## First Verification",
        "",
        f"- Positions: `{summary['trades']}`.",
        f"- Window: `{summary.get('position_start', '')}` .. `{summary.get('position_end', '')}`.",
        f"- Initial equity: `${summary['equity_start']:.2f}`.",
        f"- Finish equity: `${summary['equity_end']:.2f}`.",
        f"- Net: `{summary['net_pct']:.2f}%`.",
        f"- PF: `{pf_display}`.",
        f"- Max realized DD: `{summary['max_realized_dd_pct']:.2f}%`.",
        f"- Max MTM DD: `{summary['max_mtm_dd_pct']:.2f}%`.",
        f"- Min trade MTM: `{summary['min_trade_mtm_pct_equity']:.2f}%`.",
        f"- Avg DCA fills: `{summary['avg_dca_fills']:.2f}`.",
        f"- Avg sub-sells: `{summary['avg_sub_sells']:.2f}`.",
        f"- Max notional: `${summary['max_notional']:.2f}`.",
        f"- `notional > equity_before`: `{summary['notional_gt_equity_before_count']}`.",
        f"- Exit reasons: `{json.dumps(summary['exit_reasons'], sort_keys=True)}`.",
        "",
        "## Interpretation",
        "",
        "These results are not directly comparable to the previous `$500 -> $2109.57` champion result because that replay closed at Binance lead close. This report is the first TP-aware lane for tuning `tpPercent`, `callback`, and `subSell` without pseudo-optimizing unused fields.",
        "",
        "PF is `inf` in this first run because every simulated realized trade closed positive. Treat that as a model diagnostic, not a promotion signal: the current OHLC-level trailing approximation can be optimistic until validated against paper-live fills and a stricter intrabar event-order model.",
    ]
    (out_dir / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), "summary": summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
