#!/usr/bin/env python3
"""Replay historical external signals through the real V21 one-leg wrapper.

This is intentionally separate from the older simplified DCA overlay scripts.
External signals only gate the first directional leg. Sizing and management are
delegated to the real V21 Cryptomine one-leg strategy wrappers.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import numpy as np
import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from obw_platform.telegram_signal_tools.telegram_v21_one_leg_wrapper import (  # noqa: E402
    load_one_leg_config,
    make_strategy,
    manage_existing_position,
    open_external_signal,
    restore_state,
)


DEFAULT_OUT = (
    ROOT
    / "obw_platform"
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "full_v21_external_signal_replay_20260519"
)
DEFAULT_BINANCE_REPORTS = [
    ROOT / "obw_platform/meta_strategies/telegram_signal_dca/reports/binance_copy_4751838302089254401_20260519_ttl72_reversal",
    ROOT / "obw_platform/meta_strategies/telegram_signal_dca/reports/binance_copy_4906010685108267264_20260519",
]


@dataclass(frozen=True)
class ExternalSignalRow:
    signal_id: str
    source: str
    source_type: str
    symbol: str
    side: str
    ts: datetime
    max_hold_hours: float
    rows: list[dict[str, Any]] | None = None
    coverage_note: str = ""


def parse_dt(raw: Any) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def ms_iso(ms: int) -> str:
    return iso(datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc))


def norm_side(raw: Any) -> str:
    side = str(raw or "").upper().strip()
    if side in {"LONG", "BUY"}:
        return "LONG"
    if side in {"SHORT", "SELL"}:
        return "SHORT"
    return side


def market_symbol(raw: Any) -> str:
    s = str(raw or "").upper().strip()
    if "/" in s:
        return s
    if s.endswith("USDT"):
        return f"{s[:-4]}/USDT:USDT"
    return f"{s}/USDT:USDT"


def safe_float(raw: Any, default: float = 0.0) -> float:
    try:
        if raw in ("", None):
            return default
        return float(str(raw).replace(",", ""))
    except Exception:
        return default


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as fp:
        return list(csv.DictReader(fp))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: list[str] = []
    seen = set()
    for row in rows:
        for key in row:
            if key not in seen:
                seen.add(key)
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


class NpzStore:
    def __init__(self, path: Path, parts_dir: Path | None = None):
        self.path = path
        self.parts_dir = parts_dir
        self.z = np.load(path, allow_pickle=True)
        self.symbols = [str(x) for x in self.z["symbols"]]
        self.offsets = self.z["offsets"]
        self.by_symbol = {s.upper(): i for i, s in enumerate(self.symbols)}
        self.part_by_symbol: dict[str, Path] = {}
        if parts_dir and parts_dir.exists():
            for part in parts_dir.glob("*_bingx_1m_event_windows.npz"):
                base = part.name.split("_bingx_", 1)[0].upper()
                self.part_by_symbol[f"{base}/USDT:USDT"] = part

    def has_symbol(self, symbol: str) -> bool:
        want = market_symbol(symbol).upper()
        return want in self.part_by_symbol or want in self.by_symbol

    def rows_for_symbol(self, symbol: str) -> list[dict[str, Any]]:
        want = market_symbol(symbol).upper()
        part = self.part_by_symbol.get(want)
        if part is not None:
            z = np.load(part, allow_pickle=True)
            a, b = 0, len(z["timestamp_s"])
        else:
            z = self.z
            idx = self.by_symbol[want]
            a, b = int(self.offsets[idx]), int(self.offsets[idx + 1])
        ts = np.asarray(z["timestamp_s"][a:b])
        op = np.asarray(z["open"][a:b])
        hi = np.asarray(z["high"][a:b])
        lo = np.asarray(z["low"][a:b])
        cl = np.asarray(z["close"][a:b])
        vol = np.asarray(z["volume"][a:b]) if "volume" in z.files else np.zeros_like(cl)
        gain_24h_arr = np.zeros_like(cl, dtype=float)
        dp6h_arr = np.zeros_like(cl, dtype=float)
        if len(cl) > 1440:
            prev = cl[:-1440]
            gain_24h_arr[1440:] = np.where(prev > 0, (cl[1440:] / prev - 1.0) * 100.0, 0.0)
        if len(cl) > 360:
            prev = cl[:-360]
            dp6h_arr[360:] = np.where(prev > 0, (cl[360:] / prev - 1.0) * 100.0, 0.0)
        tr = (hi - lo) / np.maximum(cl, 1e-12)
        atr_arr = np.zeros_like(cl, dtype=float)
        if len(cl) >= 61:
            cs = np.concatenate(([0.0], np.cumsum(tr)))
            atr_arr[60:] = (cs[61:] - cs[:-60 - 1]) / 61.0
        vol_surge_arr = np.zeros_like(cl, dtype=float)
        if len(vol) >= 361:
            vcs = np.concatenate(([0.0], np.cumsum(vol)))
            recent = (vcs[61:] - vcs[:-60 - 1]) / 61.0
            baseline_start = np.arange(len(cl) - 60) - 360
            baseline_end = np.arange(len(cl) - 60) - 60
            valid = baseline_start >= 0
            base = np.zeros(len(cl) - 60, dtype=float)
            base[valid] = (vcs[baseline_end[valid]] - vcs[baseline_start[valid]]) / 300.0
            ratio = np.zeros_like(base)
            np.divide(recent, base, out=ratio, where=base > 0)
            vol_surge_arr[60:] = ratio
        rows: list[dict[str, Any]] = []
        for i in range(len(ts)):
            close = float(cl[i])
            rows.append(
                {
                    "datetime_utc": ms_iso(int(ts[i]) * 1000),
                    "timestamp_s": int(ts[i]),
                    "open": float(op[i]),
                    "high": float(hi[i]),
                    "low": float(lo[i]),
                    "close": close,
                    "volume": float(vol[i]),
                    "gain_24h_before": float(gain_24h_arr[i]),
                    "dp6h": float(dp6h_arr[i]),
                    "vol_surge_mult": float(vol_surge_arr[i]),
                    "atr_ratio": float(atr_arr[i]),
                }
            )
        return rows


def load_telegram_signals(path: Path, ttl_hours: float, limit: int) -> list[ExternalSignalRow]:
    out: list[ExternalSignalRow] = []
    for row in read_csv(path):
        side = norm_side(row.get("side"))
        if side not in {"LONG", "SHORT"}:
            continue
        if not row.get("dt_utc"):
            continue
        source = str(row.get("source_channel") or "telegram_standard").strip() or "telegram_standard"
        out.append(
            ExternalSignalRow(
                signal_id=str(row.get("message_idx") or len(out)),
                source=f"tg:{source}",
                source_type="telegram",
                symbol=market_symbol(row.get("symbol")),
                side=side,
                ts=parse_dt(row["dt_utc"]),
                max_hold_hours=ttl_hours,
            )
        )
        if limit and len(out) >= limit:
            break
    return sorted(out, key=lambda s: s.ts)


def load_binance_signals(report_dirs: Iterable[Path], ttl_hours: float, limit: int) -> list[ExternalSignalRow]:
    out: list[ExternalSignalRow] = []
    for report_dir in report_dirs:
        trade_path = report_dir / "plain_trades.csv"
        candle_dir = report_dir / "candles_1m_after_close"
        if not trade_path.exists():
            continue
        source = "binance:" + report_dir.name.replace("binance_copy_", "")
        for row in read_csv(trade_path):
            signal_id = str(row.get("id") or len(out))
            candles_path = next(iter(candle_dir.glob(f"{signal_id}_*.json")), None) if candle_dir.exists() else None
            candles = None
            note = ""
            if candles_path and candles_path.exists():
                raw = json.loads(candles_path.read_text(encoding="utf-8"))
                candles = [
                    {
                        "datetime_utc": ms_iso(int(c["t"])),
                        "timestamp_s": int(c["t"]) // 1000,
                        "open": float(c["open"]),
                        "high": float(c["high"]),
                        "low": float(c["low"]),
                        "close": float(c["close"]),
                        "volume": 0.0,
                        "gain_24h_before": 0.0,
                        "dp6h": 0.0,
                        "vol_surge_mult": 0.0,
                        "atr_ratio": 0.0,
                    }
                    for c in raw
                ]
                note = "Binance copy replay uses saved after-signal 1m candles only; no pre-signal warmup/indicators in cache."
            out.append(
                ExternalSignalRow(
                    signal_id=signal_id,
                    source=source,
                    source_type="binance_copy",
                    symbol=market_symbol(row.get("symbol")),
                    side=norm_side(row.get("side")),
                    ts=parse_dt(row.get("entry_utc")),
                    max_hold_hours=ttl_hours,
                    rows=candles,
                    coverage_note=note or "missing Binance candle cache",
                )
            )
            if limit and len(out) >= limit:
                return sorted(out, key=lambda s: s.ts)
    return sorted(out, key=lambda s: s.ts)


def side_sign(side: str) -> float:
    return 1.0 if side == "LONG" else -1.0


def unrealized(side: str, qty: float, avg_price: float, close: float) -> float:
    return side_sign(side) * qty * (close - avg_price)


def max_drawdown_pct(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            worst = min(worst, (v / peak - 1.0) * 100.0)
    return worst


def monthly_return_pct(times: list[datetime], values: list[float]) -> float:
    if len(times) < 2 or not values or values[0] <= 0:
        return 0.0
    days = max((times[-1] - times[0]).total_seconds() / 86400.0, 1e-12)
    return ((values[-1] / values[0]) - 1.0) * 100.0 * 30.0 / days


def mtm_delta_events(curves: list[dict[str, Any]]) -> dict[datetime, float]:
    by_signal: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in curves:
        by_signal[str(row.get("signal_id") or "")].append(row)
    events: dict[datetime, float] = defaultdict(float)
    for rows in by_signal.values():
        prev = 0.0
        for row in sorted(rows, key=lambda r: parse_dt(r["datetime_utc"])):
            cur = float(row["mtm_pnl"])
            events[parse_dt(row["datetime_utc"])] += cur - prev
            prev = cur
    return events


def tf_days(raw: Any) -> float:
    s = str(raw or "1m").strip().lower()
    if s == "d":
        return 1.0
    if s == "w":
        return 7.0
    if s.endswith("m"):
        return float(s[:-1]) / 1440.0
    if s.endswith("h"):
        return float(s[:-1]) / 24.0
    if s.endswith("d"):
        return float(s[:-1])
    if s.endswith("w"):
        return 7.0
    return 1.0 / 1440.0


def required_warmup_days(cfg: dict[str, Any], side: str) -> float:
    params = cfg.get("strategy_params_long" if side == "LONG" else "strategy_params_short") or {}
    if not bool(params.get("useTrendAdaptiveSizing", True)):
        return 0.0
    bars = float(params.get("trendMaLen", 20.0)) + float(params.get("trendSlopeBars", 3.0))
    return bars * tf_days(params.get("trendMaTf", "D"))


def warmup_coverage_rows(
    signals: list[ExternalSignalRow],
    rows_cache: dict[str, list[dict[str, Any]]],
    cfg: dict[str, Any],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sig in signals:
        rows = sig.rows if sig.rows is not None else rows_cache.get(sig.symbol)
        required = required_warmup_days(cfg, sig.side)
        before: list[dict[str, Any]] = []
        after: list[dict[str, Any]] = []
        if rows:
            sig_s = int(sig.ts.timestamp())
            before = [r for r in rows if int(r["timestamp_s"]) < sig_s]
            after = [r for r in rows if int(r["timestamp_s"]) >= sig_s]
        warm_days = 0.0
        earliest = ""
        latest = ""
        if before:
            earliest_dt = datetime.fromtimestamp(int(before[0]["timestamp_s"]), tz=timezone.utc)
            latest_dt = datetime.fromtimestamp(int(before[-1]["timestamp_s"]), tz=timezone.utc)
            earliest = iso(earliest_dt)
            latest = iso(latest_dt)
            warm_days = max((sig.ts - earliest_dt).total_seconds() / 86400.0, 0.0)
        reason = ""
        if not rows:
            reason = "missing_symbol_or_candles"
        elif warm_days + 1e-9 < required:
            reason = "insufficient_pre_signal_warmup"
        elif not after:
            reason = "missing_post_signal_window"
        out.append(
            {
                "signal_id": sig.signal_id,
                "source": sig.source,
                "source_type": sig.source_type,
                "symbol": sig.symbol,
                "side": sig.side,
                "signal_utc": iso(sig.ts),
                "required_warmup_days": f"{required:.4f}",
                "available_warmup_days": f"{warm_days:.4f}",
                "pre_signal_bars": len(before),
                "post_signal_bars": len(after),
                "warmup_earliest_utc": earliest,
                "warmup_latest_utc": latest,
                "coverage_ok": int(reason == ""),
                "reason": reason,
            }
        )
    return out


def replay_signal(
    sig: ExternalSignalRow,
    rows: list[dict[str, Any]],
    *,
    config_path: str,
    delegated_capital: float,
    fee_rate: float,
    slippage: float,
    warm_snapshot: Any | None = None,
    warm_meta: dict[str, Any] | None = None,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None]:
    if not rows:
        return None, [], {"signal_id": sig.signal_id, "source": sig.source, "symbol": sig.symbol, "reason": "no candle rows"}
    ts_s = int(sig.ts.timestamp())
    start_idx = next((i for i, row in enumerate(rows) if int(row["timestamp_s"]) >= ts_s), None)
    if start_idx is None:
        return None, [], {"signal_id": sig.signal_id, "source": sig.source, "symbol": sig.symbol, "reason": "signal after available candles"}
    end_s = ts_s + int(sig.max_hold_hours * 3600.0)
    cfg = load_one_leg_config(config_path, sig.side, delegated_capital)
    strat = make_strategy(cfg, sig.side)
    warm_meta = warm_meta or {"bars_loaded": 0, "ready": False}
    if warm_snapshot is not None and hasattr(strat, "restore_state_snapshot"):
        strat.restore_state_snapshot(sig.symbol, warm_snapshot)
    elif hasattr(strat, "warmup_history"):
        warm_meta = strat.warmup_history(sig.symbol, rows[:start_idx])
    entry_row = rows[start_idx]
    try:
        opened = open_external_signal(strat, sig.symbol, entry_row)
    except Exception as exc:
        return None, [], {
            "signal_id": sig.signal_id,
            "source": sig.source,
            "symbol": sig.symbol,
            "reason": f"V21 open rejected: {exc}",
            "warmup_bars": warm_meta.get("bars_loaded", 0),
            "warm_ready": warm_meta.get("ready", False),
        }
    state = opened["state"]
    qty = float(state["pos_size"])
    avg_price = float(state["avg_price"])
    realized = -qty * float(opened["entry_price"]) * (fee_rate + slippage)
    events = [
        {
            "datetime_utc": entry_row["datetime_utc"],
            "signal_id": sig.signal_id,
            "source": sig.source,
            "symbol": sig.symbol,
            "side": sig.side,
            "event": "OPEN",
            "price": opened["entry_price"],
            "qty": qty,
            "notional": qty * float(opened["entry_price"]),
            "realized_pnl": realized,
            "reason": opened.get("reason", ""),
            "num_fills": state.get("num_fills"),
        }
    ]
    curve: list[dict[str, Any]] = []
    exit_reason = "TTL/end_of_coverage"
    exit_price = float(entry_row["close"])
    exit_utc = entry_row["datetime_utc"]
    for row in rows[start_idx:]:
        now_s = int(row["timestamp_s"])
        close = float(row["close"])
        if now_s > end_s:
            break
        before = state
        before_qty = float(before.get("pos_size") or 0.0)
        before_avg = float(before.get("avg_price") or avg_price)
        result = manage_existing_position(strat, sig.symbol, row, state)
        state = result["state"]
        after_qty = float(state.get("pos_size") or 0.0)
        after_avg = float(state.get("avg_price") or before_avg)
        event = result.get("event")
        if event and event.get("action") == "DCA":
            add_qty = max(0.0, after_qty - before_qty)
            add_notional = add_qty * close
            realized -= add_notional * (fee_rate + slippage)
            events.append(
                {
                    "datetime_utc": row["datetime_utc"],
                    "signal_id": sig.signal_id,
                    "source": sig.source,
                    "symbol": sig.symbol,
                    "side": sig.side,
                    "event": "DCA",
                    "price": close,
                    "qty": add_qty,
                    "notional": add_notional,
                    "realized_pnl": realized,
                    "reason": event.get("reason", ""),
                    "num_fills": state.get("num_fills"),
                }
            )
        elif event and (str(event.get("action", "")).startswith("TP") or event.get("action") == "EXIT"):
            frac = float(event.get("qty_frac", 1.0) or 1.0)
            close_qty = before_qty * max(0.0, min(1.0, frac))
            entry_ref = float(result.get("pos_entry") or before_avg)
            exit_px = float(event.get("exit_price") or close)
            pnl = side_sign(sig.side) * close_qty * (exit_px - entry_ref)
            realized += pnl - close_qty * exit_px * (fee_rate + slippage)
            events.append(
                {
                    "datetime_utc": row["datetime_utc"],
                    "signal_id": sig.signal_id,
                    "source": sig.source,
                    "symbol": sig.symbol,
                    "side": sig.side,
                    "event": event.get("action"),
                    "price": exit_px,
                    "qty": close_qty,
                    "notional": close_qty * exit_px,
                    "realized_pnl": realized,
                    "reason": event.get("reason", ""),
                    "num_fills": state.get("num_fills"),
                }
            )
            if after_qty <= 1e-12:
                exit_reason = str(event.get("reason") or event.get("action"))
                exit_price = exit_px
                exit_utc = row["datetime_utc"]
                curve.append({"datetime_utc": row["datetime_utc"], "signal_id": sig.signal_id, "source": sig.source, "mtm_pnl": realized})
                break
        mtm = realized + (unrealized(sig.side, after_qty, after_avg, close) if after_qty > 0 and after_avg > 0 else 0.0)
        curve.append({"datetime_utc": row["datetime_utc"], "signal_id": sig.signal_id, "source": sig.source, "mtm_pnl": mtm})
        exit_price = close
        exit_utc = row["datetime_utc"]
    else:
        row = rows[-1]
    if state and float(state.get("pos_size") or 0.0) > 1e-12:
        close_qty = float(state["pos_size"])
        avg = float(state.get("avg_price") or avg_price)
        exit_px = float(exit_price)
        realized += side_sign(sig.side) * close_qty * (exit_px - avg) - close_qty * exit_px * (fee_rate + slippage)
        events.append(
            {
                "datetime_utc": exit_utc,
                "signal_id": sig.signal_id,
                "source": sig.source,
                "symbol": sig.symbol,
                "side": sig.side,
                "event": "FORCED_CLOSE",
                "price": exit_px,
                "qty": close_qty,
                "notional": close_qty * exit_px,
                "realized_pnl": realized,
                "reason": exit_reason,
                "num_fills": state.get("num_fills"),
            }
        )
        if curve:
            curve[-1]["mtm_pnl"] = realized
    trade = {
        "signal_id": sig.signal_id,
        "source": sig.source,
        "source_type": sig.source_type,
        "symbol": sig.symbol,
        "side": sig.side,
        "entry_utc": entry_row["datetime_utc"],
        "exit_utc": exit_utc,
        "entry_price": opened["entry_price"],
        "exit_price": exit_price,
        "pnl": realized,
        "return_on_delegated_pct": 100.0 * realized / max(delegated_capital, 1e-12),
        "max_fills": max(int(e.get("num_fills") or 0) for e in events),
        "events": len(events),
        "warmup_bars": warm_meta.get("bars_loaded", 0),
        "warm_ready": bool(warm_meta.get("ready")),
        "coverage_note": sig.coverage_note,
        "exit_reason": exit_reason,
    }
    return trade, curve, None


def source_summary(name: str, trades: list[dict[str, Any]], curves: list[dict[str, Any]], capital: float) -> dict[str, Any]:
    if not trades:
        return {"source": name, "trades": 0, "score": 0.0}
    events = mtm_delta_events(curves)
    times = sorted(events)
    values = []
    equity = capital
    for t in times:
        equity += events[t]
        values.append(equity)
    if not values:
        values = [capital + sum(float(t["pnl"]) for t in trades)]
    if not times:
        times = [parse_dt(trades[-1]["exit_utc"])]
    mdd = max_drawdown_pct(values)
    monthly = monthly_return_pct(times, values)
    pnl = sum(float(t["pnl"]) for t in trades)
    wins = sum(1 for t in trades if float(t["pnl"]) > 0)
    score = max(0.0, monthly) / max(abs(mdd), 0.25) * math.sqrt(min(len(trades), 100) / 100.0)
    return {
        "source": name,
        "trades": len(trades),
        "start_utc": iso(times[0]),
        "end_utc": iso(times[-1]),
        "days": max((times[-1] - times[0]).total_seconds() / 86400.0, 0.0),
        "pnl": pnl,
        "net_pct": 100.0 * pnl / max(capital, 1e-12),
        "monthly_pct": monthly,
        "mdd_pct": mdd,
        "win_pct": 100.0 * wins / len(trades),
        "score": score,
    }


def plot_source_curves(curves_by_source: dict[str, list[dict[str, Any]]], out_path: Path, source_capital: float) -> None:
    fig, ax = plt.subplots(figsize=(16, 9))
    for source, rows in sorted(curves_by_source.items()):
        events = mtm_delta_events(rows)
        times = sorted(events)
        if not times:
            continue
        pnl = 0.0
        values = []
        for t in times:
            pnl += events[t]
            values.append(100.0 * pnl / max(source_capital, 1e-12))
        ax.plot(times, values, linewidth=1.2, label=f"{source} ({values[-1]:+.2f}%)")
    ax.axhline(0, color="#222222", linewidth=0.8)
    ax.set_title("FULL V21 replay per-source MTM PnL")
    ax.set_ylabel("PnL, % on source replay capital")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_portfolio(times: list[datetime], values: list[float], out_path: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(16, 10), sharex=True, height_ratios=[3, 1])
    axes[0].plot(times, values, color="#111111", linewidth=2.0)
    axes[0].set_title("FULL V21 replay $500 ranked portfolio")
    axes[0].set_ylabel("Equity, USD")
    axes[0].grid(True, alpha=0.25)
    peak = values[0] if values else 0.0
    dd = []
    for value in values:
        peak = max(peak, value)
        dd.append((value / peak - 1.0) * 100.0 if peak else 0.0)
    axes[1].fill_between(times, dd, 0, color="#b33a3a", alpha=0.35)
    axes[1].set_ylabel("DD, %")
    axes[1].grid(True, alpha=0.25)
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def build_portfolio(summaries: list[dict[str, Any]], curves_by_source: dict[str, list[dict[str, Any]]], capital: float, source_capital: float) -> tuple[list[dict[str, Any]], list[datetime], list[float]]:
    score_sum = sum(float(s.get("score", 0.0)) for s in summaries)
    allocations = []
    events: dict[datetime, float] = defaultdict(float)
    for s in summaries:
        score = float(s.get("score", 0.0))
        alloc = capital * score / score_sum if score_sum > 0 and score > 0 else 0.0
        row = dict(s)
        row["allocation_usd"] = alloc
        row["allocation_pct"] = 100.0 * alloc / capital if capital else 0.0
        allocations.append(row)
        for t, delta_pnl in mtm_delta_events(curves_by_source.get(str(s["source"]), [])).items():
            events[t] += alloc * delta_pnl / max(source_capital, 1e-12)
    times = sorted(events)
    values = []
    equity = capital
    for t in times:
        equity += events[t]
        values.append(equity)
    return allocations, times, values


def build_warmup_cache(
    signals: list[ExternalSignalRow],
    rows_by_symbol: dict[str, list[dict[str, Any]]],
    *,
    config_path: str,
    delegated_capital: float,
) -> dict[tuple[str, str, str], tuple[Any, dict[str, Any]]]:
    """Return warm V21 state snapshots just before each signal timestamp."""
    cache: dict[tuple[str, str, str], tuple[Any, dict[str, Any]]] = {}
    grouped: dict[tuple[str, str], list[ExternalSignalRow]] = defaultdict(list)
    for sig in signals:
        if sig.rows is None and sig.symbol in rows_by_symbol:
            grouped[(sig.symbol, sig.side)].append(sig)
    for (symbol, side), group in grouped.items():
        rows = rows_by_symbol[symbol]
        if not rows:
            continue
        cfg = load_one_leg_config(config_path, side, delegated_capital)
        strat = make_strategy(cfg, side)
        cursor = 0
        ordered = sorted(group, key=lambda s: s.ts)
        for sig in ordered:
            ts_s = int(sig.ts.timestamp())
            while cursor < len(rows) and int(rows[cursor]["timestamp_s"]) < ts_s:
                row = rows[cursor]
                try:
                    st = strat._get_state(symbol)
                    close = float(row["close"])
                    strat._update_vol_state(st, close)
                    strat._update_trend(st, row.get("datetime_utc"), close)
                except Exception:
                    pass
                cursor += 1
            if hasattr(strat, "export_state_snapshot"):
                snapshot = strat.export_state_snapshot(symbol)
            else:
                st = strat._get_state(symbol)
                snapshot = {
                    "pos_size": float(st.pos_size or 0.0),
                    "pos_value_usdt": float(st.pos_value_usdt or 0.0),
                    "avg_price": st.avg_price,
                    "num_fills": int(st.num_fills or 0),
                    "last_fill_price": st.last_fill_price,
                    "next_level_price": st.next_level_price,
                    "lots": [[float(q), float(p)] for q, p in list(st.lots or [])],
                    "cycle_base_qty_coin": st.cycle_base_qty_coin,
                    "cycle_start_ts": st.cycle_start_ts,
                    "last_fill_ts": st.last_fill_ts,
                    "trailing_active": bool(st.trailing_active),
                    "trailing_ref": st.trailing_ref,
                    "tp_levels_done": list(st.tp_levels_done or []),
                }
            meta = {"bars_loaded": cursor, "ready": bool(getattr(strat, "is_warm_ready", lambda _s: False)(symbol))}
            cache[(sig.source, sig.signal_id, sig.side)] = (snapshot, meta)
        print(f"[warmup] {symbol} {side} signals={len(group)} bars={cursor}", flush=True)
    return cache


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-csv", type=Path, default=ROOT / "telegram_standard_bt_bundle/telegram_signal_standard_bt/telegram_signals_extracted.csv")
    ap.add_argument("--npz", type=Path, default=ROOT / "DB/telegram_signals_1m_event_windows_720h_bingx.npz")
    ap.add_argument("--npz-parts-dir", type=Path, default=ROOT / "DB/telegram_signals_1m_event_windows_720h_bingx_parts")
    ap.add_argument("--v21-config", default=str(ROOT / "obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml"))
    ap.add_argument("--out-dir", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--ttl-hours", type=float, default=720.0)
    ap.add_argument("--capital", type=float, default=500.0)
    ap.add_argument("--source-replay-capital", type=float, default=100.0)
    ap.add_argument("--base-order-pct", type=float, default=5.0)
    ap.add_argument("--telegram-limit", type=int, default=0)
    ap.add_argument("--binance-limit", type=int, default=0)
    ap.add_argument("--skip-binance", action="store_true")
    ap.add_argument("--coverage-only", action="store_true")
    args = ap.parse_args()

    args.out_dir.mkdir(parents=True, exist_ok=True)
    cfg = yaml.safe_load(Path(args.v21_config).read_text(encoding="utf-8")) or {}
    fee = float((cfg.get("portfolio") or {}).get("fee_rate", 0.0005))
    slip = float((cfg.get("portfolio") or {}).get("slippage_per_side", 0.0009380229915652661))

    store = NpzStore(args.npz, args.npz_parts_dir)
    signals = load_telegram_signals(args.signals_csv, args.ttl_hours, args.telegram_limit)
    if not args.skip_binance:
        signals.extend(load_binance_signals(DEFAULT_BINANCE_REPORTS, min(args.ttl_hours, 72.0), args.binance_limit))
    signals = sorted(signals, key=lambda s: s.ts)

    rows_cache: dict[str, list[dict[str, Any]]] = {}
    for sig in signals:
        if sig.rows is None and store.has_symbol(sig.symbol) and sig.symbol not in rows_cache:
            rows_cache[sig.symbol] = store.rows_for_symbol(sig.symbol)
    coverage_rows = warmup_coverage_rows(signals, rows_cache, cfg)
    write_csv(args.out_dir / "warmup_coverage.csv", coverage_rows)
    if args.coverage_only:
        missing_rows = [row for row in coverage_rows if str(row.get("coverage_ok")) != "1"]
        write_csv(args.out_dir / "coverage_missing.csv", missing_rows)
        lines = [
            "# FULL V21 External Signal Replay Coverage Check",
            "",
            "Coverage-only run. No PnL backtest was executed.",
            "",
            f"- Signals loaded: `{len(signals)}`",
            f"- Warmup coverage OK: `{sum(1 for row in coverage_rows if str(row.get('coverage_ok')) == '1')}/{len(coverage_rows)}`",
            f"- Missing/insufficient rows: `{len(missing_rows)}`",
            "",
            "Files:",
            "- `warmup_coverage.csv`",
            "- `coverage_missing.csv`",
        ]
        (args.out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"Wrote coverage manifest {args.out_dir}")
        return 0
    warmup_cache = build_warmup_cache(
        signals,
        rows_cache,
        config_path=args.v21_config,
        delegated_capital=args.source_replay_capital,
    )
    trades: list[dict[str, Any]] = []
    curves: list[dict[str, Any]] = []
    events: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    active_until_by_source_symbol: dict[tuple[str, str], datetime] = {}
    for i, sig in enumerate(signals, 1):
        active_key = (sig.source, sig.symbol)
        active_until = active_until_by_source_symbol.get(active_key)
        if active_until is not None and sig.ts < active_until:
            missing.append(
                {
                    "signal_id": sig.signal_id,
                    "source": sig.source,
                    "symbol": sig.symbol,
                    "side": sig.side,
                    "ts": iso(sig.ts),
                    "reason": "blocked_existing_source_symbol_leg",
                    "active_until_utc": iso(active_until),
                }
            )
            continue
        if sig.rows is not None:
            rows = sig.rows
        elif sig.symbol in rows_cache:
            rows = rows_cache[sig.symbol]
        else:
            missing.append({"signal_id": sig.signal_id, "source": sig.source, "symbol": sig.symbol, "side": sig.side, "ts": iso(sig.ts), "reason": "symbol missing from NPZ"})
            continue
        trade, curve, miss = replay_signal(
            sig,
            rows,
            config_path=args.v21_config,
            delegated_capital=args.source_replay_capital,
            fee_rate=fee,
            slippage=slip,
            warm_snapshot=(warmup_cache.get((sig.source, sig.signal_id, sig.side)) or (None, None))[0],
            warm_meta=(warmup_cache.get((sig.source, sig.signal_id, sig.side)) or (None, None))[1],
        )
        if miss:
            missing.append(miss)
            continue
        if trade:
            trades.append(trade)
            curves.extend(curve)
            active_until_by_source_symbol[active_key] = parse_dt(trade["exit_utc"])
        if i % 25 == 0:
            print(f"[replay] {i}/{len(signals)} trades={len(trades)} missing={len(missing)}", flush=True)
    write_csv(args.out_dir / "full_v21_trades.csv", trades)
    write_csv(args.out_dir / "full_v21_mtm_points.csv", curves)
    write_csv(args.out_dir / "coverage_missing.csv", missing)
    missing_reasons: dict[str, int] = defaultdict(int)
    missing_symbols: dict[str, int] = defaultdict(int)
    for row in missing:
        reason = str(row.get("reason") or "unknown")
        missing_reasons[reason] += 1
        if reason == "symbol missing from NPZ":
            missing_symbols[str(row.get("symbol") or "")] += 1

    curves_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    trades_by_source: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in curves:
        curves_by_source[row["source"]].append(row)
    for row in trades:
        trades_by_source[row["source"]].append(row)
    summaries = [source_summary(src, trades_by_source[src], curves_by_source[src], args.source_replay_capital) for src in sorted(trades_by_source)]
    allocations, p_times, p_values = build_portfolio(summaries, curves_by_source, args.capital, args.source_replay_capital)
    write_csv(args.out_dir / "source_summary.csv", summaries)
    write_csv(args.out_dir / "source_allocations.csv", allocations)
    write_csv(
        args.out_dir / "portfolio_500_mtm.csv",
        [{"datetime_utc": iso(t), "portfolio_mtm": v} for t, v in zip(p_times, p_values)],
    )
    if curves_by_source:
        plot_source_curves(curves_by_source, args.out_dir / "full_v21_per_source_mtm.png", args.source_replay_capital)
    if p_times:
        plot_portfolio(p_times, p_values, args.out_dir / "full_v21_portfolio_500_canvas.png")
    portfolio_mdd = max_drawdown_pct(p_values)
    portfolio_monthly = monthly_return_pct(p_times, p_values)
    portfolio_days = max((p_times[-1] - p_times[0]).total_seconds() / 86400.0, 0.0) if p_times else 0.0
    coverage_ok = sum(1 for row in coverage_rows if str(row.get("coverage_ok")) == "1")
    insufficient_warmup = sum(1 for row in coverage_rows if row.get("reason") == "insufficient_pre_signal_warmup")
    lines = [
        "# FULL V21 External Signal Replay",
        "",
        "This report is generated by the real V21 one-leg wrapper path, not the older simplified DCA3/DCA-overlay scripts.",
        "",
        f"- V21 config: `{Path(args.v21_config).as_posix()}`",
        f"- Strategy classes: `CryptomineLongPackAdaptiveEven` / `CryptomineShortPackAdaptiveEven`",
        f"- baseOrderPctEq input override: `{args.base_order_pct:.2f}`",
        "- useTrendAdaptiveSizing: kept enabled from V21 config",
        f"- Source replay capital: `${args.source_replay_capital:.2f}`",
        f"- Portfolio capital: `${args.capital:.2f}`",
        f"- Signals loaded/tested/missing: `{len(signals)} / {len(trades)} / {len(missing)}`",
        f"- Warmup coverage OK: `{coverage_ok}/{len(coverage_rows)}`; insufficient pre-signal warmup: `{insufficient_warmup}`",
        f"- Portfolio test window: `{portfolio_days:.2f}` calendar days",
        f"- Portfolio final MTM: `${(p_values[-1] if p_values else args.capital):.2f}`",
        f"- Portfolio extrapolated monthly return: `{portfolio_monthly:+.2f}%`",
        f"- Portfolio MTM max drawdown: `{portfolio_mdd:.2f}%`",
        "",
        "## Source Results",
        "",
        "| source | trades | days | net % | monthly % | MDD % | win % | score | allocation |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    alloc_by_source = {a["source"]: a for a in allocations}
    for s in summaries:
        a = alloc_by_source.get(s["source"], {})
        lines.append(
            f"| {s['source']} | {s['trades']} | {s.get('days', 0.0):.2f} | {s.get('net_pct', 0.0):+.2f}% | "
            f"{s.get('monthly_pct', 0.0):+.2f}% | {s.get('mdd_pct', 0.0):.2f}% | {s.get('win_pct', 0.0):.1f}% | "
            f"{s.get('score', 0.0):.4f} | ${float(a.get('allocation_usd', 0.0)):.2f} |"
        )
    lines.extend(
        [
            "",
            "## Coverage / Blockers",
            "",
        "- Telegram replay uses the available 1m NPZ windows and computes V21 regime helper fields from OHLCV.",
        "- Binance copy replay is partial when only saved post-signal candle caches are available; those rows have no pre-signal warmup, volume, or 24h/6h regime fields.",
        f"- Missing by reason: `{dict(sorted(missing_reasons.items()))}`",
        f"- Symbols missing from NPZ: `{dict(sorted(missing_symbols.items()))}`",
        "- `blocked_existing_source_symbol_leg` means the adapter enforced one active directional leg per source/symbol and skipped overlapping signals.",
        "- Any missing symbol/time coverage is written to `coverage_missing.csv`; this run must not be treated as a full universe backtest if that file is non-empty.",
            "- Warmup adequacy is written to `warmup_coverage.csv`; when insufficient, V21 still runs with available history but the report is marked as partial coverage.",
            "",
            "## Files",
            "",
            "- `full_v21_trades.csv`",
            "- `full_v21_mtm_points.csv`",
            "- `source_summary.csv`",
            "- `source_allocations.csv`",
            "- `portfolio_500_mtm.csv`",
            "- `full_v21_per_source_mtm.png`",
            "- `full_v21_portfolio_500_canvas.png`",
            "- `coverage_missing.csv`",
            "- `warmup_coverage.csv`",
        ]
    )
    (args.out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"Wrote {args.out_dir}")
    print(f"trades={len(trades)} missing={len(missing)} portfolio_final={(p_values[-1] if p_values else args.capital):.2f} mdd={portfolio_mdd:.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
