#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Static Telegram-triggered DCA MVP runner on a multi-symbol NPZ cache.

Research-only. Does not import or touch live/order execution code.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import heapq
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import yaml

OBW_ROOT = Path(__file__).resolve().parents[2]
if str(OBW_ROOT) not in sys.path:
    sys.path.insert(0, str(OBW_ROOT))

from strategies.cryptomine_pack_dual_full import (  # noqa: E402
    CryptomineLongPackAdaptiveEven,
    CryptomineShortPackAdaptiveEven,
)


@dataclass
class Event:
    t: int
    symbol: str
    base: str
    side: str
    entry_low: float
    entry_high: float
    tp1: Optional[float]
    tp2: Optional[float]
    tp3: Optional[float]
    sl: Optional[float]
    source_id: str
    raw_text: str = ""


@dataclass
class ChannelExit:
    t: int
    base: str
    source_id: str
    raw_text: str = ""


@dataclass
class Position:
    side: str
    entry: float
    qty: float


@dataclass
class LotBook:
    side: str
    fee_rate: float
    slip_rate: float
    lots: List[Tuple[float, float]] = field(default_factory=list)
    realized: float = 0.0

    def qty(self) -> float:
        return float(sum(q for q, _ in self.lots))

    def avg_entry(self) -> float:
        q = self.qty()
        return float(sum(q * px for q, px in self.lots) / q) if q > 1e-12 else 0.0

    def _exec_px(self, mark: float, action: str) -> float:
        if self.side == "LONG":
            return mark * (1.0 + self.slip_rate) if action == "open" else mark * (1.0 - self.slip_rate)
        return mark * (1.0 - self.slip_rate) if action == "open" else mark * (1.0 + self.slip_rate)

    def open_fill(self, qty: float, mark: float) -> float:
        px = self._exec_px(mark, "open")
        qty = float(qty)
        self.lots.append((qty, px))
        self.realized -= self.fee_rate * px * qty
        return px

    def close_fill(self, qty: float, mark: float) -> Tuple[float, float]:
        px = self._exec_px(mark, "close")
        rem = min(float(qty), self.qty())
        pnl = 0.0
        closed_qty = rem
        while rem > 1e-12 and self.lots:
            lot_qty, lot_px = self.lots[-1]
            take = min(lot_qty, rem)
            pnl += (px - lot_px) * take if self.side == "LONG" else (lot_px - px) * take
            lot_qty -= take
            rem -= take
            if lot_qty <= 1e-12:
                self.lots.pop()
            else:
                self.lots[-1] = (lot_qty, lot_px)
        pnl -= self.fee_rate * px * closed_qty
        self.realized += pnl
        return px, pnl

    def unrealized(self, mark: float) -> float:
        q = self.qty()
        if q <= 1e-12:
            return 0.0
        cost = sum(qty * px for qty, px in self.lots)
        return mark * q - cost if self.side == "LONG" else cost - mark * q


@dataclass
class Trade:
    event: Event
    market_symbol: str
    strategy: Any
    pos: Position
    book: LotBook
    entry_t: int
    entry_mark: float
    entry_i: int
    tp_done: List[bool] = field(default_factory=lambda: [False, False, False])
    meta_stop: Optional[float] = None
    meta_dca_done: int = 0


def parse_dt(v: Any) -> int:
    d = dt.datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.astimezone(dt.timezone.utc).timestamp())


def parse_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def base_symbol(sym: Any) -> str:
    s = str(sym or "").strip().upper().lstrip("#$")
    if "/" in s:
        return s.split("/", 1)[0]
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)]
    return s


