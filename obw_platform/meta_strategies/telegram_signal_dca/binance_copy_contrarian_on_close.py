#!/usr/bin/env python3
"""Fetch Binance copy-trading positions and test contrarian-on-close exits.

For each closed lead position, this opens the opposite side at avgClosePrice
and exits after a fixed TTL or the end of available 1m Binance Futures candles.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import requests

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from telegram_signal_dca_compare import load_v21_policy, max_drawdown, policy_for_capital_mode, ret_for


POSITION_HISTORY_URL = (
    "https://www.binance.com/bapi/futures/v1/public/future/copy-trade/"
    "lead-portfolio/position-history"
)
BINANCE_FAPI_KLINES = "https://fapi.binance.com/fapi/v1/klines"
HEADERS = {
    "clienttype": "web",
    "content-type": "application/json",
    "user-agent": "Mozilla/5.0",
}


@dataclass(frozen=True)
class Position:
    id: str
    symbol: str
    lead_side: str
    contra_side: str
    opened_ms: int
    closed_ms: int
    opened: datetime
    closed: datetime
    avg_cost: float
    avg_close: float
    closing_pnl: float
    roi: float
    max_open_interest: float
    closed_volume: float
    isolated: str
    status: str
    leverage: str


def utc_from_ms(raw: Any) -> datetime:
    return datetime.fromtimestamp(int(raw) / 1000.0, tz=timezone.utc)


def parse_float(raw: Any, default: float = math.nan) -> float:
    try:
        if raw in ("", None):
            return default
        return float(str(raw).replace(",", ""))
    except Exception:
        return default


def norm_side(raw: Any) -> str:
    side = str(raw or "").strip().upper()
    if side in {"LONG", "BUY"}:
        return "LONG"
    if side in {"SHORT", "SELL"}:
        return "SHORT"
    return side


def opposite(side: str) -> str:
    return "SHORT" if side == "LONG" else "LONG"


def iso_ms(ms: int) -> str:
    return utc_from_ms(ms).isoformat().replace("+00:00", "Z")


def fetch_position_pages(
    portfolio_id: str,
    *,
    time_range: str,
    page_size: int,
    sleep_sec: float,
    max_pages: int,
    max_retries: int,
) -> List[Dict[str, Any]]:
    sess = requests.Session()
    pages: List[Dict[str, Any]] = []
    total = None
    page = 1
    while page <= max_pages:
        payload = {
            "portfolioId": str(portfolio_id),
            "pageNumber": page,
            "pageSize": page_size,
            "timeRange": time_range,
        }
        for attempt in range(max_retries + 1):
            try:
                resp = sess.post(POSITION_HISTORY_URL, json=payload, headers=HEADERS, timeout=20)
                resp.raise_for_status()
                data = resp.json()
                if data.get("code") != "000000":
                    raise RuntimeError(json.dumps(data, ensure_ascii=False)[:300])
                break
            except Exception as exc:
                if attempt >= max_retries:
                    raise RuntimeError(f"position page {page} failed: {exc}") from exc
                time.sleep(max(1.0, sleep_sec * 8.0) * (attempt + 1))
        page_data = data.get("data") or {}
        rows = page_data.get("list") or []
        total = int(page_data.get("total") or len(rows)) if total is None else total
        pages.append({"pageNumber": page, "request": payload, "response": data})
        print(f"[positions] page={page} rows={len(rows)} total={total}", flush=True)
        if not rows or page * page_size >= total:
            break
        page += 1
        time.sleep(sleep_sec)
    return pages


def normalize_pages(pages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen: Dict[str, Dict[str, Any]] = {}
    for page in pages:
        for row in ((page.get("response") or {}).get("data") or {}).get("list") or []:
            lead_side = norm_side(row.get("side"))
            avg_cost = parse_float(row.get("avgCost"))
            avg_close = parse_float(row.get("avgClosePrice"))
            opened_ms = int(row.get("opened") or 0)
            closed_ms = int(row.get("closed") or row.get("updateTime") or 0)
            if lead_side not in {"LONG", "SHORT"}:
                continue
            if opened_ms <= 0 or closed_ms <= 0 or not math.isfinite(avg_cost) or not math.isfinite(avg_close):
                continue
            if avg_cost <= 0 or avg_close <= 0:
                continue
            out = {
                "id": str(row.get("id") or ""),
                "symbol": str(row.get("symbol") or "").upper().strip(),
                "side": lead_side,
                "contrarian_side": opposite(lead_side),
                "opened_ms": opened_ms,
                "opened_utc": iso_ms(opened_ms),
                "closed_ms": closed_ms,
                "closed_utc": iso_ms(closed_ms),
                "avgCost": avg_cost,
                "avgClosePrice": avg_close,
                "closingPnl": parse_float(row.get("closingPnl"), 0.0),
                "roi": parse_float(row.get("roi"), 0.0),
                "maxOpenInterest": parse_float(row.get("maxOpenInterest"), 0.0),
                "closedVolume": parse_float(row.get("closedVolume"), 0.0),
                "isolated": str(row.get("isolated") or ""),
                "status": str(row.get("status") or ""),
                "leverage": str(row.get("leverage") or ""),
            }
            seen[out["id"]] = out
    return sorted(seen.values(), key=lambda r: int(r["closed_ms"]))


def read_positions(path: Path) -> List[Position]:
    out: List[Position] = []
    with path.open("r", encoding="utf-8", newline="") as fp:
        for row in csv.DictReader(fp):
            lead_side = norm_side(row.get("side"))
            if lead_side not in {"LONG", "SHORT"}:
                continue
            opened_ms = int(row["opened_ms"])
            closed_ms = int(row["closed_ms"])
            out.append(
                Position(
                    id=str(row["id"]),
                    symbol=str(row["symbol"]).upper().strip(),
                    lead_side=lead_side,
                    contra_side=norm_side(row.get("contrarian_side") or opposite(lead_side)),
                    opened_ms=opened_ms,
                    closed_ms=closed_ms,
                    opened=utc_from_ms(opened_ms),
                    closed=utc_from_ms(closed_ms),
                    avg_cost=parse_float(row.get("avgCost")),
                    avg_close=parse_float(row.get("avgClosePrice")),
                    closing_pnl=parse_float(row.get("closingPnl"), 0.0),
                    roi=parse_float(row.get("roi"), 0.0),
                    max_open_interest=parse_float(row.get("maxOpenInterest"), 0.0),
                    closed_volume=parse_float(row.get("closedVolume"), 0.0),
                    isolated=str(row.get("isolated") or ""),
                    status=str(row.get("status") or ""),
                    leverage=str(row.get("leverage") or ""),
                )
            )
    return sorted(out, key=lambda p: p.closed_ms)


def write_csv(path: Path, rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def fetch_klines(
    symbol: str,
    start_ms: int,
    end_ms: int,
    *,
    sleep_sec: float,
    max_retries: int,
) -> List[Dict[str, float]]:
    sess = requests.Session()
    rows: Dict[int, Dict[str, float]] = {}
    cursor = start_ms
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
                    raise RuntimeError(f"rate limited: {resp.text[:160]}")
                resp.raise_for_status()
                batch = resp.json()
                break
            except Exception as exc:
                if attempt >= max_retries:
                    raise RuntimeError(f"{symbol} klines failed at {cursor}: {exc}") from exc
                time.sleep(max(1.0, sleep_sec * 8.0) * (attempt + 1))
        if not batch:
            break
        for raw in batch:
            t = int(raw[0])
            if start_ms <= t <= end_ms:
                rows[t] = {
                    "t": t,
                    "open": float(raw[1]),
                    "high": float(raw[2]),
                    "low": float(raw[3]),
                    "close": float(raw[4]),
                }
        last = int(batch[-1][0])
        nxt = last + 60_000
        if nxt <= cursor or last >= end_ms:
            break
        cursor = nxt
        time.sleep(sleep_sec)
    return [rows[k] for k in sorted(rows)]


def load_or_fetch_candles(
    pos: Position,
    cache_dir: Path,
    ttl_hours: float,
    *,
    sleep_sec: float,
    max_retries: int,
) -> List[Dict[str, float]]:
    start_ms = ((pos.closed_ms // 60_000) + 1) * 60_000
    end_ms = pos.closed_ms + int(ttl_hours * 3600_000)
    path = cache_dir / f"{pos.id}_{pos.symbol}_{start_ms}_{end_ms}.json"
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    rows = fetch_klines(pos.symbol, start_ms, end_ms, sleep_sec=sleep_sec, max_retries=max_retries)
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


def touched(side: str, candle: Dict[str, float], level: float) -> bool:
    return candle["low"] <= level if side == "LONG" else candle["high"] >= level


def simulate(
    pos: Position,
    candles: List[Dict[str, float]],
    *,
    policy: Dict[str, Any],
    dca_count: int,
) -> Dict[str, Any]:
    side_policy = policy["long"] if pos.contra_side == "LONG" else policy["short"]
    fee = float(policy["fee"])
    slippage = float(policy["slippage"])
    entry = pos.avg_close
    avg_entry = entry
    notional = float(side_policy["base_notional"])
    fills = 0
    fill_rows: List[Dict[str, Any]] = []
    levels, adds = dca_plan(pos.contra_side, entry, policy, dca_count)
    for candle in candles:
        while fills < len(levels) and touched(pos.contra_side, candle, levels[fills]):
            add_notional = adds[fills]
            old_qty = notional / max(avg_entry, 1e-12)
            add_qty = add_notional / max(levels[fills], 1e-12)
            notional += add_notional
            avg_entry = notional / max(old_qty + add_qty, 1e-12)
            fill_rows.append(
                {
                    "t": int(candle["t"]),
                    "utc": iso_ms(int(candle["t"])),
                    "level": levels[fills],
                    "notional": add_notional,
                }
            )
            fills += 1
    exit_row = candles[-1]
    exit_px = float(exit_row["close"])
    gross_ret = ret_for(pos.contra_side, avg_entry, exit_px)
    net_ret = gross_ret - 2 * fee - 2 * slippage
    pnl = net_ret * notional
    return {
        "id": pos.id,
        "symbol": pos.symbol,
        "lead_side": pos.lead_side,
        "side": pos.contra_side,
        "lead_opened_utc": pos.opened.isoformat().replace("+00:00", "Z"),
        "entry_utc": pos.closed.isoformat().replace("+00:00", "Z"),
        "exit_utc": iso_ms(int(exit_row["t"])),
        "hold_h": (int(exit_row["t"]) - pos.closed_ms) / 3600_000.0,
        "entry": entry,
        "exit": exit_px,
        "avg_entry": avg_entry,
        "notional": notional,
        "dca_fills": fills,
        "gross_ret_pct": 100.0 * gross_ret,
        "net_ret_pct": 100.0 * net_ret,
        "pnl": pnl,
        "ret_on_100usdt_pct": 100.0 * pnl / max(float(policy["target_notional"]), 1e-12),
        "lead_avg_cost": pos.avg_cost,
        "lead_avg_close": pos.avg_close,
        "lead_pnl": pos.closing_pnl,
        "lead_roi": pos.roi,
        "candles": len(candles),
        "fills_json": json.dumps(fill_rows, separators=(",", ":")),
    }


def max_concurrent_positions(rows: List[Dict[str, Any]]) -> int:
    events: List[Tuple[datetime, int]] = []
    for row in rows:
        entry = datetime.fromisoformat(str(row["entry_utc"]).replace("Z", "+00:00"))
        exit_dt = datetime.fromisoformat(str(row["exit_utc"]).replace("Z", "+00:00"))
        events.append((entry, 1))
        events.append((exit_dt, -1))
    cur = 0
    max_cur = 0
    for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
        cur += delta
        max_cur = max(max_cur, cur)
    return max_cur


def summarize(rows: List[Dict[str, Any]], *, target_notional: float) -> Dict[str, Any]:
    if not rows:
        return {"count": 0}
    pnl_values = [float(r["pnl"]) for r in rows]
    start = min(str(r["entry_utc"]) for r in rows)
    end = max(str(r["exit_utc"]) for r in rows)
    start_dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
    end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
    days = max((end_dt - start_dt).total_seconds() / 86400.0, 1e-12)
    equity = float(target_notional)
    curve = [equity]
    for pnl in pnl_values:
        equity += pnl
        curve.append(equity)
    pnl_sum = sum(pnl_values)
    wins = sum(1 for x in pnl_values if x > 0)
    gross_profit = sum(x for x in pnl_values if x > 0)
    gross_loss = sum(x for x in pnl_values if x < 0)
    max_concurrent = max_concurrent_positions(rows)
    max_concurrent_capital = max_concurrent * target_notional
    return {
        "count": len(rows),
        "test_start_utc": start,
        "test_end_utc": end,
        "period_days": days,
        "target_notional": target_notional,
        "max_concurrent_positions": max_concurrent,
        "max_concurrent_capital": max_concurrent_capital,
        "net_pnl": pnl_sum,
        "net_pct": 100.0 * pnl_sum / max(target_notional, 1e-12),
        "net_pct_on_max_concurrent_capital": 100.0 * pnl_sum / max(max_concurrent_capital, 1e-12),
        "net_pct_per_30d": 100.0 * pnl_sum / max(target_notional, 1e-12) * 30.0 / days,
        "net_pct_per_30d_on_max_concurrent_capital": 100.0 * pnl_sum / max(max_concurrent_capital, 1e-12) * 30.0 / days,
        "avg_ret_on_100usdt_pct": sum(float(r["ret_on_100usdt_pct"]) for r in rows) / len(rows),
        "win_rate_pct": 100.0 * wins / len(rows),
        "pf": gross_profit / abs(gross_loss) if gross_loss < 0 else 0.0,
        "max_dd_pct": 100.0 * max_drawdown(curve),
        "avg_dca_fills": sum(float(r["dca_fills"]) for r in rows) / len(rows),
    }


def render_report(
    out_dir: Path,
    *,
    portfolio_id: str,
    positions_count: int,
    tested_count: int,
    skipped: List[Dict[str, Any]],
    summaries: Dict[str, Dict[str, Any]],
    ttl_hours: float,
) -> None:
    md = [f"# Binance Copy Contrarian-On-Close: {portfolio_id}", ""]
    md.append(f"Positions normalized: {positions_count}; tested: {tested_count}; skipped: {len(skipped)}.")
    md.append(f"Entry is opposite side at lead avgClosePrice/closed time. Exit is TTL {ttl_hours:g}h or final available 1m candle.")
    md.append("Plain notional is 100 USDT; DCA variants use V21 same_max 100.")
    md.append("`net %` is sum PnL divided by one 100 USDT unit; `net % max-cap` divides by max concurrent positions * 100 USDT.")
    md.append("")
    md.append("| variant | count | max conc | period days | net % | net % max-cap | /30d max-cap % | win % | PF | maxDD % | avg DCA fills |")
    md.append("|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|")
    for label, s in summaries.items():
        md.append(
            f"| {label} | {s.get('count', 0)} | {s.get('max_concurrent_positions', 0)} | "
            f"{s.get('period_days', 0.0):.2f} | {s.get('net_pct', 0.0):.2f} | "
            f"{s.get('net_pct_on_max_concurrent_capital', 0.0):.2f} | "
            f"{s.get('net_pct_per_30d_on_max_concurrent_capital', 0.0):.2f} | "
            f"{s.get('win_rate_pct', 0.0):.1f} | {s.get('pf', 0.0):.2f} | "
            f"{s.get('max_dd_pct', 0.0):.2f} | {s.get('avg_dca_fills', 0.0):.2f} |"
        )
    if skipped:
        md.append("")
        md.append(f"Skipped rows are in `{(out_dir / 'skipped.json').name}`.")
    (out_dir / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Fetch Binance copy positions and backtest contrarian-on-close.")
    ap.add_argument("--portfolio-id", default="4751838302089254401")
    ap.add_argument("--time-range", default="365D")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=5)
    ap.add_argument(
        "--out-dir",
        default="obw_platform/meta_strategies/telegram_signal_dca/reports/binance_copy_4751838302089254401_20260519",
    )
    ap.add_argument("--positions-csv", default="")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--ttl-hours", type=float, default=72.0)
    ap.add_argument("--target-notional", type=float, default=100.0)
    ap.add_argument("--dca-counts", default="0,1,2,3")
    ap.add_argument("--v21-config", default="obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml")
    ap.add_argument("--sleep-sec", type=float, default=0.08)
    ap.add_argument("--max-retries", type=int, default=5)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    positions_csv = Path(args.positions_csv) if args.positions_csv else out_dir / "position_history_normalized.csv"

    if not args.skip_fetch:
        pages = fetch_position_pages(
            args.portfolio_id,
            time_range=args.time_range,
            page_size=args.page_size,
            sleep_sec=args.sleep_sec,
            max_pages=args.max_pages,
            max_retries=args.max_retries,
        )
        (out_dir / "position_history_pages_raw.json").write_text(
            json.dumps(pages, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        normalized = normalize_pages(pages)
        write_csv(positions_csv, normalized)
    positions = read_positions(positions_csv)
    if not positions:
        raise SystemExit("No positions loaded")

    max_dca_count = max(int(x.strip()) for x in args.dca_counts.split(",") if x.strip())
    policy0 = load_v21_policy(args.v21_config, max_dca_count)
    cache_dir = out_dir / "candles_1m_after_close"
    cache_dir.mkdir(parents=True, exist_ok=True)

    eligible: List[Tuple[Position, List[Dict[str, float]]]] = []
    skipped: List[Dict[str, Any]] = []
    for i, pos in enumerate(positions, 1):
        try:
            candles = load_or_fetch_candles(
                pos,
                cache_dir,
                args.ttl_hours,
                sleep_sec=args.sleep_sec,
                max_retries=args.max_retries,
            )
        except Exception as exc:
            skipped.append({"id": pos.id, "symbol": pos.symbol, "error": str(exc)})
            print(f"[skip] {i}/{len(positions)} {pos.symbol} {pos.id}: {exc}", flush=True)
            continue
        if not candles:
            skipped.append({"id": pos.id, "symbol": pos.symbol, "error": "no after-close candles"})
            continue
        eligible.append((pos, candles))
        print(f"[candles] {i}/{len(positions)} {pos.symbol} id={pos.id} rows={len(candles)}", flush=True)

    summaries: Dict[str, Dict[str, Any]] = {}
    for raw_count in args.dca_counts.split(","):
        count = int(raw_count.strip())
        policy = policy_for_capital_mode(policy0, count, args.target_notional, "same_max")
        rows = [simulate(pos, candles, policy=policy, dca_count=count) for pos, candles in eligible]
        label = "plain" if count == 0 else f"dca{count}"
        write_csv(out_dir / f"{label}_trades.csv", rows)
        summary = summarize(rows, target_notional=args.target_notional)
        summary.update(
            {
                "label": label,
                "dca_count": count,
                "capital_mode": "same_max",
                "fee": policy["fee"],
                "slippage_per_side": policy["slippage"],
            }
        )
        summaries[label] = summary

    (out_dir / "skipped.json").write_text(json.dumps(skipped, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summaries, ensure_ascii=False, indent=2), encoding="utf-8")
    render_report(
        out_dir,
        portfolio_id=args.portfolio_id,
        positions_count=len(positions),
        tested_count=len(eligible),
        skipped=skipped,
        summaries=summaries,
        ttl_hours=args.ttl_hours,
    )
    print(json.dumps(summaries, ensure_ascii=False, indent=2))
    print(f"[done] {out_dir / 'REPORT.md'}")


if __name__ == "__main__":
    main()
