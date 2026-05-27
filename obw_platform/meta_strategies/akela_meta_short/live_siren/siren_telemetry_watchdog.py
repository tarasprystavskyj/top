#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
import time
from pathlib import Path

import ccxt
import yaml

ROOT = Path(__file__).resolve().parents[4]
LANE = ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "live_siren"
DEFAULT_CFG = LANE / "V21_siren_bingx_live_s0.yaml"
DEFAULT_LIVE_DIR = ROOT / "obw_platform" / "_reports" / "_live" / "bingx_siren_v21_s0"
STATUS_JSON = ROOT / "_reports" / "akela_meta_short" / "siren_live_prep" / "siren_watchdog_status.json"


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def sqlite_scalar(db_path: Path, sql: str, default=0):
    if not db_path.exists():
        return default
    try:
        con = sqlite3.connect(str(db_path))
        cur = con.execute(sql)
        row = cur.fetchone()
        con.close()
        return row[0] if row else default
    except Exception:
        return default


def sqlite_rows(db_path: Path, sql: str):
    if not db_path.exists():
        return []
    try:
        con = sqlite3.connect(str(db_path))
        con.row_factory = sqlite3.Row
        rows = [dict(r) for r in con.execute(sql).fetchall()]
        con.close()
        return rows
    except Exception:
        return []


def orderbook_status(exchange: str, symbol: str) -> dict:
    try:
        ex = getattr(ccxt, exchange)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
        book = ex.fetch_order_book(symbol, 10)
        bids = book.get("bids") or []
        asks = book.get("asks") or []
        best_bid = float(bids[0][0]) if bids else 0.0
        best_ask = float(asks[0][0]) if asks else 0.0
        mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
        spread_bp = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else None
        return {"ok": True, "best_bid": best_bid, "best_ask": best_ask, "spread_bp": spread_bp}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def q(values, quantile: float):
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not xs:
        return None
    idx = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * quantile))))
    return xs[idx]


def reason_text(reasons) -> str:
    return "\n".join(f"- {r}" for r in reasons) + "\n"


