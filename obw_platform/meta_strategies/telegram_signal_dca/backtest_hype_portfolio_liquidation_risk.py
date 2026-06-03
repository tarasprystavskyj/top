#!/usr/bin/env python3
"""Offline portfolio liquidation-risk meta-backtester.

Research-only. Uses local Binance-copy position history plus local 1m NPZ
market data. It does not read secrets, call exchange APIs, or place orders.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

import numpy as np

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compare_binance_copy_positions_dca import CopyPosition, read_positions  # noqa: E402
from telegram_signal_dca_compare import ret_for  # noqa: E402


DEFAULT_REPORT_DIR = (
    Path("obw_platform")
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "binance_430051_hype_v21_loop_20260523"
)
DEFAULT_POSITIONS = DEFAULT_REPORT_DIR / "wave_002" / "position_refresh" / "position_history_normalized.csv"
DEFAULT_NPZ = DEFAULT_REPORT_DIR / "binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz"
STRICT_FILL_MODE = "close_beyond_skip_boundary"
GROUNDING_TOL = 1e-9


@dataclass(frozen=True)
class LegConfig:
    name: str
    allocation_usdt: float
    leverage: float = 1.0
    leverage_mode: str = "fixed"
    symbol: str = "HYPEUSDT"
    margin_mode: str = "both"
    base_frac: float = 1.0
    dca_steps_pct: tuple[float, ...] = ()
    dca_add_weights: tuple[float, ...] = ()
    side: str = "signal"


def iso_ms(ts_ms: int) -> str:
    return datetime.fromtimestamp(ts_ms / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def parse_float(raw: Any, default: float) -> float:
    try:
        if raw in ("", None):
            return default
        value = float(raw)
        return value if math.isfinite(value) else default
    except Exception:
        return default


def parse_float_tuple(raw: Any) -> tuple[float, ...]:
    if raw in ("", None):
        return ()
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    return tuple(float(x) for x in raw)


def normalize_margin_mode(raw: Any) -> str:
    value = str(raw or "both").strip().lower()
    if value in {"isolated", "iso"}:
        return "isolated"
    if value in {"cross", "crossed"}:
        return "cross"
    if value in {"both", "all"}:
        return "both"
    raise ValueError(f"unsupported margin_mode={raw!r}; expected isolated, cross, or both")


def normalize_leverage_mode(raw: Any) -> str:
    value = str(raw or "fixed").strip().lower()
    if value in {"fixed", "source", "copy", "source_div2", "copy_div2"}:
        return value
    raise ValueError(f"unsupported leverage_mode={raw!r}; expected fixed, source, copy, source_div2, or copy_div2")


def effective_leverage(pos: CopyPosition, leg: LegConfig) -> float:
    source = float(pos.leverage) if math.isfinite(float(pos.leverage or 0.0)) and float(pos.leverage or 0.0) > 0 else 1.0
    mode = normalize_leverage_mode(leg.leverage_mode)
    if mode in {"source", "copy"}:
        return max(1.0, source)
    if mode in {"source_div2", "copy_div2"}:
        return max(1.0, math.floor(source / 2.0)) if source > 1.0 else 1.0
    return max(1.0, float(leg.leverage))


def leg_from_dict(raw: Dict[str, Any], index: int) -> LegConfig:
    allocation = parse_float(
        raw.get("allocation_usdt", raw.get("allocation", raw.get("target_notional", raw.get("max_notional_usdt")))),
        math.nan,
    )
    if not math.isfinite(allocation) or allocation <= 0.0:
        raise ValueError(f"leg {index} must define positive allocation_usdt")
    leverage = max(1.0, parse_float(raw.get("leverage"), 1.0))
    leverage_mode = raw.get("leverage_mode", raw.get("source_leverage_mode", "fixed"))
    base_frac = parse_float(raw.get("base_frac", raw.get("base_fraction")), 1.0)
    if base_frac <= 0.0:
        raise ValueError(f"leg {index} base_frac must be positive")
    return LegConfig(
        name=str(raw.get("name") or f"leg_{index}"),
        allocation_usdt=allocation,
        leverage=leverage,
        leverage_mode=normalize_leverage_mode(leverage_mode),
        symbol=str(raw.get("symbol") or "HYPEUSDT").upper().strip(),
        margin_mode=normalize_margin_mode(raw.get("margin_mode", "both")),
        base_frac=base_frac,
        dca_steps_pct=parse_float_tuple(raw.get("dca_steps_pct", raw.get("steps_pct"))),
        dca_add_weights=parse_float_tuple(raw.get("dca_add_weights", raw.get("add_weights"))),
        side=str(raw.get("side") or "signal").upper().strip(),
    )


def load_legs(*, legs_json: str, portfolio_config: Path | None, allocation_usdt: float, leverage: float) -> List[LegConfig]:
    if portfolio_config is not None:
        data = json.loads(portfolio_config.read_text(encoding="utf-8"))
        raw_legs = data if isinstance(data, list) else data.get("legs")
        if not isinstance(raw_legs, list):
            raise ValueError("portfolio config must be a JSON list or an object with a 'legs' list")
        return [leg_from_dict(x, i + 1) for i, x in enumerate(raw_legs)]
    if legs_json:
        raw_legs = json.loads(legs_json)
        if not isinstance(raw_legs, list):
            raise ValueError("--legs-json must be a JSON list")
        return [leg_from_dict(x, i + 1) for i, x in enumerate(raw_legs)]
    return [
        LegConfig(
            name="hype_live_style",
            allocation_usdt=float(allocation_usdt),
            leverage=max(1.0, float(leverage)),
            leverage_mode="fixed",
        )
    ]


def normalize_symbol(raw: Any) -> str:
    text = str(raw or "").upper().strip()
    if "/" in text:
        base = text.split("/", 1)[0]
        rest = text.split("/", 1)[1]
        quote = rest.split(":", 1)[0]
        return f"{base}{quote}"
    return text.replace("-", "").replace("_", "")


def _npz_arrays_slice(z: Any, start: int | None = None, end: int | None = None) -> Dict[str, np.ndarray]:
    sl = slice(start, end)
    return {
        "t": z["timestamp_s"][sl].astype(np.int64) * 1000,
        "open": z["open"][sl].astype(float),
        "high": z["high"][sl].astype(float),
        "low": z["low"][sl].astype(float),
        "close": z["close"][sl].astype(float),
    }


def load_npz_arrays(path: Path) -> Dict[str, Dict[str, np.ndarray]]:
    z = np.load(path, allow_pickle=True)
    if "symbols" in z and "offsets" in z:
        symbols = [normalize_symbol(x) for x in z["symbols"]]
        offsets = z["offsets"].astype(np.int64)
        return {
            symbol: _npz_arrays_slice(z, int(offsets[i]), int(offsets[i + 1]))
            for i, symbol in enumerate(symbols)
        }
    return {"__single__": _npz_arrays_slice(z)}


def arrays_for_symbol(market_arrays: Dict[str, Dict[str, np.ndarray]], symbol: str) -> Dict[str, np.ndarray] | None:
    normalized = normalize_symbol(symbol)
    if normalized in market_arrays:
        return market_arrays[normalized]
    return market_arrays.get("__single__")


def allocations(leg: LegConfig) -> tuple[float, List[float]]:
    base = min(float(leg.allocation_usdt), float(leg.allocation_usdt) * float(leg.base_frac))
    remaining = max(float(leg.allocation_usdt) - base, 0.0)
    weights = [float(x) for x in leg.dca_add_weights]
    if not weights and leg.dca_steps_pct:
        weights = [1.0 for _ in leg.dca_steps_pct]
    if len(weights) > len(leg.dca_steps_pct):
        weights = weights[: len(leg.dca_steps_pct)]
    total = sum(weights)
    adds = [remaining * w / total for w in weights] if total > 0.0 else []
    return base, adds


def dca_levels(side: str, entry: float, steps_pct: Sequence[float]) -> List[float]:
    levels: List[float] = []
    last = float(entry)
    for step in steps_pct:
        if side == "LONG":
            last *= 1.0 - float(step) / 100.0
        else:
            last *= 1.0 + float(step) / 100.0
        levels.append(last)
    return levels


def level_crossed(side: str, *, low: float, high: float, close: float, level: float, fill_mode: str) -> bool:
    if fill_mode in {"close_beyond", "close_beyond_skip_boundary"}:
        return close <= level if side == "LONG" else close >= level
    return low <= level if side == "LONG" else high >= level


def entry_for_source(pos: CopyPosition, arrays: Dict[str, np.ndarray], entry_source: str) -> float:
    if entry_source == "avgCost":
        return float(pos.entry)
    fields = {
        "first_bar_open": "open",
        "first_bar_close": "close",
        "first_bar_high": "high",
        "first_bar_low": "low",
        "next_bar_open": "open",
    }
    if entry_source not in fields:
        raise ValueError(f"unknown entry_source={entry_source!r}")
    i = int(np.searchsorted(arrays["t"], ms(pos.opened), side="left"))
    if entry_source == "next_bar_open":
        i += 1
    i = min(max(i, 0), len(arrays["t"]) - 1)
    return float(arrays[fields[entry_source]][i])


def effective_side(pos: CopyPosition, leg: LegConfig) -> str | None:
    leg_side = str(leg.side or "signal").upper().strip()
    if leg_side in {"SIGNAL", ""}:
        return pos.side
    if leg_side in {"LONG", "SHORT"}:
        return leg_side
    if leg_side == "LONG_ONLY":
        return pos.side if pos.side == "LONG" else None
    if leg_side == "SHORT_ONLY":
        return pos.side if pos.side == "SHORT" else None
    raise ValueError(f"unsupported leg side={leg.side!r}")


def isolated_metrics(
    side: str,
    avg_entry: float,
    risk_mark: float,
    notional: float,
    leverage: float,
    mtm: float,
    maintenance_margin_pct: float,
) -> Dict[str, float]:
    leverage = max(float(leverage), 1.0)
    margin = float(notional) / leverage
    maintenance_margin = float(notional) * max(float(maintenance_margin_pct), 0.0) / 100.0
    if side == "LONG":
        liq_price = avg_entry * (1.0 - 1.0 / leverage)
        price_buffer_pct = 100.0 * (risk_mark - liq_price) / max(avg_entry, 1e-12)
    else:
        liq_price = avg_entry * (1.0 + 1.0 / leverage)
        price_buffer_pct = 100.0 * (liq_price - risk_mark) / max(avg_entry, 1e-12)
    buffer_usd = margin + mtm - maintenance_margin
    return {
        "isolated_margin_usd": margin,
        "maintenance_margin_usd": maintenance_margin,
        "isolated_liq_price": liq_price,
        "isolated_buffer_usd": buffer_usd,
        "isolated_buffer_pct_margin": 100.0 * buffer_usd / max(margin, 1e-12),
        "isolated_price_buffer_pct": price_buffer_pct,
        "isolated_breach": 1.0 if buffer_usd <= GROUNDING_TOL else 0.0,
    }


def simulate_leg_trade(
    pos: CopyPosition,
    leg: LegConfig,
    arrays: Dict[str, np.ndarray],
    *,
    fill_mode: str,
    entry_source: str,
    fee: float,
    slippage: float,
    maintenance_margin_pct: float,
    include_boundary_risk: bool,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]] | None:
    if leg.symbol not in {"*", "ALL"} and pos.symbol != leg.symbol:
        return None
    side = effective_side(pos, leg)
    if side is None:
        return None
    t = arrays["t"]
    start = int(np.searchsorted(t, ms(pos.opened), side="left"))
    end = int(np.searchsorted(t, ms(pos.closed), side="right"))
    if end <= start:
        return None
    ts = t[start:end]
    high = arrays["high"][start:end]
    low = arrays["low"][start:end]
    close = arrays["close"][start:end]

    entry = entry_for_source(pos, arrays, entry_source)
    base_notional, adds = allocations(leg)
    levels = dca_levels(side, entry, leg.dca_steps_pct[: len(adds)])
    avg_entry = float(entry)
    notional = float(base_notional)
    leverage = effective_leverage(pos, leg)
    fills = 0
    fills_json: List[Dict[str, Any]] = []
    snapshots: List[Dict[str, Any]] = []
    skip_boundary = fill_mode in {"touch_skip_boundary", "close_beyond_skip_boundary"}

    for i in range(len(ts)):
        boundary = skip_boundary and (i == 0 or i == len(ts) - 1)
        can_fill = not boundary
        fills_this_candle = 0
        while can_fill and fills < len(levels):
            crossed = level_crossed(
                side,
                low=float(low[i]),
                high=float(high[i]),
                close=float(close[i]),
                level=float(levels[fills]),
                fill_mode=fill_mode,
            )
            if not crossed:
                break
            if fills_this_candle >= 1 and skip_boundary:
                break
            add_notional = float(adds[fills])
            old_qty = notional / max(avg_entry, 1e-12)
            add_qty = add_notional / max(levels[fills], 1e-12)
            notional += add_notional
            avg_entry = notional / max(old_qty + add_qty, 1e-12)
            fills_json.append({"t": int(ts[i]), "level": float(levels[fills]), "notional": add_notional})
            fills += 1
            fills_this_candle += 1

        if boundary and not include_boundary_risk:
            continue
        risk_mark = float(low[i] if side == "LONG" else high[i])
        mtm_ret = ret_for(side, avg_entry, risk_mark) - 2.0 * fee - 2.0 * slippage
        mtm = mtm_ret * notional
        iso = isolated_metrics(side, avg_entry, risk_mark, notional, leverage, mtm, maintenance_margin_pct)
        snapshots.append(
            {
                "t": int(ts[i]),
                "t_utc": iso_ms(int(ts[i])),
                "source_position_id": pos.id,
                "leg_name": leg.name,
                "symbol": pos.symbol,
                "side": side,
                "avg_entry": avg_entry,
                "risk_mark": risk_mark,
                "notional": notional,
                "leverage": leverage,
                "leverage_mode": leg.leverage_mode,
                "source_leverage": pos.leverage,
                "mtm_usd": mtm,
                **iso,
            }
        )

    gross_ret = ret_for(side, avg_entry, float(pos.exit))
    pnl = (gross_ret - 2.0 * fee - 2.0 * slippage) * notional
    trade = {
        "source_position_id": pos.id,
        "leg_name": leg.name,
        "symbol": pos.symbol,
        "side": side,
        "opened_utc": pos.opened.isoformat().replace("+00:00", "Z"),
        "closed_utc": pos.closed.isoformat().replace("+00:00", "Z"),
        "entry": entry,
        "exit": pos.exit,
        "avg_entry": avg_entry,
        "allocation_usdt": leg.allocation_usdt,
        "leverage": leverage,
        "leverage_mode": leg.leverage_mode,
        "source_leverage": pos.leverage,
        "base_frac": leg.base_frac,
        "dca_fills": fills,
        "notional": notional,
        "pnl": pnl,
        "min_isolated_buffer_usd": min((float(x["isolated_buffer_usd"]) for x in snapshots), default=0.0),
        "min_isolated_buffer_pct_margin": min((float(x["isolated_buffer_pct_margin"]) for x in snapshots), default=0.0),
        "min_isolated_price_buffer_pct": min((float(x["isolated_price_buffer_pct"]) for x in snapshots), default=0.0),
        "isolated_breach_bars": sum(int(x["isolated_breach"]) for x in snapshots),
        "candles": len(ts),
        "risk_snapshots": len(snapshots),
        "fills_json": json.dumps(fills_json, separators=(",", ":")),
    }
    return trade, snapshots


def margin_mode_includes(leg: LegConfig, scenario: str) -> bool:
    return leg.margin_mode == "both" or leg.margin_mode == scenario


def summarize_isolated(snapshots: Sequence[Dict[str, Any]], trades: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    breach_snaps = [s for s in snapshots if int(float(s.get("isolated_breach", 0.0))) > 0]
    breach_trade_ids = {(s["source_position_id"], s["leg_name"]) for s in breach_snaps}
    return {
        "breach_bar_count": len(breach_snaps),
        "breach_trade_count": len(breach_trade_ids),
        "min_buffer_usd": min((float(s["isolated_buffer_usd"]) for s in snapshots), default=0.0),
        "min_buffer_pct_margin": min((float(s["isolated_buffer_pct_margin"]) for s in snapshots), default=0.0),
        "min_price_buffer_pct": min((float(s["isolated_price_buffer_pct"]) for s in snapshots), default=0.0),
        "max_margin_used_usd": max((float(s["isolated_margin_usd"]) for s in snapshots), default=0.0),
        "trade_count": len(trades),
    }


def summarize_cross(
    snapshots: Sequence[Dict[str, Any]],
    trades: Sequence[Dict[str, Any]],
    *,
    initial_equity: float,
    maintenance_margin_pct: float,
) -> tuple[Dict[str, Any], List[Dict[str, Any]]]:
    by_t: Dict[int, List[Dict[str, Any]]] = {}
    for snap in snapshots:
        by_t.setdefault(int(snap["t"]), []).append(snap)
    close_events = sorted((ms(datetime.fromisoformat(str(t["closed_utc"]).replace("Z", "+00:00"))), float(t["pnl"])) for t in trades)
    realized = 0.0
    close_i = 0
    rows: List[Dict[str, Any]] = []
    for ts in sorted(by_t):
        while close_i < len(close_events) and close_events[close_i][0] < ts:
            realized += close_events[close_i][1]
            close_i += 1
        active = by_t[ts]
        mtm = sum(float(s["mtm_usd"]) for s in active)
        margin_used = sum(float(s["isolated_margin_usd"]) for s in active)
        gross_notional = sum(float(s["notional"]) for s in active)
        maintenance_margin = gross_notional * max(float(maintenance_margin_pct), 0.0) / 100.0
        equity_buffer = float(initial_equity) + realized + mtm
        liquidation_buffer = equity_buffer - maintenance_margin
        row = {
            "t": ts,
            "t_utc": iso_ms(ts),
            "active_legs": len(active),
            "realized_pnl_before_t": realized,
            "cross_mtm_usd": mtm,
            "cross_gross_notional_usd": gross_notional,
            "cross_initial_margin_used_usd": margin_used,
            "cross_maintenance_margin_usd": maintenance_margin,
            "cross_equity_buffer_usd": equity_buffer,
            "cross_equity_buffer_pct_start": 100.0 * equity_buffer / max(float(initial_equity), 1e-12),
            "cross_equity_buffer_pct_gross_notional": 100.0 * equity_buffer / max(gross_notional, 1e-12),
            "cross_liquidation_buffer_usd": liquidation_buffer,
            "cross_liquidation_buffer_pct_start": 100.0 * liquidation_buffer / max(float(initial_equity), 1e-12),
            "cross_margin_excess_usd": equity_buffer - margin_used,
            "cross_breach": liquidation_buffer <= GROUNDING_TOL,
        }
        rows.append(row)
    breach_rows = [r for r in rows if bool(r["cross_breach"])]
    summary = {
        "breach_bar_count": len(breach_rows),
        "min_equity_buffer_usd": min((float(r["cross_liquidation_buffer_usd"]) for r in rows), default=0.0),
        "min_equity_buffer_pct_start": min((float(r["cross_liquidation_buffer_pct_start"]) for r in rows), default=0.0),
        "min_equity_buffer_pct_gross_notional": min((float(r["cross_equity_buffer_pct_gross_notional"]) for r in rows), default=0.0),
        "min_margin_excess_usd": min((float(r["cross_margin_excess_usd"]) for r in rows), default=0.0),
        "max_gross_notional_usd": max((float(r["cross_gross_notional_usd"]) for r in rows), default=0.0),
        "max_initial_margin_used_usd": max((float(r["cross_initial_margin_used_usd"]) for r in rows), default=0.0),
        "trade_count": len(trades),
    }
    return summary, rows


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_report(path: Path, summary: Dict[str, Any], legs: Sequence[LegConfig]) -> None:
    isolated = summary.get("isolated", {})
    cross = summary.get("cross", {})
    lines = [
        "# HYPE Portfolio Liquidation-Risk Backtest",
        "",
        "Offline research-only meta-backtest. It uses local position history and local HYPE 1m NPZ candles.",
        "",
        "## Portfolio Legs",
        "",
        "| leg | symbol | allocation | leverage mode | leverage | margin mode | base frac | DCA steps | DCA weights |",
        "|---|---|---:|---|---:|---|---:|---|---|",
    ]
    for leg in legs:
        lines.append(
            f"| {leg.name} | {leg.symbol} | {leg.allocation_usdt:.2f} | "
            f"{leg.leverage_mode} | {leg.leverage:g} | {leg.margin_mode} | {leg.base_frac:.4f} | "
            f"{json.dumps(leg.dca_steps_pct)} | {json.dumps(leg.dca_add_weights)} |"
        )
    lines.extend(
        [
            "",
            "## Results",
            "",
            "| scenario | breach bars | breach trades | min buffer USD | min buffer % | extra metric |",
            "|---|---:|---:|---:|---:|---:|",
            (
                f"| isolated | {isolated.get('breach_bar_count', 0)} | {isolated.get('breach_trade_count', 0)} | "
                f"{float(isolated.get('min_buffer_usd', 0.0)):.4f} | "
                f"{float(isolated.get('min_buffer_pct_margin', 0.0)):.4f} | "
                f"price buffer {float(isolated.get('min_price_buffer_pct', 0.0)):.4f}% |"
            ),
            (
                f"| cross | {cross.get('breach_bar_count', 0)} | n/a | "
                f"{float(cross.get('min_equity_buffer_usd', 0.0)):.4f} | "
                f"{float(cross.get('min_equity_buffer_pct_start', 0.0)):.4f} | "
                f"margin excess {float(cross.get('min_margin_excess_usd', 0.0)):.4f} |"
            ),
            "",
            "## Model Notes",
            "",
            "- Isolated breach means `position initial margin + adverse intrabar MTM - maintenance margin <= 0`.",
            "- Cross breach means `initial shared equity + realized PnL before the minute + concurrent adverse intrabar MTM - maintenance margin <= 0`.",
            "- Maintenance margin is a flat configured approximation; funding, exchange-specific tiers, liquidation fees, and mark/index divergence are not modeled.",
            "- For mixed concurrent long/short exposure, adverse lows for longs and adverse highs for shorts are aggregated conservatively even though both extremes may not occur at the same instant.",
        ]
    )
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_backtest(
    positions: Sequence[CopyPosition],
    market_arrays: Dict[str, Dict[str, np.ndarray]],
    legs: Sequence[LegConfig],
    *,
    initial_equity: float,
    fill_mode: str,
    entry_source: str,
    fee: float,
    slippage: float,
    maintenance_margin_pct: float,
    include_boundary_risk: bool,
) -> tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    if "t" in market_arrays:
        market_arrays = {"__single__": market_arrays}  # type: ignore[dict-item]
    trades: List[Dict[str, Any]] = []
    snapshots: List[Dict[str, Any]] = []
    skipped_missing_market = 0
    for pos in positions:
        arrays = arrays_for_symbol(market_arrays, pos.symbol)
        if arrays is None:
            skipped_missing_market += 1
            continue
        for leg in legs:
            result = simulate_leg_trade(
                pos,
                leg,
                arrays,
                fill_mode=fill_mode,
                entry_source=entry_source,
                fee=fee,
                slippage=slippage,
                maintenance_margin_pct=maintenance_margin_pct,
                include_boundary_risk=include_boundary_risk,
            )
            if result is None:
                continue
            trade, leg_snaps = result
            trades.append(trade)
            snapshots.extend(leg_snaps)

    isolated_legs = {leg.name for leg in legs if margin_mode_includes(leg, "isolated")}
    cross_legs = {leg.name for leg in legs if margin_mode_includes(leg, "cross")}
    isolated_trades = [t for t in trades if t["leg_name"] in isolated_legs]
    isolated_snapshots = [s for s in snapshots if s["leg_name"] in isolated_legs]
    cross_trades = [t for t in trades if t["leg_name"] in cross_legs]
    cross_snapshots = [s for s in snapshots if s["leg_name"] in cross_legs]
    cross_summary, cross_rows = summarize_cross(
        cross_snapshots,
        cross_trades,
        initial_equity=initial_equity,
        maintenance_margin_pct=maintenance_margin_pct,
    )
    summary = {
        "positions_loaded": len(positions),
        "positions_skipped_missing_market": skipped_missing_market,
        "legs": len(legs),
        "simulated_leg_trades": len(trades),
        "risk_snapshot_count": len(snapshots),
        "initial_equity": initial_equity,
        "fill_mode": fill_mode,
        "entry_source": entry_source,
        "fee": fee,
        "slippage_per_side": slippage,
        "maintenance_margin_pct": maintenance_margin_pct,
        "include_boundary_risk": include_boundary_risk,
        "liquidation_model": "conservative_offline_flat_maintenance_no_funding_or_exchange_tiers",
        "isolated": summarize_isolated(isolated_snapshots, isolated_trades),
        "cross": cross_summary,
    }
    return summary, trades, snapshots, cross_rows


def main() -> None:
    ap = argparse.ArgumentParser(description="Offline HYPE live-style portfolio liquidation risk backtester.")
    ap.add_argument("--positions-csv", default=str(DEFAULT_POSITIONS))
    ap.add_argument("--npz", default=str(DEFAULT_NPZ))
    ap.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR / "portfolio_liquidation_risk"))
    ap.add_argument("--portfolio-config", default="", help="JSON file with a top-level legs list, or a JSON list.")
    ap.add_argument("--leg-filter", default="", help="Comma-separated leg names to run from the portfolio config.")
    ap.add_argument("--legs-json", default="", help="Inline JSON list of portfolio legs.")
    ap.add_argument("--allocation-usdt", type=float, default=50.0, help="Default single-leg allocation when no leg config is passed.")
    ap.add_argument("--leverage", type=float, default=1.0, help="Default single-leg leverage when no leg config is passed.")
    ap.add_argument("--initial-equity", type=float, default=50.0, help="Shared cross-margin wallet equity for the portfolio.")
    ap.add_argument(
        "--entry-source",
        default="avgCost",
        choices=("avgCost", "first_bar_open", "first_bar_close", "first_bar_high", "first_bar_low", "next_bar_open"),
    )
    ap.add_argument(
        "--fill-mode",
        default=STRICT_FILL_MODE,
        choices=("touch", "touch_skip_boundary", "close_beyond", "close_beyond_skip_boundary"),
    )
    ap.add_argument("--include-boundary-risk", action="store_true")
    ap.add_argument("--fee", type=float, default=0.0005)
    ap.add_argument("--slippage", type=float, default=0.0009380229915652661)
    ap.add_argument("--maintenance-margin-pct", type=float, default=0.0)
    args = ap.parse_args()

    portfolio_config = Path(args.portfolio_config) if args.portfolio_config else None
    legs = load_legs(
        legs_json=args.legs_json,
        portfolio_config=portfolio_config,
        allocation_usdt=args.allocation_usdt,
        leverage=args.leverage,
    )
    if args.leg_filter:
        wanted = {x.strip() for x in str(args.leg_filter).split(",") if x.strip()}
        legs = [leg for leg in legs if leg.name in wanted]
        missing = wanted.difference(leg.name for leg in legs)
        if missing:
            raise SystemExit(f"leg-filter names not found: {sorted(missing)}")
    if not legs:
        raise SystemExit("no portfolio legs selected")
    positions = read_positions(Path(args.positions_csv))
    market_arrays = load_npz_arrays(Path(args.npz))
    summary, trades, snapshots, cross_rows = run_backtest(
        positions,
        market_arrays,
        legs,
        initial_equity=float(args.initial_equity),
        fill_mode=args.fill_mode,
        entry_source=args.entry_source,
        fee=float(args.fee),
        slippage=float(args.slippage),
        maintenance_margin_pct=float(args.maintenance_margin_pct),
        include_boundary_risk=bool(args.include_boundary_risk),
    )

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    write_csv(out_dir / "leg_trades.csv", trades)
    write_csv(out_dir / "isolated_risk_snapshots.csv", snapshots)
    write_csv(out_dir / "cross_risk_timeline.csv", cross_rows)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    write_report(out_dir / "REPORT.md", summary, legs)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    print(f"[done] {out_dir / 'REPORT.md'}")


if __name__ == "__main__":
    main()
