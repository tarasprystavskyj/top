#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Simple Telegram signal execution baseline on multi-symbol NPZ.

No DCA, no lower-level strategy. Opens at the first bar after Telegram signal,
then exits by Telegram SL/TP or optional channel_exit events.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import heapq
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np


def parse_dt(raw: Any) -> int:
    d = dt.datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
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


def load_signals(path: str) -> List[Dict[str, Any]]:
    out = []
    with Path(path).open("r", encoding="utf-8", newline="") as f:
        for i, r in enumerate(csv.DictReader(f)):
            side = norm_side(r.get("side"))
            lo = parse_float(r.get("entry_low") or r.get("entry_a"))
            hi = parse_float(r.get("entry_high") or r.get("entry_b"))
            if not side or lo is None or hi is None or not r.get("dt_utc"):
                continue
            out.append({
                "t": parse_dt(r["dt_utc"]),
                "base": base_symbol(r.get("symbol")),
                "side": side,
                "entry_low": min(lo, hi),
                "entry_high": max(lo, hi),
                "sl": parse_float(r.get("sl") or r.get("stop")),
                "tp": [parse_float(r.get("tp1")), parse_float(r.get("tp2")), parse_float(r.get("tp3"))],
                "source_id": str(r.get("telegram_message_id") or r.get("message_idx") or i),
                "raw_text": r.get("raw_text", ""),
            })
    return sorted(out, key=lambda x: (x["t"], x["source_id"]))


def load_channel_exit_events(path: str) -> List[Dict[str, Any]]:
    if not path or not Path(path).exists():
        return []
    p = Path(path)
    rows = []
    if p.suffix.lower() == ".jsonl":
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    else:
        with p.open("r", encoding="utf-8", newline="") as f:
            rows = [dict(r) for r in csv.DictReader(f)]
    out = []
    for i, r in enumerate(rows):
        et = str(r.get("event_type") or r.get("type") or r.get("kind") or "").lower()
        reason = str(r.get("reason") or "").lower()
        if et not in {"close", "exit", "channel_exit", "manual_exit"} and reason != "channel_exit":
            continue
        t_raw = r.get("dt_utc") or r.get("ts_utc") or r.get("telegram_message_date")
        if not t_raw:
            continue
        out.append({
            "t": parse_dt(t_raw),
            "base": base_symbol(r.get("base_symbol") or r.get("symbol")) if (r.get("base_symbol") or r.get("symbol")) else "",
            "source_id": str(r.get("telegram_message_id") or r.get("message_idx") or i),
            "raw_text": r.get("raw_text", ""),
        })
    return sorted(out, key=lambda x: (x["t"], x["source_id"]))


def load_npz(path: str, keep_bases=None) -> Dict[str, Dict[str, Any]]:
    """Load only needed symbol blocks and detach them from the full NPZ arrays.

    NpzFile does not support cheap per-symbol slicing. The old version kept
    views into full 5.7M-row arrays, so even a 68-signal subset retained the
    entire market cache in RAM. This version copies selected slices field by
    field and then releases each full array immediately.
    """
    keep = {base_symbol(x) for x in keep_bases} if keep_bases else None
    out: Dict[str, Dict[str, Any]] = {}
    with np.load(path, allow_pickle=False) as z:
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
            out[sym] = {"symbol": sym, "base": b}

        if not selected:
            return out

        for name, dtype in (("timestamp_s", np.int64), ("open", np.float64), ("high", np.float64), ("low", np.float64), ("close", np.float64)):
            arr = z[name]
            for _, sym, _, a, c in selected:
                out[sym][name] = np.asarray(arr[a:c], dtype=dtype).copy()
            del arr
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

def valid_tp(side: str, tp: Optional[float], entry_low: float, entry_high: float) -> bool:
    if tp is None:
        return False
    if side == "LONG":
        return tp > entry_high
    return tp < entry_low


def sl_hit(side: str, block: Dict[str, Any], i: int, sl: Optional[float]) -> bool:
    if sl is None:
        return False
    return bool(block["low"][i] <= sl) if side == "LONG" else bool(block["high"][i] >= sl)


def first_tp_hit(side: str, block: Dict[str, Any], i: int, sig: Dict[str, Any]) -> Optional[float]:
    for tp in sig["tp"]:
        if not valid_tp(side, tp, sig["entry_low"], sig["entry_high"]):
            continue
        if side == "LONG" and block["high"][i] >= tp:
            return float(tp)
        if side == "SHORT" and block["low"][i] <= tp:
            return float(tp)
    return None


