
from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


def ensure_microstructure_tables(session_db_path: str) -> None:
    con = sqlite3.connect(session_db_path)
    cur = con.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS market_microstructure (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            ts_utc TEXT,
            bar_time_utc TEXT,
            symbol TEXT,
            strategy_side TEXT,
            order_action TEXT,
            order_direction TEXT,
            phase TEXT,
            qty REAL,
            requested_price REAL,
            best_bid REAL,
            best_ask REAL,
            mid_price REAL,
            spread_abs REAL,
            spread_bp REAL,
            bid_top_qty REAL,
            ask_top_qty REAL,
            bid_depth_qty REAL,
            ask_depth_qty REAL,
            est_sweep_price REAL,
            est_sweep_slip_bp REAL,
            book_imbalance REAL,
            levels INTEGER,
            raw_json TEXT
        )
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS slippage_observations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id TEXT,
            ts_utc TEXT,
            bar_time_utc TEXT,
            symbol TEXT,
            strategy_side TEXT,
            order_action TEXT,
            order_direction TEXT,
            qty REAL,
            requested_price REAL,
            fill_price REAL,
            actual_adverse_bp REAL,
            snapshot_est_sweep_bp REAL,
            snapshot_spread_bp REAL,
            best_bid REAL,
            best_ask REAL,
            mid_price REAL,
            raw_json TEXT
        )
    """)
    con.commit()
    con.close()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def order_direction(strategy_side: str, order_action: str) -> str:
    side = str(strategy_side or '').upper()
    action = str(order_action or '').upper()
    if (side == 'LONG' and action == 'OPEN') or (side == 'SHORT' and action in {'CLOSE', 'PARTIAL'}):
        return 'BUY'
    return 'SELL'


def adverse_slip_bp(requested_price: float, fill_price: float, strategy_side: str, order_action: str) -> float:
    req = float(requested_price or 0.0)
    fill = float(fill_price or 0.0)
    if req <= 0 or fill <= 0:
        return 0.0
    side = str(strategy_side or '').upper()
    action = str(order_action or '').upper()
    if action == 'OPEN':
        return ((fill - req) / req) * 10000.0 if side == 'LONG' else ((req - fill) / req) * 10000.0
    return ((req - fill) / req) * 10000.0 if side == 'LONG' else ((fill - req) / req) * 10000.0


def safe_fetch_order_book(fetcher, symbol: str, limit: int = 10) -> Optional[Dict[str, Any]]:
    ex = getattr(fetcher, 'ex', fetcher)
    resolver = getattr(fetcher, 'resolve_symbol', None)
    ccxt_sym = symbol
    if callable(resolver):
        try:
            resolved = resolver(symbol)
            if resolved:
                ccxt_sym = resolved
        except Exception:
            pass
    try:
        book = ex.fetch_order_book(ccxt_sym, limit)
        if isinstance(book, dict):
            return book
    except Exception:
        return None
    return None


def _sum_depth(levels: List[List[float]]) -> float:
    total = 0.0
    for lv in levels or []:
        try:
            total += float(lv[1])
        except Exception:
            pass
    return total


def _sweep_price(levels: List[List[float]], qty: float) -> Optional[float]:
    remaining = float(qty or 0.0)
    if remaining <= 0:
        return None
    cost = 0.0
    got = 0.0
    for lv in levels or []:
        try:
            px = float(lv[0]); avail = float(lv[1])
        except Exception:
            continue
        take = min(avail, remaining)
        cost += take * px
        got += take
        remaining -= take
        if remaining <= 1e-12:
            break
    if got <= 0:
        return None
    if remaining > 1e-12:
        return None
    return cost / got


def summarize_order_book(book: Optional[Dict[str, Any]], *, qty: float, direction: str, requested_price: float) -> Dict[str, Any]:
    direction = str(direction or 'BUY').upper()
    bids = (book or {}).get('bids') or []
    asks = (book or {}).get('asks') or []
    best_bid = float(bids[0][0]) if bids else None
    best_ask = float(asks[0][0]) if asks else None
    bid_top_qty = float(bids[0][1]) if bids else None
    ask_top_qty = float(asks[0][1]) if asks else None
    mid = None
    spread_abs = None
    spread_bp = None
    if best_bid and best_ask and best_bid > 0 and best_ask > 0:
        mid = (best_bid + best_ask) / 2.0
        spread_abs = best_ask - best_bid
        spread_bp = (spread_abs / mid) * 10000.0 if mid > 0 else None

    levels = asks if direction == 'BUY' else bids
    est_sweep = _sweep_price(levels, qty)
    req = float(requested_price or 0.0)
    est_bp = None
    if est_sweep and req > 0:
        if direction == 'BUY':
            est_bp = ((est_sweep - req) / req) * 10000.0
        else:
            est_bp = ((req - est_sweep) / req) * 10000.0

    bid_depth = _sum_depth(bids)
    ask_depth = _sum_depth(asks)
    imbalance = None
    denom = bid_depth + ask_depth
    if denom > 0:
        imbalance = (bid_depth - ask_depth) / denom

    return {
        'best_bid': best_bid,
        'best_ask': best_ask,
        'mid_price': mid,
        'spread_abs': spread_abs,
        'spread_bp': spread_bp,
        'bid_top_qty': bid_top_qty,
        'ask_top_qty': ask_top_qty,
        'bid_depth_qty': bid_depth,
        'ask_depth_qty': ask_depth,
        'est_sweep_price': est_sweep,
        'est_sweep_slip_bp': est_bp,
        'book_imbalance': imbalance,
        'levels': min(len(bids), len(asks)),
        'raw_json': json.dumps(book or {}, ensure_ascii=False),
    }


def record_pretrade_snapshot(session_db_path: str, *, run_id: str, bar_time_utc: str, symbol: str, strategy_side: str, order_action: str, qty: float, requested_price: float, book: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    direction = order_direction(strategy_side, order_action)
    data = summarize_order_book(book, qty=qty, direction=direction, requested_price=requested_price)
    row = {
        'run_id': run_id,
        'ts_utc': _now_iso(),
        'bar_time_utc': str(bar_time_utc),
        'symbol': symbol,
        'strategy_side': str(strategy_side).upper(),
        'order_action': str(order_action).upper(),
        'order_direction': direction,
        'phase': 'pre',
        'qty': float(qty),
        'requested_price': float(requested_price),
        **data,
    }
    con = sqlite3.connect(session_db_path)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO market_microstructure
        (run_id, ts_utc, bar_time_utc, symbol, strategy_side, order_action, order_direction, phase, qty, requested_price,
         best_bid, best_ask, mid_price, spread_abs, spread_bp, bid_top_qty, ask_top_qty, bid_depth_qty, ask_depth_qty,
         est_sweep_price, est_sweep_slip_bp, book_imbalance, levels, raw_json)
        VALUES
        (:run_id, :ts_utc, :bar_time_utc, :symbol, :strategy_side, :order_action, :order_direction, :phase, :qty, :requested_price,
         :best_bid, :best_ask, :mid_price, :spread_abs, :spread_bp, :bid_top_qty, :ask_top_qty, :bid_depth_qty, :ask_depth_qty,
         :est_sweep_price, :est_sweep_slip_bp, :book_imbalance, :levels, :raw_json)
    """, row)
    con.commit()
    con.close()
    return row


