# -*- coding: utf-8 -*-
"""Telegram signal replay strategy for standard universe backtester.

Contract compatible with:
  obw_platform/backtester_core_speed3_veto_universe_4_mtm_unrealized_v5.py

It replays already extracted Telegram signals from CSV:
  dt_utc,symbol,side,entry_low,entry_high,tp1,tp2,tp3,sl

Entry model:
  - a signal is eligible after its dt_utc and before dt_utc + signal_ttl_hours;
  - opens only when close is inside [entry_low, entry_high] by default;
  - one consumed signal cannot be opened twice;
  - symbol names in CSV may be plain base symbols (SUI) or futures symbols.

Exit model:
  - close by TP1/TP2 partials and TP3 final, or SL;
  - uses high/low touch if available;
  - if SL and TP are both touched in one bar, SL is chosen first by default.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, List, Mapping, Optional, Literal
import csv
import os

Side = Literal["LONG", "SHORT"]
ExitAction = Literal["HOLD", "TP", "SL", "EXIT", "TP_PARTIAL"]

@dataclass
class Sig:
    side: Side
    take_profit: float
    stop_price: float
    confidence: float = 1.0
    size: Optional[float] = None
    reason: Optional[str] = None

    @property
    def tp(self): return self.take_profit
    @property
    def tp_price(self): return self.take_profit
    @property
    def sl(self): return self.stop_price
    @property
    def sl_price(self): return self.stop_price

@dataclass
class ExitSig:
    action: ExitAction
    exit_price: Optional[float] = None
    reason: Optional[str] = None
    qty_frac: Optional[float] = None


def _f(x: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if x is None or x == "":
            return default
        return float(str(x).replace(",", "."))
    except Exception:
        return default


def _parse_dt(s: str) -> datetime:
    s = str(s).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def _row_dt(row: Mapping[str, Any]) -> datetime:
    return _parse_dt(str(row.get("datetime_utc")))


def _base(sym: str) -> str:
    s = str(sym).upper().strip()
    if "/" in s:
        return s.split("/", 1)[0]
    return s


class TelegramSignalReplayStrategy:
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.cfg = dict(cfg or {})
        sp = self.cfg.get("strategy_params", {}) or {}
        self.signals_csv = str(sp.get("signals_csv") or self.cfg.get("signals_csv") or "telegram_signals_extracted.csv")
        self.signal_ttl_hours = float(sp.get("signal_ttl_hours", 72.0))
        self.entry_mode = str(sp.get("entry_mode", "close_in_zone")).lower()
        self.stop_first_on_same_bar = bool(sp.get("stop_first_on_same_bar", True))
        self.tp1_qty_frac = float(sp.get("tp1_qty_frac", 1.0 / 3.0))
        self.tp2_qty_frac = float(sp.get("tp2_qty_frac", 0.5))  # fraction of remaining
        self.allow_late_entry_after_tp1 = bool(sp.get("allow_late_entry_after_tp1", False))
        self.allowed_bases = set(str(x).upper() for x in (sp.get("allowed_bases", []) or []))

        if not os.path.exists(self.signals_csv):
            raise FileNotFoundError(self.signals_csv)

        self.signals: List[Dict[str, Any]] = []
        with open(self.signals_csv, "r", encoding="utf-8") as f:
            for i, r in enumerate(csv.DictReader(f)):
                side_raw = str(r.get("side", "")).upper()
                if side_raw not in ("LONG", "SHORT"):
                    side_raw = side_raw.upper()
                base = _base(r.get("symbol", ""))
                if self.allowed_bases and base not in self.allowed_bases:
                    continue
                dt = _parse_dt(r.get("dt_utc") or r.get("ts_utc") or r.get("date") or "1970-01-01T00:00:00+00:00")
                sig = {
                    "id": str(r.get("message_idx", i)),
                    "dt": dt,
                    "expires": dt + timedelta(hours=self.signal_ttl_hours),
                    "base": base,
                    "side": side_raw,
                    "entry_low": _f(r.get("entry_low"), None),
                    "entry_high": _f(r.get("entry_high"), None),
                    "tp1": _f(r.get("tp1"), None),
                    "tp2": _f(r.get("tp2"), None),
                    "tp3": _f(r.get("tp3"), None),
                    "sl": _f(r.get("sl") or r.get("stop"), None),
                    "raw": r,
                }
                if None in (sig["entry_low"], sig["entry_high"], sig["tp1"], sig["tp2"], sig["tp3"], sig["sl"]):
                    continue
                if sig["side"] not in ("LONG", "SHORT"):
                    continue
                self.signals.append(sig)
        self.signals.sort(key=lambda x: x["dt"])

        self.consumed: set[str] = set()
        self.pending_by_symbol: Dict[str, Dict[str, Any]] = {}
        self.open_signal_by_symbol: Dict[str, Dict[str, Any]] = {}
        self.stage_by_symbol: Dict[str, int] = {}

    @staticmethod
    def required_db_columns(cfg: Mapping[str, Any]) -> List[str]:
        return ["open", "high", "low"]

    def _eligible_signal_for_row(self, symbol: str, row: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
        base = _base(symbol)
        t = _row_dt(row)
        close = _f(row.get("close"), None)
        high = _f(row.get("high"), close)
        low = _f(row.get("low"), close)
        if close is None:
            return None

        for sig in self.signals:
            if sig["id"] in self.consumed:
                continue
            if sig["base"] != base:
                continue
            if t < sig["dt"] or t > sig["expires"]:
                continue

            lo = float(sig["entry_low"]); hi = float(sig["entry_high"])
            if self.entry_mode == "touch_zone":
                in_zone = bool(high >= lo and low <= hi)
            else:
                in_zone = bool(lo <= close <= hi)
            if not in_zone:
                continue

            if not self.allow_late_entry_after_tp1:
                # Avoid entering after the move has already reached TP1 before we got a fill.
                if sig["side"] == "LONG" and high >= float(sig["tp1"]):
                    continue
                if sig["side"] == "SHORT" and low <= float(sig["tp1"]):
                    continue
            return sig
        return None

    def universe(self, t: Any, md_map: Mapping[str, Mapping[str, Any]]) -> List[str]:
        out: List[str] = []
        self.pending_by_symbol = {}
        for sym, row in md_map.items():
            sig = self._eligible_signal_for_row(sym, row)
            if sig:
                out.append(sym)
                self.pending_by_symbol[sym] = sig
        return out

    def rank(self, t: Any, md_map: Mapping[str, Mapping[str, Any]], universe_syms: List[str]) -> List[str]:
        return sorted(universe_syms, key=lambda s: self.pending_by_symbol.get(s, {}).get("dt", datetime.max.replace(tzinfo=timezone.utc)))

    def entry_signal(self, bar_close: bool, symbol: str, row: Mapping[str, Any], ctx: Optional[Mapping[str, Any]] = None) -> Optional[Sig]:
        sig = self.pending_by_symbol.get(symbol) or self._eligible_signal_for_row(symbol, row)
        if not sig:
            return None
        self.consumed.add(sig["id"])
        self.open_signal_by_symbol[symbol] = sig
        self.stage_by_symbol[symbol] = 0
        side: Side = "LONG" if sig["side"] == "LONG" else "SHORT"
        return Sig(side=side, take_profit=float(sig["tp3"]), stop_price=float(sig["sl"]), reason=f"telegram_signal_{sig['id']}")

    def manage_position(self, symbol: str, row: Mapping[str, Any], pos: Any, ctx: Optional[Mapping[str, Any]] = None) -> ExitSig:
        sig = self.open_signal_by_symbol.get(symbol)
        if not sig:
            return ExitSig("HOLD")
        side = str(getattr(pos, "side", sig["side"])).upper()
        high = _f(row.get("high"), _f(row.get("close"), 0.0)) or 0.0
        low = _f(row.get("low"), _f(row.get("close"), 0.0)) or 0.0
        sl = float(sig["sl"])
        stage = int(self.stage_by_symbol.get(symbol, 0))
        tps = [float(sig["tp1"]), float(sig["tp2"]), float(sig["tp3"])]

        def sl_hit() -> bool:
            return low <= sl if side == "LONG" else high >= sl
        def tp_hit(tp: float) -> bool:
            return high >= tp if side == "LONG" else low <= tp

        # Conservative ambiguity handling.
        if self.stop_first_on_same_bar and sl_hit():
            self.open_signal_by_symbol.pop(symbol, None)
            self.stage_by_symbol.pop(symbol, None)
            return ExitSig("SL", exit_price=sl, reason="sl")

        if stage <= 0 and tp_hit(tps[0]):
            self.stage_by_symbol[symbol] = 1
            return ExitSig("TP_PARTIAL", exit_price=tps[0], reason="tp1", qty_frac=self.tp1_qty_frac)
        if stage <= 1 and tp_hit(tps[1]):
            self.stage_by_symbol[symbol] = 2
            return ExitSig("TP_PARTIAL", exit_price=tps[1], reason="tp2", qty_frac=self.tp2_qty_frac)
        if stage <= 2 and tp_hit(tps[2]):
            self.open_signal_by_symbol.pop(symbol, None)
            self.stage_by_symbol.pop(symbol, None)
            return ExitSig("TP", exit_price=tps[2], reason="tp3")

        if not self.stop_first_on_same_bar and sl_hit():
            self.open_signal_by_symbol.pop(symbol, None)
            self.stage_by_symbol.pop(symbol, None)
            return ExitSig("SL", exit_price=sl, reason="sl")

        return ExitSig("HOLD")