def in_entry_zone(block: Dict[str, Any], i: int, sig: Dict[str, Any], mode: str) -> bool:
    if mode == "first_bar":
        return True
    if mode == "touch_zone":
        return bool(block["low"][i] <= sig["entry_high"] and block["high"][i] >= sig["entry_low"])
    return bool(sig["entry_low"] <= block["close"][i] <= sig["entry_high"])


def row_left_entry_zone(block: Dict[str, Any], i: int, sig: Dict[str, Any]) -> bool:
    return bool(block["low"][i] < sig["entry_low"] or block["high"][i] > sig["entry_high"])


def tp_levels_hit(side: str, block: Dict[str, Any], i: int, sig: Dict[str, Any]) -> List[tuple[int, float]]:
    out = []
    for idx, tp in enumerate(sig["tp"]):
        if not valid_tp(side, tp, sig["entry_low"], sig["entry_high"]):
            continue
        if side == "LONG" and block["high"][i] >= tp:
            out.append((idx, float(tp)))
        if side == "SHORT" and block["low"][i] <= tp:
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


def parse_tp_weight_config(raw: str) -> tuple[Optional[List[float]], str]:
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
    return max(0.0, float(tp) / entry - 1.0) if side == "LONG" else max(0.0, entry / float(tp) - 1.0)


def sl_loss_pct(side: str, entry: float, sl: Optional[float]) -> float:
    if sl is None or entry <= 0:
        return 0.0
    return max(0.0, 1.0 - float(sl) / entry) if side == "LONG" else max(0.0, float(sl) / entry - 1.0)


def dynamic_tp_weights(sig: Dict[str, Any], entry: float, preset: str) -> List[float]:
    probs = TP_REACH_PROBS[preset]
    returns = [tp_return_pct(sig["side"], entry, tp) for tp in sig["tp"]]
    initial_sl_risk = (1.0 - probs[0]) * sl_loss_pct(sig["side"], entry, sig["sl"])
    scores = [
        max(0.0, probs[0] * returns[0] - initial_sl_risk),
        max(0.0, probs[1] * returns[1]),
        max(0.0, probs[2] * returns[2]),
    ]
    s = sum(scores)
    return [x / s for x in scores] if s > 1e-12 else [1.0, 0.0, 0.0]


def current_close_frac_from_initial_weights(weights: List[float], tp_idx: int, exit_at_tp: int) -> float:
    if tp_idx + 1 >= exit_at_tp:
        return 1.0
    remaining_before = 1.0 - sum(weights[:tp_idx])
    if remaining_before <= 1e-12:
        return 1.0
    return max(0.0, min(1.0, weights[tp_idx] / remaining_before))


def next_meta_stop_after_tp(st: Dict[str, Any], tp_idx: int) -> Optional[float]:
    if tp_idx == 0:
        return float(st["entry_mark"])
    if tp_idx == 1:
        return st["sig"]["tp"][0]
    if tp_idx == 2:
        return st["sig"]["tp"][1]
    return st.get("meta_stop")


def exec_price(side: str, action: str, mark: float, slip: float) -> float:
    if side == "LONG":
        return mark * (1.0 + slip) if action == "open" else mark * (1.0 - slip)
    return mark * (1.0 - slip) if action == "open" else mark * (1.0 + slip)


def pnl_for(side: str, entry_exec: float, exit_exec: float, qty: float, fee_rate: float) -> float:
    gross = (exit_exec - entry_exec) * qty if side == "LONG" else (entry_exec - exit_exec) * qty
    fees = fee_rate * entry_exec * qty + fee_rate * exit_exec * qty
    return gross - fees


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    keys = []
    for r in rows:
        for k in r:
            if k not in keys:
                keys.append(k)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=keys)
        w.writeheader()
        w.writerows(rows)


