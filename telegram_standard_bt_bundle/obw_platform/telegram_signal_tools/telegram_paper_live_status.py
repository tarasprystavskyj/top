#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only status/equity exporter for Telegram paper-live SQLite.

The output is intentionally close to the paper/live runner surface: one compact
status JSON plus an optional runner-compatible `equity` table snapshot. It never
places orders and never reads Telegram or exchange secrets.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import sqlite3
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(ts: dt.datetime) -> str:
    return ts.astimezone(dt.timezone.utc).isoformat()


def parse_float(raw: Any, default: float = 0.0) -> float:
    try:
        if raw is None or raw == "":
            return default
        val = float(raw)
        return val if math.isfinite(val) else default
    except Exception:
        return default


def load_marks(path: str) -> Dict[str, float]:
    if not path:
        return {}
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise SystemExit("--marks-json must contain a JSON object of symbol -> price")
    out: Dict[str, float] = {}
    for symbol, price in data.items():
        px = parse_float(price, math.nan)
        if math.isfinite(px) and px > 0:
            out[str(symbol)] = px
    return out


def maybe_fetch_marks(symbols: Iterable[str]) -> Dict[str, float]:
    try:
        import ccxt  # type: ignore
    except Exception:
        return {}
    try:
        ex = ccxt.bingx({"enableRateLimit": True})
        ex.load_markets()
    except Exception:
        return {}
    marks: Dict[str, float] = {}
    for symbol in sorted(set(str(s) for s in symbols if s)):
        try:
            ticker = ex.fetch_ticker(symbol)
            px = parse_float(ticker.get("last") or ticker.get("close"), math.nan)
            if math.isfinite(px) and px > 0:
                marks[symbol] = px
        except Exception:
            continue
    return marks


def table_count(cur: sqlite3.Cursor, table: str) -> int:
    try:
        return int(cur.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0])
    except Exception:
        return 0


def rows(cur: sqlite3.Cursor, sql: str, params: tuple = ()) -> List[sqlite3.Row]:
    return list(cur.execute(sql, params))


def unrealized_for_position(row: sqlite3.Row, mark_price: float) -> float:
    qty = parse_float(row["qty_open"])
    entry = parse_float(row["entry_price"])
    if qty <= 0 or entry <= 0 or mark_price <= 0:
        return 0.0
    side = str(row["side"] or "").lower()
    if side == "short":
        return qty * (entry - mark_price)
    return qty * (mark_price - entry)


def position_snapshot(row: sqlite3.Row, marks: Dict[str, float]) -> Dict[str, Any]:
    symbol = str(row["symbol"] or "")
    entry = parse_float(row["entry_price"])
    mark = marks.get(symbol) or entry
    unrealized = unrealized_for_position(row, mark)
    return {
        "signal_id": int(row["signal_id"]),
        "symbol": symbol,
        "side": row["side"],
        "status": row["status"],
        "entry_price": entry if entry > 0 else None,
        "mark_price": mark if mark > 0 else None,
        "mark_source": "marks" if symbol in marks else "entry_fallback",
        "qty_open": parse_float(row["qty_open"]),
        "notional": parse_float(row["notional"]),
        "realized_pnl": parse_float(row["realized_pnl"]),
        "unrealized_pnl": unrealized,
        "dca_count": int(row["dca_count"] or 0),
        "dca_filled": int(row["dca_filled"] or 0),
        "opened_at": row["opened_at"],
        "updated_at": row["updated_at"],
    }


