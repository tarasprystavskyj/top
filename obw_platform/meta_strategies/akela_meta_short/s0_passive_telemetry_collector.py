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

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUT = ROOT / "_reports" / "akela_meta_short" / "s0_passive_orderbook"
DEFAULT_SYMBOLS = [
    "FREEDOMMONEY/USDT:USDT",
    "IDOL/USDT:USDT",
    "MAXXING/USDT:USDT",
    "SUP/USDT:USDT",
]
CONFIGS = {
    "FREEDOMMONEY/USDT:USDT": ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "live_freedommoney" / "V21_freedommoney_bingx_live_min2p2.yaml",
    "IDOL/USDT:USDT": ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "generated_configs" / "margin_zero" / "V21_idol_margin_zero_budget125.yaml",
    "MAXXING/USDT:USDT": ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "generated_configs" / "margin_zero" / "V21_maxxing_margin_zero_budget125_stress_exit.yaml",
    "SUP/USDT:USDT": ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "generated_configs" / "margin_zero" / "V21_sup_margin_zero_budget32_fast_exit.yaml",
}


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def parse_symbols(text: str):
    if not text:
        return list(DEFAULT_SYMBOLS)
    return [s.strip() for s in text.split(",") if s.strip()]


def load_tp_bp(symbol: str) -> dict:
    path = CONFIGS.get(symbol)
    out = {"long_tp_bp": None, "short_tp_bp": None, "cfg": str(path.relative_to(ROOT)) if path and path.exists() else ""}
    if not path or not path.exists():
        return out
    try:
        cfg = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        lp = ((cfg.get("strategy_params_long") or {}).get("tpPercent"))
        sp = ((cfg.get("strategy_params_short") or {}).get("tpPercent"))
        out["long_tp_bp"] = float(lp) * 100.0 if lp is not None else None
        out["short_tp_bp"] = float(sp) * 100.0 if sp is not None else None
    except Exception:
        pass
    return out