def record_fill_observation(session_db_path: str, *, run_id: str, bar_time_utc: str, symbol: str, strategy_side: str, order_action: str, qty: float, requested_price: float, fill_price: float, pre_snapshot: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    row = {
        'run_id': run_id,
        'ts_utc': _now_iso(),
        'bar_time_utc': str(bar_time_utc),
        'symbol': symbol,
        'strategy_side': str(strategy_side).upper(),
        'order_action': str(order_action).upper(),
        'order_direction': order_direction(strategy_side, order_action),
        'qty': float(qty),
        'requested_price': float(requested_price),
        'fill_price': float(fill_price),
        'actual_adverse_bp': adverse_slip_bp(requested_price, fill_price, strategy_side, order_action),
        'snapshot_est_sweep_bp': (pre_snapshot or {}).get('est_sweep_slip_bp'),
        'snapshot_spread_bp': (pre_snapshot or {}).get('spread_bp'),
        'best_bid': (pre_snapshot or {}).get('best_bid'),
        'best_ask': (pre_snapshot or {}).get('best_ask'),
        'mid_price': (pre_snapshot or {}).get('mid_price'),
        'raw_json': json.dumps({'pre_snapshot': pre_snapshot or {}}, ensure_ascii=False),
    }
    con = sqlite3.connect(session_db_path)
    cur = con.cursor()
    cur.execute("""
        INSERT INTO slippage_observations
        (run_id, ts_utc, bar_time_utc, symbol, strategy_side, order_action, order_direction, qty, requested_price, fill_price,
         actual_adverse_bp, snapshot_est_sweep_bp, snapshot_spread_bp, best_bid, best_ask, mid_price, raw_json)
        VALUES
        (:run_id, :ts_utc, :bar_time_utc, :symbol, :strategy_side, :order_action, :order_direction, :qty, :requested_price, :fill_price,
         :actual_adverse_bp, :snapshot_est_sweep_bp, :snapshot_spread_bp, :best_bid, :best_ask, :mid_price, :raw_json)
    """, row)
    con.commit()
    con.close()
    return row