def norm_side(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    if s in ("long", "buy"):
        return "LONG"
    if s in ("short", "sell"):
        return "SHORT"
    return None


def load_events(path: str) -> List[Event]:
    out: List[Event] = []
    with Path(path).open("r", newline="", encoding="utf-8") as f:
        for i, row in enumerate(csv.DictReader(f)):
            side = norm_side(row.get("side"))
            lo = parse_float(row.get("entry_low") or row.get("entry_a"))
            hi = parse_float(row.get("entry_high") or row.get("entry_b"))
            t_raw = row.get("dt_utc") or row.get("ts_utc")
            if not side or lo is None or hi is None or not t_raw:
                continue
            base = base_symbol(row.get("symbol"))
            out.append(Event(
                t=parse_dt(t_raw),
                symbol=str(row.get("symbol") or ""),
                base=base,
                side=side,
                entry_low=min(lo, hi),
                entry_high=max(lo, hi),
                tp1=parse_float(row.get("tp1")),
                tp2=parse_float(row.get("tp2")),
                tp3=parse_float(row.get("tp3")),
                sl=parse_float(row.get("sl") or row.get("stop")),
                source_id=str(row.get("telegram_message_id") or row.get("message_idx") or i),
                raw_text=str(row.get("raw_text") or ""),
            ))
    return sorted(out, key=lambda e: (e.t, e.source_id))


def load_channel_exit_events(path: str) -> List[ChannelExit]:
    if not path or not Path(path).exists():
        return []
    p = Path(path)
    if p.suffix.lower() == ".jsonl":
        rows = [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]
    else:
        with p.open("r", newline="", encoding="utf-8") as f:
            rows = [dict(r) for r in csv.DictReader(f)]
    out: List[ChannelExit] = []
    for i, row in enumerate(rows):
        ev_type = str(row.get("event_type") or row.get("type") or row.get("kind") or "").strip().lower()
        reason = str(row.get("reason") or "").strip().lower()
        if ev_type not in {"close", "exit", "channel_exit", "manual_exit"} and reason != "channel_exit":
            continue
        t_raw = row.get("dt_utc") or row.get("timestamp") or row.get("datetime") or row.get("ts_utc") or row.get("telegram_message_date")
        if not t_raw:
            continue
        base = base_symbol(row.get("base_symbol") or row.get("symbol")) if (row.get("base_symbol") or row.get("symbol")) else ""
        out.append(ChannelExit(
            t=parse_dt(t_raw),
            base=base,
            source_id=str(row.get("telegram_message_id") or row.get("message_id") or row.get("message_idx") or i),
            raw_text=str(row.get("raw_text") or ""),
        ))
    return sorted(out, key=lambda e: (e.t, e.source_id))


def load_npz(path: str, keep_bases=None) -> Dict[str, Dict[str, Any]]:
    """Load selected symbol blocks and release full NPZ arrays immediately.

    This avoids retaining views into full multi-million-row arrays. It is slower
    than pure views during loading but cuts steady-state memory sharply, which is
    what makes the runner usable in constrained sandboxes and worker loops.
    """
    keep = {base_symbol(x) for x in keep_bases} if keep_bases else None
    out: Dict[str, Dict[str, Any]] = {}
    with np.load(path, allow_pickle=False) as z:
        files = set(z.files)
        symbols = [str(s) for s in z["symbols"]]
        offsets = z["offsets"].astype(np.int64, copy=True)
        if len(offsets) == len(symbols):
            n_total = int(z["timestamp_s"].shape[0])
            offsets = np.concatenate([offsets, np.asarray([n_total], dtype=np.int64)])

        selected = []
        for idx, sym in enumerate(symbols):
            b = base_symbol(sym)
            if keep is not None and b not in keep:
                continue
            a, c = int(offsets[idx]), int(offsets[idx + 1])
            selected.append((idx, sym, b, a, c))
            out[sym] = {"symbol": sym, "base": b, "extras": {}}

        if not selected:
            return out

        # Required arrays.
        for name, dtype in (("timestamp_s", np.int64), ("close", np.float64)):
            arr = z[name]
            for _, sym, _, a, c in selected:
                out[sym][name] = np.asarray(arr[a:c], dtype=dtype).copy()
            del arr

        # Optional OHLCV arrays. Missing OHLC fields fall back to close after load.
        for name in ("open", "high", "low", "volume"):
            if name not in files:
                continue
            arr = z[name]
            for _, sym, _, a, c in selected:
                out[sym][name] = np.asarray(arr[a:c], dtype=np.float64).copy()
            del arr

        # Preserve same-shaped numeric extras if future caches add indicators.
        core = {"symbols", "offsets", "timestamp_s", "open", "high", "low", "close", "volume"}
        close_len = int(z["close"].shape[0])
        for name in sorted(files - core):
            arr = z[name]
            try:
                if getattr(arr, "shape", None) == (close_len,):
                    for _, sym, _, a, c in selected:
                        out[sym]["extras"][name] = np.asarray(arr[a:c], dtype=np.float64).copy()
            except Exception:
                pass
            del arr

    for block in out.values():
        close = block["close"]
        block.setdefault("open", close)
        block.setdefault("high", close)
        block.setdefault("low", close)
        if "volume" not in block:
            block["volume"] = np.zeros(len(close), dtype=np.float64)
    return out



def iter_market_timestamps(market: Dict[str, Dict[str, Any]]):
    """Yield chronological market timestamps without materializing ts_all or idx_by_sym."""
    heap = []
    for sym, block in market.items():
        ts = block.get("timestamp_s")
        if ts is not None and len(ts):
            heapq.heappush(heap, (int(ts[0]), sym, 0))
    while heap:
        t = int(heap[0][0])
        rows = {}
        while heap and int(heap[0][0]) == t:
            _, sym, i = heapq.heappop(heap)
            rows[sym] = i
            ni = i + 1
            ts = market[sym]["timestamp_s"]
            if ni < len(ts):
                heapq.heappush(heap, (int(ts[ni]), sym, ni))
        yield t, rows


def market_time_bounds(market: Dict[str, Dict[str, Any]]) -> tuple[int, int]:
    starts = [int(b["timestamp_s"][0]) for b in market.values() if len(b.get("timestamp_s", []))]
    ends = [int(b["timestamp_s"][-1]) for b in market.values() if len(b.get("timestamp_s", []))]
    return (min(starts), max(ends)) if starts and ends else (0, 0)

def row_at(block: Dict[str, Any], i: int) -> Dict[str, Any]:
    ts = int(block["timestamp_s"][i])
    close = float(block["close"][i])
    row = {
        "timestamp_s": ts,
        "ts_s": ts,
        "datetime_utc": dt.datetime.fromtimestamp(ts, tz=dt.timezone.utc),
        "open": float(block["open"][i]),
        "high": float(block["high"][i]),
        "low": float(block["low"][i]),
        "close": close,
        "volume": float(block["volume"][i]),
    }
    for k, arr in block["extras"].items():
        row[k] = float(arr[i])
    row.setdefault("quote_volume", row["volume"] * close)
    return row


def warm_rows(block: Dict[str, Any], i: int, n: int) -> List[Dict[str, Any]]:
    a = max(0, i - n)
    return [row_at(block, j) for j in range(a, i)]


def cfg_for_mvp(path: str, force_entry: bool) -> Dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    cfg["live"] = False
    if force_entry:
        for key in ("strategy_params_long", "strategy_params_short"):
            sp = cfg.get(key) or {}
            for k in (
                "entryTrendStrengthMin",
                "entryBlockCounterTrendGainAbs",
                "entryBlockCounterTrendDp6hAbs",
                "entryBlockVolSurgeMax",
                "entryBlockAtrRatioMax",
            ):
                sp[k] = 0.0
            sp["useLiveSyncStart"] = 0.0
            sp["useEvenBars"] = 0.0
            cfg[key] = sp
    return cfg


def tp_hit(side: str, row: Dict[str, Any], ev: Event) -> Optional[float]:
    tps = [ev.tp1, ev.tp2, ev.tp3]
    for tp in tps:
        if tp is None:
            continue
        if side == "LONG" and tp <= ev.entry_high:
            continue
        if side == "SHORT" and tp >= ev.entry_low:
            continue
        if side == "LONG" and row["high"] >= tp:
            return float(tp)
        if side == "SHORT" and row["low"] <= tp:
            return float(tp)
    return None


def sl_hit(side: str, row: Dict[str, Any], ev: Event) -> Optional[float]:
    if ev.sl is None:
        return None
    if side == "LONG" and row["low"] <= ev.sl:
        return float(ev.sl)
    if side == "SHORT" and row["high"] >= ev.sl:
        return float(ev.sl)
    return None


def in_entry_zone(row: Dict[str, Any], ev: Event, mode: str) -> bool:
    if mode == "first_bar":
        return True
    if mode == "touch_zone":
        return bool(row["low"] <= ev.entry_high and row["high"] >= ev.entry_low)
    return bool(ev.entry_low <= row["close"] <= ev.entry_high)


def row_left_entry_zone(row: Dict[str, Any], ev: Event) -> bool:
    return bool(row["low"] < ev.entry_low or row["high"] > ev.entry_high)


def tp_levels_hit(side: str, row: Dict[str, Any], ev: Event) -> List[Tuple[int, float]]:
    levels = [ev.tp1, ev.tp2, ev.tp3]
    out: List[Tuple[int, float]] = []
    for idx, tp in enumerate(levels):
        if tp is None:
            continue
        if side == "LONG" and tp <= ev.entry_high:
            continue
        if side == "SHORT" and tp >= ev.entry_low:
            continue
        if side == "LONG" and row["high"] >= tp:
            out.append((idx, float(tp)))
        if side == "SHORT" and row["low"] <= tp:
            out.append((idx, float(tp)))
    return out


TP_REACH_PROBS = {
    "edge_in_zone": [0.837, 0.639, 0.518],
    "edge_outside_zone": [0.842, 0.640, 0.489],
    "edge_in_zone_long": [0.815, 0.605, 0.479],
    "edge_in_zone_short": [0.894, 0.723, 0.617],
    "edge_outside_long": [0.838, 0.629, 0.486],
    "edge_outside_short": [0.853, 0.677, 0.500],
}


def parse_tp_weight_config(raw: str) -> Tuple[Optional[List[float]], str]:
    key = str(raw).strip()
    if key in TP_REACH_PROBS:
        return None, key
    vals = [float(x.strip()) for x in key.split(",") if x.strip()]
    if len(vals) != 3:
        raise ValueError("--tp-margin-weights must be a known edge_* preset or exactly 3 comma-separated numbers")
    vals = [max(0.0, x) for x in vals]
    s = sum(vals)
    if s <= 0:
        raise ValueError("--tp-margin-weights sum must be positive")
    return [x / s for x in vals], "custom"


def tp_return_pct(side: str, entry: float, tp: Optional[float]) -> float:
    if tp is None or entry <= 0:
        return 0.0
    if side == "LONG":
        return max(0.0, float(tp) / entry - 1.0)
    return max(0.0, entry / float(tp) - 1.0)


def sl_loss_pct(side: str, entry: float, sl: Optional[float]) -> float:
    if sl is None or entry <= 0:
        return 0.0
    if side == "LONG":
        return max(0.0, 1.0 - float(sl) / entry)
    return max(0.0, float(sl) / entry - 1.0)


def dynamic_tp_weights(ev: Event, entry: float, preset: str) -> List[float]:
    probs = TP_REACH_PROBS[preset]
    tps = [ev.tp1, ev.tp2, ev.tp3]
    returns = [tp_return_pct(ev.side, entry, tp) for tp in tps]
    initial_sl_risk = (1.0 - probs[0]) * sl_loss_pct(ev.side, entry, ev.sl)
    # TP1 carries initial SL risk. After TP1 the channel guidance moves stop
    # to break-even, so later buckets are penalized only by failure to reach
    # that next TP, not by the original SL distance.
    scores = [
        max(0.0, probs[0] * returns[0] - initial_sl_risk),
        max(0.0, probs[1] * returns[1]),
        max(0.0, probs[2] * returns[2]),
    ]
    s = sum(scores)
    if s <= 1e-12:
        return [1.0, 0.0, 0.0]
    return [x / s for x in scores]


def current_close_frac_from_initial_weights(weights: List[float], tp_idx: int, exit_at_tp: int) -> float:
    if tp_idx + 1 >= exit_at_tp:
        return 1.0
    remaining_before = 1.0 - sum(weights[:tp_idx])
    if remaining_before <= 1e-12:
        return 1.0
    return max(0.0, min(1.0, weights[tp_idx] / remaining_before))


def next_meta_stop_after_tp(tr: Trade, tp_idx: int) -> Optional[float]:
    if tp_idx == 0:
        return tr.entry_mark
    if tp_idx == 1:
        return tr.event.tp1
    if tp_idx == 2:
        return tr.event.tp2
    return tr.meta_stop


def meta_dca_level(tr: Trade, level_no: int, total_levels: int) -> Optional[float]:
    if total_levels <= 0:
        return None
    frac = float(level_no) / float(total_levels)
    if tr.event.side == "LONG":
        adverse = tr.event.entry_low
        if adverse >= tr.entry_mark:
            return None
        return tr.entry_mark + (adverse - tr.entry_mark) * frac
    adverse = tr.event.entry_high
    if adverse <= tr.entry_mark:
        return None
    return tr.entry_mark + (adverse - tr.entry_mark) * frac


def meta_dca_touched(tr: Trade, row: Dict[str, Any], level: float) -> bool:
    if tr.event.side == "LONG":
        return bool(row["low"] <= level)
    return bool(row["high"] >= level)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys: List[str] = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def dd(values: List[float]) -> float:
    peak = -math.inf
    worst = 0.0
    for v in values:
        peak = max(peak, float(v))
        if peak > 0:
            worst = min(worst, float(v) / peak - 1.0)
    return worst


def run(args: argparse.Namespace) -> Dict[str, Any]:
    cfg = cfg_for_mvp(args.cfg, args.force_telegram_entry)
    events = load_events(args.signals_csv)
    channel_exits = load_channel_exit_events(args.events)
    keep_bases = {e.base for e in events}
    keep_bases.update(e.base for e in channel_exits if e.base)
    market = load_npz(args.npz, keep_bases=keep_bases if args.load_only_signal_symbols else None)
    by_base = {b["base"]: sym for sym, b in market.items()}
    missing_bases = sorted({e.base for e in events} - set(by_base))
    market_start, market_end = market_time_bounds(market)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    active: Dict[str, Trade] = {}
    active_order: List[str] = []
    pending_events = list(events)
    signals_before_market = sum(1 for e in events if e.t < market_start)
    signals_inside_market = sum(1 for e in events if market_start <= e.t <= market_end)
    signals_after_market = sum(1 for e in events if e.t > market_end)
    trades: List[Dict[str, Any]] = []
    fills: List[Dict[str, Any]] = []
    curve: List[Dict[str, Any]] = []
    opened = rejected = skipped_missing = skipped_busy = 0
    unprocessed_after_market = 0
    channel_exit_used = 0
    tp_weights_static, tp_weights_source = parse_tp_weight_config(args.tp_margin_weights)
    waiting_events: List[Event] = []

    start_equity = float(args.start_equity)
    fee_rate = float(args.fee_rate)
    slip_rate = float(args.slip_rate)
    prices: Dict[str, float] = {}

    def equity_now() -> Tuple[float, float, float]:
        realized = sum(t.book.realized for t in active.values()) + sum(float(t["pnl"]) for t in trades)
        unreal = 0.0
        for sym, tr in active.items():
            if sym in prices:
                unreal += tr.book.unrealized(prices[sym])
        return start_equity + realized + unreal, realized, unreal

    def close_trade(sym: str, mark: float, reason: str, t: int, i: int) -> None:
        tr = active.pop(sym)
        if sym in active_order:
            active_order.remove(sym)
        qty = tr.book.qty()
        exec_px, last_pnl = tr.book.close_fill(qty, mark)
        total_pnl = tr.book.realized
        if hasattr(tr.strategy, "sync_after_external_fill"):
            tr.strategy.sync_after_external_fill(sym, 0.0, exec_px, fill_price=exec_px, event="close")
        trades.append({
            "symbol": sym,
            "side": tr.event.side.lower(),
            "entry_signal_id": tr.event.source_id,
            "entry_t": dt.datetime.fromtimestamp(tr.entry_t, tz=dt.timezone.utc).isoformat(),
            "exit_t": dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).isoformat(),
            "entry": tr.entry_mark,
            "exit": mark,
            "exit_exec": exec_px,
            "qty": qty,
            "pnl": total_pnl,
            "final_close_pnl": last_pnl,
            "reason": reason,
            "entry_i": tr.entry_i,
            "exit_i": i,
        })

    def partial_close_trade(sym: str, mark: float, qty_frac_current: float, reason: str, t: int, i: int) -> None:
        tr = active[sym]
        qty_before = tr.book.qty()
        qty_close = qty_before * max(0.0, min(1.0, float(qty_frac_current)))
        if qty_close <= 1e-12:
            return
        exec_px, pnl = tr.book.close_fill(qty_close, mark)
        tr.pos.qty = tr.book.qty()
        tr.pos.entry = tr.book.avg_entry() or tr.pos.entry
        fills.append({
            "t": dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).isoformat(),
            "symbol": sym,
            "side": tr.event.side.lower(),
            "action": reason,
            "mark": mark,
            "exec": exec_px,
            "qty": qty_close,
            "pnl": pnl,
            "remaining_qty": tr.pos.qty,
        })
        if hasattr(tr.strategy, "sync_after_external_fill"):
            tr.strategy.sync_after_external_fill(sym, tr.pos.qty, tr.pos.entry, fill_price=exec_px, event="partial")

    event_ptr = 0
    exit_ptr = 0
    for step, (t, row_idx_by_sym) in enumerate(iter_market_timestamps(market)):
        # Channel close messages are meta-exits. Symbol-specific exits close
        # that active symbol; no-symbol exits close only the latest still-open
        # Telegram position because the channel message does not identify more.
        while exit_ptr < len(channel_exits) and channel_exits[exit_ptr].t <= t:
            cev = channel_exits[exit_ptr]
            exit_ptr += 1
            if cev.base:
                close_symbols = [sym for sym, tr in active.items() if tr.event.base == cev.base]
            else:
                close_symbols = [active_order[-1]] if active_order else []
            for sym in close_symbols:
                block = market[sym]
                i = int(np.searchsorted(block["timestamp_s"], t, side="left"))
                if i >= len(block["timestamp_s"]):
                    continue
                row = row_at(block, i)
                prices[sym] = row["close"]
                close_trade(sym, row["close"], "channel_exit", t, i)
                channel_exit_used += 1

        # Manage open trades first; a meta TP/SL disables the DCA layer for that bar.
        for sym in list(active.keys()):
            block = market[sym]
            i = row_idx_by_sym.get(sym)
            if i is None:
                continue
            row = row_at(block, i)
            prices[sym] = row["close"]
            tr = active[sym]
            sl_event = Event(
                t=tr.event.t, symbol=tr.event.symbol, base=tr.event.base, side=tr.event.side,
                entry_low=tr.event.entry_low, entry_high=tr.event.entry_high,
                tp1=tr.event.tp1, tp2=tr.event.tp2, tp3=tr.event.tp3,
                sl=tr.meta_stop if tr.meta_stop is not None else tr.event.sl,
                source_id=tr.event.source_id, raw_text=tr.event.raw_text,
            )
            sl = sl_hit(tr.event.side, row, sl_event)
            if sl is not None:
                close_trade(sym, sl, "telegram_meta_stop" if tr.meta_stop is not None else "telegram_sl", t, i)
                continue
            closed_by_tp = False
            meta_touched = False
            for tp_idx, tp in tp_levels_hit(tr.event.side, row, tr.event):
                if sym not in active:
                    closed_by_tp = True
                    break
                tr = active[sym]
                if tr.tp_done[tp_idx]:
                    continue
                meta_touched = True
                tr.tp_done[tp_idx] = True
                if args.move_meta_stop_after_tp:
                    tr.meta_stop = next_meta_stop_after_tp(tr, tp_idx)
                tp_weights = tp_weights_static if tp_weights_static is not None else dynamic_tp_weights(tr.event, tr.entry_mark, tp_weights_source)
                frac = current_close_frac_from_initial_weights(tp_weights, tp_idx, int(args.exit_at_tp))
                if frac >= 0.999 or tp_idx + 1 >= int(args.exit_at_tp):
                    close_trade(sym, tp, f"telegram_tp{tp_idx + 1}", t, i)
                    closed_by_tp = True
                    break
                partial_close_trade(sym, tp, frac, f"telegram_tp{tp_idx + 1}_partial", t, i)
            if closed_by_tp:
                continue
            if meta_touched:
                continue

            while tr.meta_dca_done < args.meta_dca_adds:
                next_no = tr.meta_dca_done + 1
                lvl = meta_dca_level(tr, next_no, args.meta_dca_adds)
                if lvl is None or not meta_dca_touched(tr, row, lvl):
                    break
                add_notional = float(args.initial_notional) * max(0.0, args.meta_dca_total_notional_mult - 1.0) / max(1, args.meta_dca_adds)
                if add_notional <= 0:
                    break
                qty_before = tr.pos.qty
                qty_add = add_notional / max(lvl, 1e-12)
                exec_px = tr.book.open_fill(qty_add, lvl)
                tr.pos.qty = tr.book.qty()
                tr.pos.entry = tr.book.avg_entry() or tr.pos.entry
                tr.meta_dca_done += 1
                if hasattr(tr.strategy, "sync_after_external_fill"):
                    tr.strategy.sync_after_external_fill(sym, tr.pos.qty, tr.pos.entry, fill_price=exec_px, delta_qty=tr.pos.qty - qty_before, event="dca")
                fills.append({
                    "t": row["datetime_utc"].isoformat(),
                    "symbol": sym,
                    "side": tr.event.side.lower(),
                    "action": "meta_dca",
                    "level_no": next_no,
                    "mark": lvl,
                    "exec": exec_px,
                    "qty": qty_add,
                })

            before_qty = tr.pos.qty
            ex = tr.strategy.manage_position(sym, row, tr.pos, ctx={"telegram_event": tr.event})
            after_qty = float(tr.pos.qty)
            if after_qty > before_qty + 1e-12:
                dq = after_qty - before_qty
                exec_px = tr.book.open_fill(dq, row["close"])
                tr.pos.entry = tr.book.avg_entry()
                if hasattr(tr.strategy, "sync_after_external_fill"):
                    tr.strategy.sync_after_external_fill(sym, tr.pos.qty, tr.pos.entry, fill_price=exec_px, delta_qty=dq, event="dca")
                fills.append({"t": row["datetime_utc"].isoformat(), "symbol": sym, "side": tr.event.side.lower(), "action": "dca", "mark": row["close"], "exec": exec_px, "qty": dq})
            if ex:
                if args.ignore_lower_exits:
                    continue
                action = str(getattr(ex, "action", "") or "").upper()
                mark = float(getattr(ex, "exit_price", row["close"]) or row["close"])
                reason = str(getattr(ex, "reason", action) or action)
                if action == "TP_PARTIAL":
                    frac = max(0.0, min(1.0, float(getattr(ex, "qty_frac", 0.0) or 0.0)))
                    qty_close = before_qty * frac
                    exec_px, pnl = tr.book.close_fill(qty_close, mark)
                    tr.pos.qty = tr.book.qty()
                    tr.pos.entry = tr.book.avg_entry() or tr.pos.entry
                    fills.append({"t": row["datetime_utc"].isoformat(), "symbol": sym, "side": tr.event.side.lower(), "action": "tp_partial", "mark": mark, "exec": exec_px, "qty": qty_close, "pnl": pnl, "reason": reason})
                elif action in ("TP", "SL", "EXIT"):
                    close_trade(sym, mark, reason, t, i)

        while event_ptr < len(pending_events) and pending_events[event_ptr].t <= t:
            waiting_events.append(pending_events[event_ptr])
            event_ptr += 1

        # Open Telegram-triggered entries once price reaches the configured entry condition.
        remaining_waiting: List[Event] = []
        for ev in waiting_events:
            sym = by_base.get(ev.base)
            if not sym:
                skipped_missing += 1
                continue
            block = market[sym]
            i = row_idx_by_sym.get(sym)
            if i is None:
                remaining_waiting.append(ev)
                continue
            if t - ev.t > int(args.signal_ttl_hours * 3600):
                rejected += 1
                continue
            i0 = int(np.searchsorted(block["timestamp_s"], ev.t, side="left"))
            if i >= len(block["timestamp_s"]):
                rejected += 1
                continue
            if (not args.allow_stale_signals) and int(block["timestamp_s"][i0]) - ev.t > args.max_open_lag_sec:
                rejected += 1
                continue
            if args.reject_if_sl_before_entry:
                if any(sl_hit(ev.side, row_at(block, j), ev) is not None for j in range(i0, i + 1)):
                    rejected += 1
                    continue
            if t - ev.t > int(args.signal_hard_ttl_sec):
                stayed_in_zone = not any(row_left_entry_zone(row_at(block, j), ev) for j in range(i0, i + 1))
                if (not args.allow_late_if_not_left_zone) or (not stayed_in_zone):
                    rejected += 1
                    continue
            if sym in active or len(active) >= args.max_concurrent:
                remaining_waiting.append(ev)
                continue
            row = row_at(block, i)
            if not in_entry_zone(row, ev, args.entry_mode):
                remaining_waiting.append(ev)
                continue
            pre_tp_hit = False
            if not args.allow_late_entry_after_tp1:
                for j in range(i0, i + 1):
                    rj = row_at(block, j)
                    if tp_levels_hit(ev.side, rj, ev):
                        pre_tp_hit = True
                        break
            if pre_tp_hit:
                rejected += 1
                continue
            entry_t = int(block["timestamp_s"][i])
            prices[sym] = row["close"]
            strat = CryptomineLongPackAdaptiveEven(cfg) if ev.side == "LONG" else CryptomineShortPackAdaptiveEven(cfg)
            strat.warmup_history(sym, warm_rows(block, i, args.warmup_bars))
            book = LotBook(ev.side, fee_rate, slip_rate)
            entry_exec_est = book._exec_px(row["close"], "open")
            qty = float(args.initial_notional) / max(entry_exec_est, 1e-12)
            exec_px = book.open_fill(qty, row["close"])
            pos = Position(ev.side, exec_px, qty)
            if hasattr(strat, "sync_after_external_fill"):
                strat.sync_after_external_fill(sym, qty, exec_px, fill_price=exec_px, event="open")
            active[sym] = Trade(ev, sym, strat, pos, book, entry_t, row["close"], i)
            active_order.append(sym)
            opened += 1
            fills.append({"t": row["datetime_utc"].isoformat(), "symbol": sym, "side": ev.side.lower(), "action": "open", "mark": row["close"], "exec": exec_px, "qty": qty, "source_id": ev.source_id})
        waiting_events = remaining_waiting

        if step % args.curve_every == 0:
            eq, realized, unreal = equity_now()
            curve.append({"t": dt.datetime.fromtimestamp(t, tz=dt.timezone.utc).isoformat(), "equity_mtm": eq, "realized_pnl": realized, "unrealized_pnl": unreal, "open_positions": len(active)})

    for sym in list(active.keys()):
        block = market[sym]
        i = len(block["timestamp_s"]) - 1
        row = row_at(block, i)
        close_trade(sym, row["close"], "eod", int(row["timestamp_s"]), i)
    unprocessed_after_market = max(0, len(pending_events) - event_ptr) + len(waiting_events)

    values = [start_equity] + [float(r["equity_mtm"]) for r in curve]
    realized_total = sum(float(t["pnl"]) for t in trades)
    mdd = dd(values)
    summary = {
        "mode": "telegram_dca_mvp_chrono",
        "signals_total": len(events),
        "market_symbols": len(market),
        "market_start": dt.datetime.fromtimestamp(market_start, tz=dt.timezone.utc).isoformat() if market_start else "",
        "market_end": dt.datetime.fromtimestamp(market_end, tz=dt.timezone.utc).isoformat() if market_end else "",
        "signals_before_market": signals_before_market,
        "signals_inside_market": signals_inside_market,
        "signals_after_market": signals_after_market,
        "missing_bases": missing_bases,
        "opened_signals": opened,
        "rejected_signals": rejected,
        "skipped_missing": skipped_missing,
        "skipped_busy": skipped_busy,
        "unprocessed_after_market": unprocessed_after_market,
        "trades": len(trades),
        "events_path": args.events,
        "channel_exit_events": len(channel_exits),
        "channel_exit_used": channel_exit_used,
        "start_equity": start_equity,
        "end_equity_mtm": start_equity + realized_total,
        "mtm_pnl_pct": realized_total / start_equity if start_equity else 0.0,
        "mtm_mdd_pct": mdd,
        "mtm_to_mdd": (realized_total / start_equity) / abs(mdd) if mdd < 0 else None,
        "fee_rate": fee_rate,
        "slip_rate": slip_rate,
        "max_open_lag_sec": args.max_open_lag_sec,
        "allow_stale_signals": bool(args.allow_stale_signals),
        "entry_mode": args.entry_mode,
        "signal_ttl_hours": args.signal_ttl_hours,
        "signal_hard_ttl_sec": args.signal_hard_ttl_sec,
        "allow_late_if_not_left_zone": bool(args.allow_late_if_not_left_zone),
        "reject_if_sl_before_entry": bool(args.reject_if_sl_before_entry),
        "allow_late_entry_after_tp1": bool(args.allow_late_entry_after_tp1),
        "exit_at_tp": args.exit_at_tp,
        "tp_margin_weights": tp_weights_static,
        "tp_margin_weights_source": tp_weights_source,
        "move_meta_stop_after_tp": bool(args.move_meta_stop_after_tp),
        "ignore_lower_exits": bool(args.ignore_lower_exits),
        "initial_notional": args.initial_notional,
        "meta_dca_adds": args.meta_dca_adds,
        "meta_dca_total_notional_mult": args.meta_dca_total_notional_mult,
    }
    write_csv(out_dir / "telegram_dca_trades.csv", trades)
    write_csv(out_dir / "telegram_dca_fills.csv", fills)
    write_csv(out_dir / "telegram_dca_equity_curve.csv", curve)
    (out_dir / "telegram_dca_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default="DB/telegram_signals_3m_7200b_bingx.npz")
    ap.add_argument("--signals-csv", default="telegram_standard_bt_bundle/telegram_signal_standard_bt/telegram_signals_extracted.csv")
    ap.add_argument("--events", default="")
    ap.add_argument("--cfg", default="obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml")
    ap.add_argument("--out-dir", default="obw_platform/meta_strategies/telegram_dca_mvp/reports/full")
    ap.add_argument("--start-equity", type=float, default=1000.0)
    ap.add_argument("--initial-notional", type=float, default=100.0)
    ap.add_argument("--meta-dca-adds", type=int, default=0)
    ap.add_argument("--meta-dca-total-notional-mult", type=float, default=1.0)
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    ap.add_argument("--slip-rate", type=float, default=0.00092387)
    ap.add_argument("--warmup-bars", type=int, default=3000)
    ap.add_argument("--max-concurrent", type=int, default=8)
    ap.add_argument("--max-open-lag-sec", type=int, default=3600)
    ap.add_argument("--entry-mode", choices=["first_bar", "close_in_zone", "touch_zone"], default="close_in_zone")
    ap.add_argument("--signal-ttl-hours", type=float, default=72.0)
    ap.add_argument("--signal-hard-ttl-sec", type=int, default=3600)
    ap.add_argument("--allow-late-if-not-left-zone", action="store_true", default=True)
    ap.add_argument("--no-allow-late-if-not-left-zone", dest="allow_late_if_not_left_zone", action="store_false")
    ap.add_argument("--reject-if-sl-before-entry", action="store_true", default=True)
    ap.add_argument("--allow-entry-after-sl", dest="reject_if_sl_before_entry", action="store_false")
    ap.add_argument("--allow-late-entry-after-tp1", action="store_true")
    ap.add_argument("--exit-at-tp", type=int, choices=[1, 2, 3], default=2)
    ap.add_argument("--tp-margin-weights", default="edge_in_zone")
    ap.add_argument("--move-meta-stop-after-tp", action="store_true", default=True)
    ap.add_argument("--no-move-meta-stop-after-tp", dest="move_meta_stop_after_tp", action="store_false")
    ap.add_argument("--ignore-lower-exits", action="store_true")
    ap.add_argument("--allow-stale-signals", action="store_true")
    ap.add_argument("--force-telegram-entry", action="store_true", default=True)
    ap.add_argument("--no-force-telegram-entry", dest="force_telegram_entry", action="store_false")
    ap.add_argument("--curve-every", type=int, default=50)
    ap.add_argument("--load-only-signal-symbols", action="store_true", help="Load only symbols referenced by signals/events. Faster/lower memory, but sparse MTM curve sampling can differ slightly.")
    args = ap.parse_args()
    summary = run(args)
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