def drawdown(vals: List[float]) -> float:
    peak = 0.0
    worst = 0.0
    for v in vals:
        peak = max(peak, float(v))
        if peak > 0:
            worst = min(worst, float(v) / peak - 1.0)
    return worst


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", required=True)
    ap.add_argument("--signals-csv", required=True)
    ap.add_argument("--events", default="")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--start-equity", type=float, default=1000.0)
    ap.add_argument("--notional", type=float, default=100.0)
    ap.add_argument("--fee-rate", type=float, default=0.0005)
    ap.add_argument("--slip-rate", type=float, default=0.00092387)
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
    ap.add_argument("--stop-first", action="store_true", default=True)
    ap.add_argument("--load-only-signal-symbols", action="store_true", help="Load only symbols referenced by signals/events. Faster/lower memory, but sparse MTM curve sampling can differ slightly.")
    args = ap.parse_args()

    signals = load_signals(args.signals_csv)
    exits = load_channel_exit_events(args.events)
    keep_bases = {s["base"] for s in signals}
    keep_bases.update(ev["base"] for ev in exits if ev.get("base"))
    market = load_npz(args.npz, keep_bases=keep_bases if args.load_only_signal_symbols else None)
    by_base = {b["base"]: sym for sym, b in market.items()}
    exits_by_base: Dict[str, List[Dict[str, Any]]] = {}
    global_exits: List[Dict[str, Any]] = []
    for ev in exits:
        if ev["base"]:
            exits_by_base.setdefault(ev["base"], []).append(ev)
        else:
            global_exits.append(ev)

    tp_weights_static, tp_weights_source = parse_tp_weight_config(args.tp_margin_weights)
    trades = []
    curve = []
    equity = float(args.start_equity)
    realized_closed = 0.0
    skipped_missing = skipped_stale = rejected = 0
    active: Dict[str, Dict[str, Any]] = {}
    active_order: List[str] = []
    waiting: List[Dict[str, Any]] = []
    sig_ptr = 0
    exit_ptr = 0

    def close_active(sym: str, block: Dict[str, Any], i: int, exit_mark: float, reason: str) -> None:
        nonlocal equity, realized_closed
        st = active.pop(sym)
        if sym in active_order:
            active_order.remove(sym)
        exit_exec = exec_price(st["side"], "close", exit_mark, args.slip_rate)
        qty = st["open_qty"]
        pnl = (exit_exec - st["entry_exec"]) * qty if st["side"] == "LONG" else (st["entry_exec"] - exit_exec) * qty
        pnl -= args.fee_rate * exit_exec * qty
        st["realized"] += pnl
        equity += st["realized"]
        realized_closed += st["realized"]
        trades.append({
            "symbol": sym,
            "side": st["side"].lower(),
            "entry_signal_id": st["source_id"],
            "entry_t": dt.datetime.fromtimestamp(st["entry_t"], tz=dt.timezone.utc).isoformat(),
            "exit_t": dt.datetime.fromtimestamp(int(block["timestamp_s"][i]), tz=dt.timezone.utc).isoformat(),
            "entry": st["entry_mark"],
            "entry_exec": st["entry_exec"],
            "exit": exit_mark,
            "exit_exec": exit_exec,
            "qty": st["qty_initial"],
            "open_qty_closed": qty,
            "pnl": st["realized"],
            "reason": reason,
        })
        curve.append({"t": dt.datetime.fromtimestamp(int(block["timestamp_s"][i]), tz=dt.timezone.utc).isoformat(), "equity": equity})

    def partial_close(sym: str, block: Dict[str, Any], i: int, exit_mark: float, frac: float) -> None:
        st = active[sym]
        qty = st["open_qty"] * max(0.0, min(1.0, frac))
        if qty <= 1e-12:
            return
        exit_exec = exec_price(st["side"], "close", exit_mark, args.slip_rate)
        pnl = (exit_exec - st["entry_exec"]) * qty if st["side"] == "LONG" else (st["entry_exec"] - exit_exec) * qty
        pnl -= args.fee_rate * exit_exec * qty
        st["realized"] += pnl
        st["open_qty"] -= qty

    for t, row_idx_by_sym in iter_market_timestamps(market):
        while exit_ptr < len(exits) and exits[exit_ptr]["t"] <= t:
            cev = exits[exit_ptr]
            exit_ptr += 1
            close_symbols = [sym for sym, st in active.items() if st["base"] == cev["base"]] if cev["base"] else ([active_order[-1]] if active_order else [])
            for sym in close_symbols:
                block = market[sym]
                i = int(np.searchsorted(block["timestamp_s"], t, side="left"))
                if i < len(block["timestamp_s"]):
                    close_active(sym, block, i, float(block["close"][i]), "channel_exit")

        for sym in list(active.keys()):
            block = market[sym]
            i = row_idx_by_sym.get(sym)
            if i is None:
                continue
            st = active[sym]
            active_sl = st.get("meta_stop") if st.get("meta_stop") is not None else st["sig"]["sl"]
            if args.stop_first and sl_hit(st["side"], block, i, active_sl):
                close_active(sym, block, i, float(active_sl), "telegram_meta_stop" if st.get("meta_stop") is not None else "telegram_sl")
                continue
            closed = False
            for tp_idx, tp in tp_levels_hit(st["side"], block, i, st["sig"]):
                if sym not in active:
                    closed = True
                    break
                st = active[sym]
                if st["tp_done"][tp_idx]:
                    continue
                st["tp_done"][tp_idx] = True
                if args.move_meta_stop_after_tp:
                    st["meta_stop"] = next_meta_stop_after_tp(st, tp_idx)
                tp_weights = tp_weights_static if tp_weights_static is not None else dynamic_tp_weights(st["sig"], st["entry_mark"], tp_weights_source)
                frac = current_close_frac_from_initial_weights(tp_weights, tp_idx, args.exit_at_tp)
                if frac >= 0.999 or tp_idx + 1 >= args.exit_at_tp:
                    close_active(sym, block, i, tp, f"telegram_tp{tp_idx + 1}")
                    closed = True
                    break
                partial_close(sym, block, i, tp, frac)
            if closed:
                continue
            active_sl = st.get("meta_stop") if st.get("meta_stop") is not None else st["sig"]["sl"]
            if (not args.stop_first) and sl_hit(st["side"], block, i, active_sl):
                close_active(sym, block, i, float(active_sl), "telegram_meta_stop" if st.get("meta_stop") is not None else "telegram_sl")

        while sig_ptr < len(signals) and signals[sig_ptr]["t"] <= t:
            waiting.append(signals[sig_ptr])
            sig_ptr += 1
        keep_waiting = []
        for sig in waiting:
            sym = by_base.get(sig["base"])
            if not sym:
                skipped_missing += 1
                continue
            block = market[sym]
            i = row_idx_by_sym.get(sym)
            if i is None:
                keep_waiting.append(sig)
                continue
            i0 = int(np.searchsorted(block["timestamp_s"], sig["t"], side="left"))
            if i0 >= len(block["timestamp_s"]):
                skipped_stale += 1
                continue
            if int(block["timestamp_s"][i0]) - int(sig["t"]) > args.max_open_lag_sec:
                skipped_stale += 1
                continue
            if t - sig["t"] > int(args.signal_ttl_hours * 3600):
                rejected += 1
                continue
            if args.reject_if_sl_before_entry and any(sl_hit(sig["side"], block, j, sig["sl"]) for j in range(i0, i + 1)):
                rejected += 1
                continue
            if t - sig["t"] > int(args.signal_hard_ttl_sec):
                stayed_in_zone = not any(row_left_entry_zone(block, j, sig) for j in range(i0, i + 1))
                if (not args.allow_late_if_not_left_zone) or (not stayed_in_zone):
                    rejected += 1
                    continue
            if sym in active:
                keep_waiting.append(sig)
                continue
            if not in_entry_zone(block, i, sig, args.entry_mode):
                keep_waiting.append(sig)
                continue
            if not args.allow_late_entry_after_tp1 and any(tp_levels_hit(sig["side"], block, j, sig) for j in range(i0, i + 1)):
                rejected += 1
                continue
            entry_mark = float(block["close"][i])
            entry_exec = exec_price(sig["side"], "open", entry_mark, args.slip_rate)
            qty = args.notional / max(entry_exec, 1e-12)
            active[sym] = {
                "sig": sig,
                "base": sig["base"],
                "side": sig["side"],
                "source_id": sig["source_id"],
                "entry_t": int(block["timestamp_s"][i]),
                "entry_mark": entry_mark,
                "entry_exec": entry_exec,
                "qty_initial": qty,
                "open_qty": qty,
                "realized": -args.fee_rate * entry_exec * qty,
                "tp_done": [False, False, False],
                "meta_stop": None,
            }
            active_order.append(sym)
        waiting = keep_waiting

    for sym in list(active.keys()):
        block = market[sym]
        i = len(block["timestamp_s"]) - 1
        close_active(sym, block, i, float(block["close"][i]), "eod")

    vals = [args.start_equity] + [float(r["equity"]) for r in curve]
    summary = {
        "mode": "telegram_simple_baseline",
        "signals_total": len(signals),
        "market_symbols": len(market),
        "events_path": args.events,
        "channel_exit_events": len(exits),
        "opened_signals": len(trades),
        "skipped_missing": skipped_missing,
        "skipped_stale": skipped_stale,
        "rejected": rejected,
        "trades": len(trades),
        "start_equity": args.start_equity,
        "end_equity": equity,
        "pnl_pct": equity / args.start_equity - 1.0 if args.start_equity else 0.0,
        "mdd_pct": drawdown(vals),
        "pnl_to_mdd": ((equity / args.start_equity - 1.0) / abs(drawdown(vals))) if drawdown(vals) < 0 else None,
        "notional": args.notional,
        "fee_rate": args.fee_rate,
        "slip_rate": args.slip_rate,
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
    }
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    write_csv(out / "telegram_simple_trades.csv", trades)
    write_csv(out / "telegram_simple_equity_curve.csv", curve)
    (out / "telegram_simple_summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(summary, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