def ensure_db(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path))
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS passive_orderbook_snapshots (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            exchange TEXT NOT NULL,
            symbol TEXT NOT NULL,
            price REAL,
            best_bid REAL,
            best_ask REAL,
            mid REAL,
            spread_bp REAL,
            bid_top_qty REAL,
            ask_top_qty REAL,
            top5_bid_notional REAL,
            top5_ask_notional REAL,
            top10_bid_notional REAL,
            top10_ask_notional REAL,
            depth_1bp_bid_notional REAL,
            depth_1bp_ask_notional REAL,
            depth_5bp_bid_notional REAL,
            depth_5bp_ask_notional REAL,
            depth_10bp_bid_notional REAL,
            depth_10bp_ask_notional REAL,
            depth_25bp_bid_notional REAL,
            depth_25bp_ask_notional REAL,
            depth_50bp_bid_notional REAL,
            depth_50bp_ask_notional REAL,
            imbalance_top10 REAL,
            quote_volume_24h REAL,
            min_amount REAL,
            min_cost REAL,
            effective_min_order_usdt REAL,
            expected_roundtrip_cost_floor_bp REAL,
            long_tp_bp REAL,
            short_tp_bp REAL,
            long_edge_pass INTEGER,
            short_edge_pass INTEGER,
            raw_json TEXT
        )
        """
    )
    con.commit()
    con.close()


def q(values, prob: float):
    xs = sorted(float(v) for v in values if v is not None and math.isfinite(float(v)))
    if not xs:
        return None
    idx = min(len(xs) - 1, max(0, int(round((len(xs) - 1) * prob))))
    return xs[idx]


def depth_notional(levels, mid: float, bp: float, side: str) -> float:
    if mid <= 0:
        return 0.0
    total = 0.0
    if side == "bid":
        low, high = mid * (1.0 - bp / 10000.0), mid
    else:
        low, high = mid, mid * (1.0 + bp / 10000.0)
    for px, qty in levels or []:
        try:
            p = float(px)
            qv = float(qty)
        except Exception:
            continue
        if low <= p <= high:
            total += p * qv
    return total


def top_notional(levels, n: int) -> float:
    total = 0.0
    for px, qty in (levels or [])[:n]:
        try:
            total += float(px) * float(qty)
        except Exception:
            pass
    return total


def snapshot(ex, exchange: str, symbol: str, min_order_floor: float, buffer: float) -> dict:
    market = ex.market(symbol)
    ticker = ex.fetch_ticker(symbol)
    book = ex.fetch_order_book(symbol, 10)
    bids = book.get("bids") or []
    asks = book.get("asks") or []
    best_bid = float(bids[0][0]) if bids else 0.0
    best_ask = float(asks[0][0]) if asks else 0.0
    mid = (best_bid + best_ask) / 2.0 if best_bid > 0 and best_ask > 0 else 0.0
    price = float(ticker.get("last") or ticker.get("close") or mid or 0.0)
    spread_bp = ((best_ask - best_bid) / mid) * 10000.0 if mid > 0 else None
    min_amount = float(((market.get("limits") or {}).get("amount") or {}).get("min") or 0.0)
    min_cost = float(((market.get("limits") or {}).get("cost") or {}).get("min") or 0.0)
    effective = max(min_order_floor, min_cost * buffer if min_cost > 0 else 0.0, min_amount * price * buffer if min_amount > 0 and price > 0 else 0.0)
    bid10 = top_notional(bids, 10)
    ask10 = top_notional(asks, 10)
    denom = bid10 + ask10
    tp = load_tp_bp(symbol)
    # Passive book-only lower bound: crossing spread twice + taker fee twice.
    fee_bp_per_side = 5.0
    rt_floor = (float(spread_bp or 0.0) * 2.0) + (fee_bp_per_side * 2.0)
    long_tp = tp.get("long_tp_bp")
    short_tp = tp.get("short_tp_bp")
    return {
        "ts_utc": now_iso(),
        "exchange": exchange,
        "symbol": symbol,
        "price": price,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "mid": mid,
        "spread_bp": spread_bp,
        "bid_top_qty": float(bids[0][1]) if bids else None,
        "ask_top_qty": float(asks[0][1]) if asks else None,
        "top5_bid_notional": top_notional(bids, 5),
        "top5_ask_notional": top_notional(asks, 5),
        "top10_bid_notional": bid10,
        "top10_ask_notional": ask10,
        "depth_1bp_bid_notional": depth_notional(bids, mid, 1.0, "bid"),
        "depth_1bp_ask_notional": depth_notional(asks, mid, 1.0, "ask"),
        "depth_5bp_bid_notional": depth_notional(bids, mid, 5.0, "bid"),
        "depth_5bp_ask_notional": depth_notional(asks, mid, 5.0, "ask"),
        "depth_10bp_bid_notional": depth_notional(bids, mid, 10.0, "bid"),
        "depth_10bp_ask_notional": depth_notional(asks, mid, 10.0, "ask"),
        "depth_25bp_bid_notional": depth_notional(bids, mid, 25.0, "bid"),
        "depth_25bp_ask_notional": depth_notional(asks, mid, 25.0, "ask"),
        "depth_50bp_bid_notional": depth_notional(bids, mid, 50.0, "bid"),
        "depth_50bp_ask_notional": depth_notional(asks, mid, 50.0, "ask"),
        "imbalance_top10": ((bid10 - ask10) / denom) if denom > 0 else None,
        "quote_volume_24h": float(ticker.get("quoteVolume") or ticker.get("baseVolume") or 0.0),
        "min_amount": min_amount,
        "min_cost": min_cost,
        "effective_min_order_usdt": effective,
        "expected_roundtrip_cost_floor_bp": rt_floor,
        "long_tp_bp": long_tp,
        "short_tp_bp": short_tp,
        "long_edge_pass": int(long_tp is not None and rt_floor <= 0.5 * float(long_tp)),
        "short_edge_pass": int(short_tp is not None and rt_floor <= 0.5 * float(short_tp)),
        "raw_json": json.dumps({"ticker": ticker, "orderbook": book, "config": tp}, ensure_ascii=False),
    }


def insert_row(db_path: Path, row: dict) -> None:
    cols = [
        "ts_utc", "exchange", "symbol", "price", "best_bid", "best_ask", "mid", "spread_bp",
        "bid_top_qty", "ask_top_qty", "top5_bid_notional", "top5_ask_notional", "top10_bid_notional", "top10_ask_notional",
        "depth_1bp_bid_notional", "depth_1bp_ask_notional", "depth_5bp_bid_notional", "depth_5bp_ask_notional",
        "depth_10bp_bid_notional", "depth_10bp_ask_notional", "depth_25bp_bid_notional", "depth_25bp_ask_notional",
        "depth_50bp_bid_notional", "depth_50bp_ask_notional", "imbalance_top10", "quote_volume_24h",
        "min_amount", "min_cost", "effective_min_order_usdt", "expected_roundtrip_cost_floor_bp",
        "long_tp_bp", "short_tp_bp", "long_edge_pass", "short_edge_pass", "raw_json",
    ]
    con = sqlite3.connect(str(db_path))
    con.execute(
        f"INSERT INTO passive_orderbook_snapshots ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
        [row.get(c) for c in cols],
    )
    con.commit()
    con.close()


def summarize(db_path: Path, out_dir: Path, windows_minutes=(30, 60, 360, 1440)) -> dict:
    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    symbols = [r[0] for r in con.execute("SELECT DISTINCT symbol FROM passive_orderbook_snapshots ORDER BY symbol").fetchall()]
    now = dt.datetime.now(dt.timezone.utc)
    summary = {"schema": "s0_passive_orderbook_summary_v1", "ts_utc": now.isoformat(), "db": str(db_path), "symbols": {}}
    for sym in symbols:
        sym_out = {}
        for minutes in windows_minutes:
            cutoff = (now - dt.timedelta(minutes=int(minutes))).isoformat()
            rows = [dict(r) for r in con.execute("SELECT * FROM passive_orderbook_snapshots WHERE symbol=? AND ts_utc>=?", (sym, cutoff)).fetchall()]
            spreads = [r.get("spread_bp") for r in rows]
            rt = [r.get("expected_roundtrip_cost_floor_bp") for r in rows]
            sym_out[f"{minutes}m"] = {
                "n": len(rows),
                "spread_p50_bp": q(spreads, 0.50),
                "spread_p90_bp": q(spreads, 0.90),
                "spread_p95_bp": q(spreads, 0.95),
                "spread_min_bp": min([float(x) for x in spreads if x is not None], default=None),
                "top10_bid_p50_usdt": q([r.get("top10_bid_notional") for r in rows], 0.50),
                "top10_ask_p50_usdt": q([r.get("top10_ask_notional") for r in rows], 0.50),
                "roundtrip_floor_p50_bp": q(rt, 0.50),
                "edge_pass_long_count": sum(int(r.get("long_edge_pass") or 0) for r in rows),
                "edge_pass_short_count": sum(int(r.get("short_edge_pass") or 0) for r in rows),
            }
        summary["symbols"][sym] = sym_out
    con.close()
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    lines = ["# S0 passive orderbook summary", "", f"Updated: {summary['ts_utc']}", ""]
    for sym, data in summary["symbols"].items():
        lines.append(f"## {sym}")
        for win, vals in data.items():
            lines.append(
                f"- {win}: n={vals['n']} spread_p50={vals['spread_p50_bp']} spread_p95={vals['spread_p95_bp']} "
                f"rt_floor_p50={vals['roundtrip_floor_p50_bp']} edge_long={vals['edge_pass_long_count']} edge_short={vals['edge_pass_short_count']}"
            )
        lines.append("")
    (out_dir / "summary.md").write_text("\n".join(lines), encoding="utf-8")
    return summary


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--interval-sec", type=float, default=10.0)
    ap.add_argument("--summary-every-sec", type=float, default=300.0)
    ap.add_argument("--min-order-floor-usdt", type=float, default=2.02)
    ap.add_argument("--min-order-buffer", type=float, default=1.10)
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    db_path = out_dir / "s0_passive_orderbook.sqlite"
    ensure_db(db_path)
    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    symbols = parse_symbols(args.symbols)
    last_summary = 0.0
    while True:
        for sym in symbols:
            try:
                row = snapshot(ex, args.exchange, sym, args.min_order_floor_usdt, args.min_order_buffer)
                insert_row(db_path, row)
                print(json.dumps({k: row.get(k) for k in ("ts_utc", "symbol", "spread_bp", "top10_bid_notional", "top10_ask_notional", "expected_roundtrip_cost_floor_bp", "long_edge_pass", "short_edge_pass")}, ensure_ascii=False, sort_keys=True), flush=True)
            except Exception as exc:
                print(json.dumps({"ts_utc": now_iso(), "symbol": sym, "error": str(exc)}, ensure_ascii=False, sort_keys=True), flush=True)
        if time.time() - last_summary >= float(args.summary_every_sec):
            summarize(db_path, out_dir)
            last_summary = time.time()
        if args.once:
            return 0
        time.sleep(max(1.0, float(args.interval_sec)))


if __name__ == "__main__":
    raise SystemExit(main())
