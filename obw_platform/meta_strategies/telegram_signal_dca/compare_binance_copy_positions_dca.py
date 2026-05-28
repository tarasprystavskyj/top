#!/usr/bin/env python3
"""Compare Binance copy-trading historical positions against V21 DCA overlays.

Input positions are treated as signal timestamps:
- entry is the lead position open time and avgCost;
- exit is the lead position close time and avgClosePrice;
- DCA can add on adverse 1m candle touches between open and close;
- all variants close at the same lead close price.

This intentionally does not use Telegram TP/SL fields.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from telegram_signal_dca_compare import (
    load_v21_policy,
    max_drawdown,
    policy_for_capital_mode,
    ret_for,
)


BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"


@dataclass(frozen=True)
class CopyPosition:
    id: str
    symbol: str
    side: str
    opened: datetime
    closed: datetime
    entry: float
    exit: float
    lead_pnl: float
    lead_roi: float


def parse_dt(raw: Any) -> datetime:
    s = str(raw or "").strip().replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def parse_float(raw: Any, default: float = math.nan) -> float:
    try:
        if raw in ("", None):
            return default
        return float(str(raw).replace(",", ""))
    except Exception:
        return default


def read_positions(path: Path) -> List[CopyPosition]:
    out: List[CopyPosition] = []
    with path.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            side = str(row.get("side") or "").upper()
            if side not in {"LONG", "SHORT"}:
                continue
            entry = parse_float(row.get("avgCost"))
            exit_px = parse_float(row.get("avgClosePrice"))
            if not math.isfinite(entry) or not math.isfinite(exit_px) or entry <= 0 or exit_px <= 0:
                continue
            out.append(
                CopyPosition(
                    id=str(row.get("id") or ""),
                    symbol=str(row.get("symbol") or "").upper().strip(),
                    side=side,
                    opened=parse_dt(row.get("opened_utc")),
                    closed=parse_dt(row.get("closed_utc")),
                    entry=entry,
                    exit=exit_px,
                    lead_pnl=parse_float(row.get("closingPnl"), 0.0),
                    lead_roi=parse_float(row.get("roi"), 0.0),
                )
            )
    return sorted(out, key=lambda p: p.opened)


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def fetch_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    sleep_sec: float,
    max_retries: int,
) -> List[Dict[str, float]]:
    query_start = start_ms - (start_ms % 60_000)
    cursor = query_start
    rows: List[Dict[str, float]] = []
    sess = requests.Session()
    while cursor <= end_ms:
        params = {
            "symbol": symbol,
            "interval": "1m",
            "startTime": cursor,
            "endTime": end_ms,
            "limit": 1500,
        }
        for attempt in range(max_retries + 1):
            try:
                resp = sess.get(BINANCE_FAPI_KLINES, params=params, timeout=20)
                if resp.status_code == 429:
                    raise RuntimeError(f"rate limited: {resp.text[:120]}")
                resp.raise_for_status()
                batch = resp.json()
                break
            except Exception as exc:
                if attempt >= max_retries:
                    raise RuntimeError(f"{symbol} klines failed at {cursor}: {exc}") from exc
                time.sleep(max(1.0, sleep_sec * 8.0) * (attempt + 1))
        if not batch:
            break
        for r in batch:
            t = int(r[0])
            if query_start <= t <= end_ms:
                rows.append(
                    {
                        "t": t,
                        "open": float(r[1]),
                        "high": float(r[2]),
                        "low": float(r[3]),
                        "close": float(r[4]),
                    }
                )
        last = int(batch[-1][0])
        nxt = last + 60_000
        if nxt <= cursor or last >= end_ms:
            break
        cursor = nxt
        time.sleep(sleep_sec)
    seen: Dict[int, Dict[str, float]] = {}
    for row in rows:
        seen[int(row["t"])] = row
    return [seen[k] for k in sorted(seen)]


def load_or_fetch_candles(
    pos: CopyPosition,
    cache_dir: Path,
    *,
    sleep_sec: float,
    max_retries: int,
) -> List[Dict[str, float]]:
    path = cache_dir / f"{pos.id}_{pos.symbol}_{int(pos.opened.timestamp())}_{int(pos.closed.timestamp())}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    rows = fetch_klines(
        pos.symbol,
        ms(pos.opened),
        ms(pos.closed),
        sleep_sec=sleep_sec,
        max_retries=max_retries,
    )
    path.write_text(json.dumps(rows, separators=(",", ":")), encoding="utf-8")
    return rows


def dca_plan(side: str, entry_px: float, policy: Dict[str, Any], count: int) -> Tuple[List[float], List[float]]:
    side_policy = policy["long"] if side == "LONG" else policy["short"]
    levels: List[float] = []
    last = entry_px
    for step in side_policy["steps"][:count]:
        last = last * (1.0 - step / 100.0) if side == "LONG" else last * (1.0 + step / 100.0)
        levels.append(last)
    return levels, [float(x) for x in side_policy["adds"][:count]]


def touched(side: str, candle: Dict[str, float], level: float, *, fill_mode: str = "touch") -> bool:
    if fill_mode in {"close_beyond", "close_beyond_skip_boundary"}:
        return candle["close"] <= level if side == "LONG" else candle["close"] >= level
    return candle["low"] <= level if side == "LONG" else candle["high"] >= level


def simulate_position(
    pos: CopyPosition,
    candles: List[Dict[str, float]],
    *,
    policy: Dict[str, Any],
    dca_count: int,
    fill_mode: str = "touch",
    min_order_usd: float = 2.0,
) -> Dict[str, Any]:
    fee = float(policy["fee"])
    slip = float(policy["slippage"])
    side_policy = policy["long"] if pos.side == "LONG" else policy["short"]
    base_notional = float(side_policy["base_notional"])
    avg_entry = pos.entry
    notional = base_notional
    fills = 0
    levels, adds = dca_plan(pos.side, pos.entry, policy, dca_count)
    fill_rows: List[Dict[str, Any]] = []
    min_mtm = 0.0
    min_mtm_pct_on_notional = 0.0
    skip_boundary = fill_mode in {"touch_skip_boundary", "close_beyond_skip_boundary"}
    for i, candle in enumerate(candles):
        can_fill = not (skip_boundary and (i == 0 or i == len(candles) - 1))
        fills_this_candle = 0
        while can_fill and fills < len(levels) and touched(pos.side, candle, levels[fills], fill_mode=fill_mode):
            if fills_this_candle >= 1 and skip_boundary:
                break
            add_notional = adds[fills]
            old_qty = notional / max(avg_entry, 1e-12)
            add_qty = add_notional / max(levels[fills], 1e-12)
            notional += add_notional
            avg_entry = notional / max(old_qty + add_qty, 1e-12)
            fill_rows.append({"level": levels[fills], "notional": add_notional, "t": candle["t"]})
            fills += 1
            fills_this_candle += 1
        if skip_boundary and (i == 0 or i == len(candles) - 1):
            continue
        mark = float(candle["low"] if pos.side == "LONG" else candle["high"])
        mtm_ret = ret_for(pos.side, avg_entry, mark) - 2 * fee - 2 * slip
        mtm = mtm_ret * notional
        min_mtm = min(min_mtm, mtm)
        min_mtm_pct_on_notional = min(min_mtm_pct_on_notional, 100.0 * mtm / max(notional, 1e-12))
    gross_ret = ret_for(pos.side, avg_entry, pos.exit)
    net_ret = gross_ret - 2 * fee - 2 * slip
    pnl = net_ret * notional
    plain_ret = ret_for(pos.side, pos.entry, pos.exit)
    min_order = min([base_notional, *adds[:dca_count]]) if dca_count > 0 else base_notional
    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "side": pos.side,
        "opened_utc": pos.opened.isoformat().replace("+00:00", "Z"),
        "closed_utc": pos.closed.isoformat().replace("+00:00", "Z"),
        "duration_h": (pos.closed - pos.opened).total_seconds() / 3600.0,
        "entry": pos.entry,
        "exit": pos.exit,
        "plain_gross_ret_pct": 100.0 * plain_ret,
        "avg_entry": avg_entry,
        "notional": notional,
        "dca_fills": fills,
        "pnl": pnl,
        "min_mtm": min_mtm,
        "min_mtm_pct_on_notional": min_mtm_pct_on_notional,
        "min_order_usd": min_order,
        "min_order_ok": min_order >= min_order_usd,
        "ret_on_max_capital_pct": 100.0 * pnl / max(float(policy["target_notional"]), 1e-12),
        "lead_pnl": pos.lead_pnl,
        "lead_roi": pos.lead_roi,
        "candles": len(candles),
        "fill_mode": fill_mode,
        "fills_json": json.dumps(fill_rows, separators=(",", ":")),
    }


def summarize(rows: List[Dict[str, Any]], initial_equity: float, start: datetime, end: datetime) -> Dict[str, Any]:
    pnl_values = [float(r["pnl"]) for r in rows]
    equity = initial_equity
    curve = [equity]
    mtm_curve = [equity]
    for row, pnl in zip(rows, pnl_values):
        mtm_curve.append(equity + min(float(row.get("min_mtm", 0.0)), 0.0))
        equity += pnl
        curve.append(equity)
    days = max((end - start).total_seconds() / 86400.0, 1e-12)
    net = equity - initial_equity
    wins = sum(1 for x in pnl_values if x > 0)
    pos = sum(x for x in pnl_values if x > 0)
    neg = sum(x for x in pnl_values if x < 0)
    return {
        "positions": len(rows),
        "opened": len(rows),
        "test_start_utc": start.isoformat().replace("+00:00", "Z"),
        "test_end_utc": end.isoformat().replace("+00:00", "Z"),
        "duration_days": days,
        "equity_start": initial_equity,
        "equity_end": equity,
        "net_pnl": net,
        "net_pct": 100.0 * net / initial_equity,
        "net_pct_per_30d": 100.0 * net / initial_equity * 30.0 / days,
        "win_rate_pct": 100.0 * wins / max(1, len(rows)),
        "pf": pos / abs(neg) if neg < 0 else 0.0,
        "max_dd_pct": 100.0 * max_drawdown(curve),
        "max_mtm_dd_pct": 100.0 * max_drawdown(mtm_curve),
        "min_trade_mtm_pct_equity": 100.0
        * min((float(r.get("min_mtm", 0.0)) for r in rows), default=0.0)
        / max(initial_equity, 1e-12),
        "avg_dca_fills": sum(float(r["dca_fills"]) for r in rows) / max(1, len(rows)),
        "avg_notional": sum(float(r["notional"]) for r in rows) / max(1, len(rows)),
        "max_notional": max((float(r["notional"]) for r in rows), default=0.0),
        "min_order_ok": all(str(r.get("min_order_ok", "True")) == "True" for r in rows),
    }


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        w = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)


def safe_console(raw: Any) -> str:
    return str(raw).encode("ascii", "backslashreplace").decode("ascii")


def main() -> None:
    ap = argparse.ArgumentParser(description="Compare Binance copy-trading position history with V21 DCA overlays.")
    ap.add_argument("--positions-csv", default="obw_platform/meta_strategies/telegram_signal_dca/reports/binance_copy_4728671486012660992_20260519/position_history_normalized.csv")
    ap.add_argument("--v21-config", default="obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml")
    ap.add_argument("--out-dir", default="obw_platform/meta_strategies/telegram_signal_dca/reports/binance_copy_4728671486012660992_20260519/backtest")
    ap.add_argument("--target-notional", type=float, default=100.0)
    ap.add_argument("--initial-equity", type=float, default=500.0)
    ap.add_argument("--dca-counts", default="0,1,2,3")
    ap.add_argument(
        "--fill-mode",
        default="close_beyond_skip_boundary",
        choices=("touch", "touch_skip_boundary", "close_beyond", "close_beyond_skip_boundary"),
    )
    ap.add_argument("--sleep-sec", type=float, default=0.08)
    ap.add_argument("--max-retries", type=int, default=5)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = out_dir / "candles"
    cache_dir.mkdir(parents=True, exist_ok=True)

    positions = read_positions(Path(args.positions_csv))
    if not positions:
        raise SystemExit("No positions loaded")
    policy0 = load_v21_policy(args.v21_config, max(int(x) for x in args.dca_counts.split(",") if x.strip()))

    candle_cache: Dict[str, List[Dict[str, float]]] = {}
    eligible: List[Tuple[CopyPosition, List[Dict[str, float]]]] = []
    skipped: List[Dict[str, Any]] = []
    for i, pos in enumerate(positions, 1):
        try:
            candles = load_or_fetch_candles(pos, cache_dir, sleep_sec=args.sleep_sec, max_retries=args.max_retries)
        except Exception as exc:
            skipped.append({"id": pos.id, "symbol": pos.symbol, "error": str(exc)})
            print(f"[skip] {i}/{len(positions)} {safe_console(pos.symbol)} {pos.id}: {safe_console(exc)}", flush=True)
            continue
        candle_cache[pos.id] = candles
        if not candles:
            skipped.append({"id": pos.id, "symbol": pos.symbol, "error": "no candles"})
            continue
        eligible.append((pos, candles))
        print(f"[candles] {i}/{len(positions)} {safe_console(pos.symbol)} id={pos.id} rows={len(candles)}", flush=True)

    summaries: Dict[str, Dict[str, Any]] = {}
    start = min(p.opened for p, _ in eligible)
    end = max(p.closed for p, _ in eligible)
    for raw_count in args.dca_counts.split(","):
        count = int(raw_count.strip())
        policy = policy_for_capital_mode(policy0, count, args.target_notional, "same_max")
        rows = [
            simulate_position(p, candles, policy=policy, dca_count=count, fill_mode=args.fill_mode)
            for p, candles in eligible
        ]
        label = "plain" if count == 0 else f"dca{count}"
        write_csv(out_dir / f"{label}_trades.csv", rows)
        summary = summarize(rows, args.initial_equity, start, end)
        summary.update(
            {
                "label": label,
                "dca_count": count,
                "target_notional": args.target_notional,
                "capital_mode": "same_max",
                "fee": policy["fee"],
                "slippage_per_side": policy["slippage"],
                "fill_mode": args.fill_mode,
            }
        )
        summaries[label] = summary

    (out_dir / "skipped.json").write_text(json.dumps(skipped, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# Binance Copy Position History: Plain vs V21 DCA", ""]
    md.append(f"Positions loaded: {len(positions)}; tested: {len(eligible)}; skipped: {len(skipped)}")
    md.append("")
    md.append(f"Initial equity: {args.initial_equity:.2f}. Target notional is planned max position notional, not account equity.")
    md.append(f"Fill mode: `{args.fill_mode}`.")
    md.append("")
    md.append("| variant | positions | net % | /30d % | win % | PF | max MTM DD % | min trade MTM % eq | avg/max notional | avg DCA fills |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, s in summaries.items():
        md.append(
            f"| {label} | {s['positions']} | {s['net_pct']:.2f} | {s['net_pct_per_30d']:.2f} | "
            f"{s['win_rate_pct']:.1f} | {s['pf']:.2f} | {s['max_mtm_dd_pct']:.2f} | "
            f"{s['min_trade_mtm_pct_equity']:.2f} | {s['avg_notional']:.1f}/{s['max_notional']:.1f} | "
            f"{s['avg_dca_fills']:.2f} |"
        )
    md.append("")
    md.append("Entry/exit are the Binance lead position avg entry and avg close. DCA fills use 1m Binance Futures candles between those timestamps.")
    (out_dir / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"[done] {out_dir / 'REPORT.md'}")


if __name__ == "__main__":
    main()