def display_path(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except Exception:
        return str(path)


def evaluate(args, cfg: dict) -> dict:
    live_dir = Path(args.live_dir)
    db_path = live_dir / "session.sqlite"
    now = utc_now()
    telemetry = ((cfg.get("runner") or {}).get("s0_micro_telemetry") or {})
    symbol = telemetry.get("symbol") or cfg.get("symbol") or "SIREN/USDT:USDT"
    fill_target = int(telemetry.get("fill_observation_target") or 30)
    notional_cap = float(telemetry.get("approved_notional_cap_usdt") or 0.0)
    loss_budget = float(telemetry.get("approved_loss_budget_usdt") or 0.0)
    debt_ratio_stop = float(telemetry.get("max_unrealized_to_realized_debt_ratio") or -0.5)
    spread_stop_bp = float(telemetry.get("spread_stop_bp") or 50.0)
    stress_bp = float(((cfg.get("backtest") or {}).get("stress_slippage_bp")) or 0.0)

    heartbeat = read_json(live_dir / "live_heartbeat.json", {})
    pnl = read_json(live_dir / "live_pnl_summary.json", {})
    positions = read_json(live_dir / "live_positions.json", {})
    exec_metrics = read_json(live_dir / "live_execution_metrics.json", {})
    live_dir_display = display_path(live_dir)
    prev_status = read_json(STATUS_JSON, {})
    if prev_status.get("live_dir") != live_dir_display:
        prev_status = {}

    reasons = []
    warnings = []
    if not live_dir.exists():
        warnings.append("live directory does not exist yet")
    if heartbeat:
        ts = heartbeat.get("ts_utc")
        try:
            age = (now - dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))).total_seconds()
            if age > float(args.heartbeat_timeout_sec):
                reasons.append(f"heartbeat stale {age:.1f}s > {args.heartbeat_timeout_sec}s")
        except Exception:
            reasons.append("heartbeat timestamp is invalid")
    elif live_dir.exists():
        warnings.append("heartbeat missing; waiting for runner startup")

    ob = orderbook_status(args.exchange, symbol)
    spread_high_count = 0
    if ob.get("ok") and ob.get("spread_bp") is not None:
        if float(ob["spread_bp"]) > spread_stop_bp:
            spread_high_count = int(prev_status.get("spread_high_consecutive_count") or 0) + 1
            if spread_high_count >= 3:
                reasons.append(f"spread {ob['spread_bp']:.3f}bp > {spread_stop_bp:.3f}bp for {spread_high_count} consecutive checks")
        else:
            spread_high_count = 0
    elif live_dir.exists():
        warnings.append(f"orderbook unavailable: {ob.get('error')}")

    fills = int(sqlite_scalar(db_path, "SELECT COUNT(*) FROM slippage_observations WHERE order_action='OPEN'", 0))
    if fills >= fill_target:
        reasons.append(f"fill observation target reached {fills} >= {fill_target}")

    adverse = [r.get("actual_adverse_bp") for r in sqlite_rows(db_path, "SELECT actual_adverse_bp FROM slippage_observations WHERE order_action='OPEN' ORDER BY id DESC LIMIT 200")]
    adverse_p95 = q(adverse, 0.95)
    adverse_max = max([float(x) for x in adverse if x is not None], default=None)
    if adverse_max is not None and adverse_max > 100.0:
        reasons.append(f"single adverse slippage {adverse_max:.3f}bp > 100bp")
    if adverse_p95 is not None and stress_bp > 0 and adverse_p95 > stress_bp * 2.0:
        reasons.append(f"adverse slippage p95 {adverse_p95:.3f}bp > 2x stress {stress_bp * 2.0:.3f}bp")

    rows = sqlite_rows(db_path, "SELECT status, reason, extra FROM orders ORDER BY ts_utc DESC LIMIT 120")
    recent_rejects = [r for r in rows if str(r.get("status") or "").upper() == "REJECTED"]
    min_rejects = [r for r in recent_rejects if "min" in (str(r.get("reason") or "") + " " + str(r.get("extra") or "")).lower()]
    if len(min_rejects) >= 2:
        reasons.append(f"min amount/cost rejects in recent window: {len(min_rejects)}")
    fallback_submitted = 0
    fallback_bad = 0
    for item in (exec_metrics.get("by_key") or []):
        if str(item.get("order_type") or "").lower() == "market_fallback":
            fallback_submitted += int(item.get("submitted") or 0)
            fallback_bad += int(item.get("rejected") or 0) + int(item.get("canceled_no_fill") or 0)
    if fallback_submitted >= 20 and fallback_bad / max(fallback_submitted, 1) > 0.30:
        reasons.append(f"market fallback bad ratio {fallback_bad}/{fallback_submitted} > 30%")
    if fills < fill_target and fallback_submitted >= 5:
        reasons.append(f"market fallback count {fallback_submitted} before {fill_target} clean fills")

    total_notional = 0.0
    for rec in (positions or {}).values() if isinstance(positions, dict) else []:
        try:
            total_notional += abs(float(rec.get("qty") or 0.0) * float(rec.get("entry") or 0.0))
        except Exception:
            pass
    if notional_cap > 0 and total_notional > notional_cap:
        reasons.append(f"open notional {total_notional:.4f} > cap {notional_cap:.4f}")

    realized = float(pnl.get("realized_pnl_cum") or pnl.get("realized_pnl") or 0.0)
    unrealized = float(pnl.get("unrealized_pnl") or 0.0)
    if loss_budget > 0 and realized + unrealized < -loss_budget:
        reasons.append(f"MTM loss {realized + unrealized:.4f} < -{loss_budget:.4f}")
    if realized > 0 and unrealized / realized < debt_ratio_stop:
        reasons.append(f"unrealized/realized ratio {unrealized / realized:.3f} < {debt_ratio_stop:.3f}")

    stop_path = live_dir / "STOP_NEW_ORDERS"
    if reasons and not stop_path.exists():
        stop_path.write_text(reason_text(reasons), encoding="utf-8")

    status = {
        "schema": "siren_s0_watchdog_status_v1",
        "ts_utc": now.isoformat(),
        "live_dir": live_dir_display,
        "symbol": symbol,
        "stop_new_orders": bool(reasons or stop_path.exists()),
        "reasons": reasons,
        "warnings": warnings,
        "fills_open": fills,
        "fill_target": fill_target,
        "adverse_slippage_p95_bp": adverse_p95,
        "adverse_slippage_max_bp": adverse_max,
        "orderbook": ob,
        "spread_high_consecutive_count": spread_high_count,
        "open_notional_usdt": total_notional,
        "realized_pnl": realized,
        "unrealized_pnl": unrealized,
        "heartbeat": heartbeat,
    }
    write_json(STATUS_JSON, status)
    write_json(live_dir / "WATCHDOG_STATUS.json", status)
    return status


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", default=str(DEFAULT_CFG))
    ap.add_argument("--live-dir", default=str(DEFAULT_LIVE_DIR))
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--interval-sec", type=float, default=5.0)
    ap.add_argument("--heartbeat-timeout-sec", type=float, default=60.0)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.cfg).read_text(encoding="utf-8")) or {}
    while True:
        status = evaluate(args, cfg)
        print(json.dumps(status, ensure_ascii=False, sort_keys=True))
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_sec)))


if __name__ == "__main__":
    raise SystemExit(main())
