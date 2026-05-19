#!/usr/bin/env python3
"""Compare plain Telegram signal execution against V21 DCA execution.

This runner intentionally keeps the model narrow:
- entries come from Telegram signal rows;
- exits are Telegram TP1/TP2/TP3 and SL;
- DCA sizing and ladder steps come from a V21 YAML config;
- price data comes from a local SQLite price_indicators table.

It is a research/meta-strategy tool, not a live runner.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml


def parse_dt(raw: Any) -> datetime:
    s = str(raw or "").strip()
    if not s:
        raise ValueError("empty datetime")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def fmt_dt(d: datetime) -> str:
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")


def f(raw: Any, default: float = math.nan) -> float:
    try:
        if raw in ("", None):
            return default
        return float(str(raw).replace(",", "."))
    except Exception:
        return default


def base_symbol(symbol: str) -> str:
    return str(symbol or "").upper().split("/", 1)[0].strip()


def norm_side(raw: Any) -> str:
    side = str(raw or "").upper().strip()
    if side in {"BUY", "LONG"}:
        return "LONG"
    if side in {"SELL", "SHORT"}:
        return "SHORT"
    return side


@dataclass(frozen=True)
class Signal:
    idx: int
    dt: datetime
    expires: datetime
    symbol: str
    base: str
    source_channel: str
    side: str
    entry_low: float
    entry_high: float
    sl: float
    tp1: float
    tp2: float
    tp3: float


@dataclass
class Position:
    signal: Signal
    symbol: str
    side: str
    entry_time: datetime
    avg_entry: float
    qty: float
    stage: int = 0
    dca_filled: int = 0
    dca_levels: List[float] = field(default_factory=list)
    dca_adds: List[float] = field(default_factory=list)
    notional_opened: float = 0.0


def read_signals(
    path: str,
    ttl_hours: float,
    side_filter: str,
    source_channel_filter: str = "",
) -> List[Signal]:
    out: List[Signal] = []
    source_channel_filter = source_channel_filter.strip().lower()
    with open(path, "r", encoding="utf-8", newline="") as fp:
        for i, row in enumerate(csv.DictReader(fp)):
            try:
                dt = parse_dt(row.get("dt_utc") or row.get("ts_utc") or row.get("telegram_message_date"))
            except Exception:
                continue
            side = norm_side(row.get("side"))
            if side not in {"LONG", "SHORT"}:
                continue
            if side_filter != "both" and side != side_filter.upper():
                continue
            entry_low = f(row.get("entry_low"))
            entry_high = f(row.get("entry_high"))
            sl = f(row.get("sl") or row.get("stop"))
            tp1 = f(row.get("tp1"))
            tp2 = f(row.get("tp2"))
            tp3 = f(row.get("tp3"))
            if any(math.isnan(x) for x in (entry_low, entry_high, sl, tp1, tp2, tp3)):
                continue
            lo, hi = sorted((entry_low, entry_high))
            symbol = str(row.get("symbol") or "").strip()
            if not symbol:
                continue
            source_channel = str(row.get("source_channel") or row.get("channel") or "").strip()
            if source_channel_filter and source_channel.lower() != source_channel_filter:
                continue
            out.append(
                Signal(
                    idx=i,
                    dt=dt,
                    expires=dt + timedelta(hours=ttl_hours),
                    symbol=symbol,
                    base=base_symbol(symbol),
                    source_channel=source_channel,
                    side=side,
                    entry_low=lo,
                    entry_high=hi,
                    sl=sl,
                    tp1=tp1,
                    tp2=tp2,
                    tp3=tp3,
                )
            )
    return sorted(out, key=lambda s: s.dt)


def load_price_rows(db_path: str) -> List[Dict[str, Any]]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        rows = [dict(r) for r in con.execute("select * from price_indicators order by datetime_utc, symbol")]
    finally:
        con.close()
    for row in rows:
        row["_dt"] = parse_dt(row["datetime_utc"])
        row["_base"] = base_symbol(row["symbol"])
        for key in ("open", "high", "low", "close"):
            row[key] = f(row[key])
    return rows


def load_v21_policy(path: str, max_dca_count: int) -> Dict[str, Any]:
    cfg = yaml.safe_load(Path(path).read_text(encoding="utf-8"))

    def side_policy(side: str) -> Dict[str, Any]:
        params = cfg[f"strategy_params_{side.lower()}"]
        if side == "LONG":
            step_keys = ["drop1", "drop2", "drop3", "drop4", "drop5"]
            linear_key = "linearDropPercent"
        else:
            step_keys = ["rise1", "rise2", "rise3", "rise4", "rise5"]
            linear_key = "linearRisePercent"
        steps = [float(params[k]) for k in step_keys if k in params]
        if max_dca_count > len(steps):
            fallback = float(params.get(linear_key, steps[-1] if steps else 0.0))
            steps.extend([fallback] * (max_dca_count - len(steps)))
        base = max(
            float(params.get("minOrderUSDT", 0.0)),
            float(params["equityForSizingUSDT"]) * float(params["baseOrderPctEq"]) / 100.0,
        )
        mults = [
            float(params.get("mult2", 1.0)),
            float(params.get("mult3", 1.0)),
            float(params.get("mult4", 1.0)),
            float(params.get("mult5", 1.0)),
        ]
        if max_dca_count > len(mults):
            mults.extend([1.0] * (max_dca_count - len(mults)))
        min_order = float(params.get("minOrderUSDT", 0.0))
        return {
            "base_notional": base,
            "steps": steps[:max_dca_count],
            "adds": [max(min_order, base * mult) for mult in mults[:max_dca_count]],
        }

    portfolio = cfg.get("portfolio", {}) or {}
    return {
        "config_path": str(path),
        "rollback_label": str(cfg.get("rollback_label", "")),
        "fee": float(portfolio.get("fee_rate", 0.0005)),
        "slippage": float(portfolio.get("slippage_per_side", 0.0)),
        "max_notional_frac": float(portfolio.get("max_notional_frac", 1.0)),
        "long": side_policy("LONG"),
        "short": side_policy("SHORT"),
    }


def v21_dca_plan(sig: Signal, entry_px: float, policy: Dict[str, Any], count: int) -> Tuple[List[float], List[float]]:
    side_policy = policy["long"] if sig.side == "LONG" else policy["short"]
    levels: List[float] = []
    last = entry_px
    for step in side_policy["steps"][:count]:
        if sig.side == "LONG":
            last *= 1.0 - step / 100.0
        else:
            last *= 1.0 + step / 100.0
        levels.append(last)
    return levels, side_policy["adds"][:count]


def tp_hit(side: str, row: Dict[str, Any], tp: float) -> bool:
    return row["high"] >= tp if side == "LONG" else row["low"] <= tp


def sl_hit(side: str, row: Dict[str, Any], sl: float) -> bool:
    return row["low"] <= sl if side == "LONG" else row["high"] >= sl


def dca_hit(side: str, row: Dict[str, Any], level: float) -> bool:
    return row["low"] <= level if side == "LONG" else row["high"] >= level


def ret_for(side: str, entry: float, exit_px: float) -> float:
    if entry <= 0:
        return 0.0
    return (exit_px - entry) / entry if side == "LONG" else (entry - exit_px) / entry


def unrealized(pos: Position, close: float, fee: float, slippage: float) -> float:
    gross = ret_for(pos.side, pos.avg_entry, close)
    net = gross - 2 * fee - 2 * slippage
    return net * pos.avg_entry * pos.qty


def close_partial(
    pos: Position,
    t: datetime,
    px: float,
    qty_close: float,
    action: str,
    reason: str,
    fee: float,
    slippage: float,
    out: List[Dict[str, Any]],
) -> float:
    notional_entry = pos.avg_entry * qty_close
    pnl = (ret_for(pos.side, pos.avg_entry, px) - 2 * fee - 2 * slippage) * notional_entry
    out.append(
        {
            "symbol": pos.symbol,
            "base": pos.signal.base,
            "side": pos.side,
            "entry_time": fmt_dt(pos.entry_time),
            "exit_time": fmt_dt(t),
            "avg_entry": pos.avg_entry,
            "exit": px,
            "tp": pos.signal.tp3,
            "sl": pos.signal.sl,
            "action": action,
            "reason": reason,
            "notional": notional_entry,
            "fees_paid": 2 * fee * notional_entry,
            "realized_pnl": pnl,
            "dca_filled": pos.dca_filled,
            "notional_opened": pos.notional_opened,
            "signal_idx": pos.signal.idx,
        }
    )
    return pnl


def max_drawdown(values: Iterable[float]) -> float:
    peak = -1e18
    dd = 0.0
    for raw in values:
        v = float(raw)
        peak = max(peak, v)
        dd = min(dd, (v - peak) / max(peak, 1e-12))
    return dd


def simulate(
    rows: List[Dict[str, Any]],
    signals: List[Signal],
    *,
    initial_equity: float,
    dca_count: int,
    policy: Dict[str, Any],
    tp1_frac: float,
    tp2_frac: float,
) -> Tuple[Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    by_time: Dict[datetime, List[Dict[str, Any]]] = {}
    for row in rows:
        by_time.setdefault(row["_dt"], []).append(row)

    fee = float(policy["fee"])
    slippage = float(policy["slippage"])
    max_notional_frac = float(policy["max_notional_frac"])
    consumed: set[int] = set()
    opened: set[int] = set()
    positions: Dict[str, Position] = {}
    realized = initial_equity
    pnl_pos = 0.0
    pnl_neg = 0.0
    fees_paid = 0.0
    trade_rows: List[Dict[str, Any]] = []
    equity_rows: List[Dict[str, Any]] = []

    for t in sorted(by_time):
        bucket = by_time[t]
        md = {r["symbol"]: r for r in bucket}
        by_base: Dict[str, List[Dict[str, Any]]] = {}
        for row in bucket:
            by_base.setdefault(row["_base"], []).append(row)

        for sym, pos in list(positions.items()):
            row = md.get(sym)
            if not row:
                continue
            sig = pos.signal
            if sl_hit(pos.side, row, sig.sl):
                qty_close = pos.qty
                pnl = close_partial(pos, t, sig.sl, qty_close, "SL", "sl", fee, slippage, trade_rows)
                realized += pnl
                fees_paid += 2 * fee * pos.avg_entry * qty_close
                pnl_pos += max(0.0, pnl)
                pnl_neg += min(0.0, pnl)
                del positions[sym]
                continue

            for stage, tp, frac, reason in (
                (0, sig.tp1, tp1_frac, "tp1"),
                (1, sig.tp2, tp2_frac, "tp2"),
                (2, sig.tp3, 1.0, "tp3"),
            ):
                if sym not in positions or pos.stage > stage:
                    continue
                if not tp_hit(pos.side, row, tp):
                    continue
                qty_close = pos.qty * frac
                action = "TP" if reason == "tp3" else "TP_PARTIAL"
                pnl = close_partial(pos, t, tp, qty_close, action, reason, fee, slippage, trade_rows)
                realized += pnl
                fees_paid += 2 * fee * pos.avg_entry * qty_close
                pnl_pos += max(0.0, pnl)
                pnl_neg += min(0.0, pnl)
                pos.qty -= qty_close
                pos.stage = stage + 1
                if reason == "tp3" or pos.qty <= 1e-12:
                    del positions[sym]
                    break

            if sym not in positions:
                continue
            while pos.dca_filled < len(pos.dca_levels):
                level = pos.dca_levels[pos.dca_filled]
                if not dca_hit(pos.side, row, level):
                    break
                add_notional = pos.dca_adds[pos.dca_filled]
                add_qty = add_notional / max(level, 1e-12)
                old_notional = pos.avg_entry * pos.qty
                new_notional = old_notional + add_notional
                pos.qty += add_qty
                pos.avg_entry = new_notional / pos.qty
                pos.notional_opened += add_notional
                pos.dca_filled += 1

        unreal = 0.0
        open_notional = 0.0
        for sym, pos in positions.items():
            row = md.get(sym)
            if row:
                unreal += unrealized(pos, row["close"], fee, slippage)
            open_notional += pos.avg_entry * pos.qty
        equity_mtm = realized + unreal

        for sig in signals:
            if sig.idx in consumed:
                continue
            if t < sig.dt or t > sig.expires:
                continue
            if any(p.signal.base == sig.base for p in positions.values()):
                continue
            side_policy = policy["long"] if sig.side == "LONG" else policy["short"]
            entry_notional = float(side_policy["base_notional"])
            if open_notional + entry_notional > max_notional_frac * max(equity_mtm, 0.0):
                break
            for row in by_base.get(sig.base, []):
                close = float(row["close"])
                if sig.entry_low <= close <= sig.entry_high:
                    qty = entry_notional / max(close, 1e-12)
                    levels, adds = v21_dca_plan(sig, close, policy, dca_count)
                    positions[row["symbol"]] = Position(
                        signal=sig,
                        symbol=row["symbol"],
                        side=sig.side,
                        entry_time=t,
                        avg_entry=close,
                        qty=qty,
                        dca_levels=levels,
                        dca_adds=adds,
                        notional_opened=entry_notional,
                    )
                    consumed.add(sig.idx)
                    opened.add(sig.idx)
                    open_notional += entry_notional
                    break

        unreal = 0.0
        open_notional = 0.0
        for sym, pos in positions.items():
            row = md.get(sym)
            if row:
                unreal += unrealized(pos, row["close"], fee, slippage)
            open_notional += pos.avg_entry * pos.qty
        equity_rows.append(
            {
                "datetime_utc": fmt_dt(t),
                "equity_realized": realized,
                "equity_mtm": realized + unreal,
                "unrealized": unreal,
                "open_notional": open_notional,
                "open_positions": len(positions),
            }
        )

    pf = pnl_pos / abs(pnl_neg) if pnl_neg < 0 else 0.0
    wins = sum(1 for row in trade_rows if float(row["realized_pnl"]) > 0)
    equity_end_mtm = float(equity_rows[-1]["equity_mtm"]) if equity_rows else realized
    summary = {
        "equity_end_realized": realized,
        "equity_end_mtm": equity_end_mtm,
        "net_pnl_realized": realized - initial_equity,
        "net_pnl_mtm": equity_end_mtm - initial_equity,
        "trades": len(trade_rows),
        "opened_signals": len(opened),
        "input_signals": len(signals),
        "pf": pf,
        "fees_realized": fees_paid,
        "win_rate": 100.0 * wins / max(1, len(trade_rows)),
        "max_dd_mtm": 100.0 * max_drawdown(float(row["equity_mtm"]) for row in equity_rows),
        "dca_count": dca_count,
        "fee": fee,
        "slippage_per_side": slippage,
        "max_notional_frac": policy["max_notional_frac"],
        "v21_config": policy["config_path"],
        "rollback_label": policy["rollback_label"],
        "long_base_notional": policy["long"]["base_notional"],
        "short_base_notional": policy["short"]["base_notional"],
    }
    return summary, trade_rows, equity_rows, symbol_summary(trade_rows)


def symbol_summary(trades: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    stats: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for row in trades:
        key = (str(row["base"]), str(row["side"]))
        st = stats.setdefault(
            key,
            {
                "base": key[0],
                "side": key[1],
                "trade_rows": 0,
                "realized_pnl": 0.0,
                "fees_paid": 0.0,
                "max_dca_filled": 0,
                "avg_dca_filled_sum": 0.0,
            },
        )
        st["trade_rows"] += 1
        st["realized_pnl"] += float(row["realized_pnl"])
        st["fees_paid"] += float(row["fees_paid"])
        st["max_dca_filled"] = max(int(st["max_dca_filled"]), int(row["dca_filled"]))
        st["avg_dca_filled_sum"] += float(row["dca_filled"])
    out = []
    for st in stats.values():
        n = max(1, int(st["trade_rows"]))
        st["avg_dca_filled"] = st.pop("avg_dca_filled_sum") / n
        out.append(st)
    return sorted(out, key=lambda r: float(r["realized_pnl"]), reverse=True)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fp:
        if not rows:
            fp.write("")
            return
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-csv", required=True)
    ap.add_argument("--price-db", required=True)
    ap.add_argument("--v21-config", default="obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dca-counts", default="0,1,2,3,4,5")
    ap.add_argument("--initial-equity", type=float, default=1000.0)
    ap.add_argument("--ttl-hours", type=float, default=72.0)
    ap.add_argument("--side", choices=["both", "long", "short"], default="both")
    ap.add_argument("--source-channel", default="", help="Optional exact source_channel filter from the signal CSV.")
    args = ap.parse_args()

    counts = [int(x.strip()) for x in args.dca_counts.split(",") if x.strip()]
    if not counts:
        raise SystemExit("--dca-counts produced no variants")

    out_dir = Path(args.out_dir)
    rows = load_price_rows(args.price_db)
    signals = read_signals(args.signals_csv, args.ttl_hours, args.side, args.source_channel)
    policy = load_v21_policy(args.v21_config, max(counts))

    summaries = []
    for count in counts:
        label = "plain" if count == 0 else f"v21_dca{count}"
        summary, trades, equity, sym = simulate(
            rows,
            signals,
            initial_equity=args.initial_equity,
            dca_count=count,
            policy=policy,
            tp1_frac=1.0 / 3.0,
            tp2_frac=0.5,
        )
        summaries.append({"variant": label, **summary})
        write_csv(out_dir / f"{label}_trades.csv", trades)
        write_csv(out_dir / f"{label}_equity.csv", equity)
        write_csv(out_dir / f"{label}_symbol_summary.csv", sym)

    write_csv(out_dir / "dca_summary.csv", summaries)
    manifest = {
        "signals_csv": args.signals_csv,
        "price_db": args.price_db,
        "v21_config": args.v21_config,
        "out_dir": str(out_dir),
        "dca_counts": counts,
        "side": args.side,
        "ttl_hours": args.ttl_hours,
        "price_rows": len(rows),
        "signals_loaded": len(signals),
    }
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    for row in summaries:
        print(row)


if __name__ == "__main__":
    main()