def summarize(db_path: Path, *, initial_equity: float, marks: Dict[str, float]) -> Dict[str, Any]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    signals_total = table_count(cur, "signals")
    orders_total = table_count(cur, "orders")
    positions_total = table_count(cur, "positions")
    by_status = {
        str(r["status"] or "unknown"): int(r["n"])
        for r in rows(cur, "SELECT status, COUNT(*) AS n FROM positions GROUP BY status")
    }
    by_channel = [
        {"source_channel": r["source_channel"] or "", "signals": int(r["n"])}
        for r in rows(cur, "SELECT source_channel, COUNT(*) AS n FROM signals GROUP BY source_channel ORDER BY n DESC")
    ]
    latest_signal = rows(
        cur,
        """SELECT telegram_message_id, source_channel, ts_utc, symbol, side, received_at
           FROM signals ORDER BY received_at DESC, telegram_message_id DESC LIMIT 1""",
    )
    latest_order = rows(
        cur,
        """SELECT signal_id, ts_utc, mode, symbol, side, action, price, qty, notional, reason
           FROM orders ORDER BY ts_utc DESC LIMIT 1""",
    )
    open_rows = rows(
        cur,
        """SELECT * FROM positions
           WHERE status='open' AND qty_open > 0
           ORDER BY COALESCE(opened_at, updated_at, '') DESC""",
    )
    pending_rows = rows(
        cur,
        """SELECT signal_id, symbol, side, entry_low, entry_high, pending_created_at, pending_expires_at
           FROM positions WHERE status='pending' ORDER BY pending_created_at DESC""",
    )
    con.close()

    open_positions = [position_snapshot(r, marks) for r in open_rows]
    realized_pnl = sum(parse_float(p["realized_pnl"]) for p in open_positions)
    # Closed/expired rows can also carry realized PnL, so account for all rows.
    con = sqlite3.connect(db_path)
    realized_all = parse_float(con.execute("SELECT COALESCE(SUM(realized_pnl), 0) FROM positions").fetchone()[0])
    con.close()
    unrealized = sum(parse_float(p["unrealized_pnl"]) for p in open_positions)
    position_value = sum(parse_float(p["qty_open"]) * parse_float(p["mark_price"]) for p in open_positions)
    equity = float(initial_equity) + realized_all + unrealized

    now = utc_now()
    return {
        "generated_at_utc": iso(now),
        "db_path": str(db_path),
        "counts": {
            "signals": signals_total,
            "orders": orders_total,
            "positions": positions_total,
            "positions_by_status": by_status,
            "source_channels": by_channel,
        },
        "latest_signal": dict(latest_signal[0]) if latest_signal else None,
        "latest_order": dict(latest_order[0]) if latest_order else None,
        "pending_positions": [dict(r) for r in pending_rows],
        "open_positions": open_positions,
        "equity": {
            "initial_equity_usdt": float(initial_equity),
            "equity_usdt": equity,
            "cash_usdt": float(initial_equity) + realized_all,
            "position_value_usdt": position_value,
            "realized_pnl_cum": realized_all,
            "unrealized_pnl": unrealized,
            "open_positions": len(open_positions),
            "pending_positions": len(pending_rows),
            "open_realized_pnl_only": realized_pnl,
        },
    }


def write_runner_equity(session_db: Path, run_id: str, summary: Dict[str, Any]) -> None:
    session_db.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(session_db)
    cur = con.cursor()
    cur.execute("""CREATE TABLE IF NOT EXISTS equity(
        run_id TEXT, ts_utc TEXT, equity_usdt REAL, cash_usdt REAL, position_value_usdt REAL,
        realized_pnl_cum REAL, unrealized_pnl REAL,
        PRIMARY KEY(run_id, ts_utc)
    )""")
    eq = summary["equity"]
    cur.execute(
        """INSERT OR REPLACE INTO equity(
            run_id, ts_utc, equity_usdt, cash_usdt, position_value_usdt, realized_pnl_cum, unrealized_pnl
        ) VALUES(?,?,?,?,?,?,?)""",
        (
            run_id,
            summary["generated_at_utc"],
            float(eq["equity_usdt"]),
            float(eq["cash_usdt"]),
            float(eq["position_value_usdt"]),
            float(eq["realized_pnl_cum"]),
            float(eq["unrealized_pnl"]),
        ),
    )
    con.commit()
    con.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="runs/telegram_paper/paper_live.sqlite")
    ap.add_argument("--initial-equity", type=float, default=1000.0)
    ap.add_argument("--marks-json", default="", help="Optional symbol -> mark price JSON. Avoids exchange calls.")
    ap.add_argument("--fetch-marks", action="store_true", help="Optionally fetch current marks with ccxt/BingX.")
    ap.add_argument("--out-json", default="")
    ap.add_argument("--write-session-db", default="", help="Optional runner-compatible session.sqlite to append equity snapshot.")
    ap.add_argument("--run-id", default="telegram_paper_live")
    args = ap.parse_args()

    db_path = Path(args.db)
    if not db_path.exists():
        raise SystemExit("paper-live DB not found: %s" % db_path)

    marks = load_marks(args.marks_json)
    if args.fetch_marks:
        con = sqlite3.connect(db_path)
        symbols = [str(r[0]) for r in con.execute("SELECT DISTINCT symbol FROM positions WHERE status='open' AND symbol IS NOT NULL")]
        con.close()
        fetched = maybe_fetch_marks(symbols)
        marks.update(fetched)

    summary = summarize(db_path, initial_equity=args.initial_equity, marks=marks)
    text = json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True)
    if args.out_json:
        out = Path(args.out_json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    if args.write_session_db:
        write_runner_equity(Path(args.write_session_db), args.run_id, summary)
    print(text)


if __name__ == "__main__":
    main()
