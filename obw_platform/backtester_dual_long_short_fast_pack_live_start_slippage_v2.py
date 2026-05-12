#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backtester with best-effort live-start restore.

Purpose:
- Start a fast candle backtest from the same *live continuation point* as a live runner.
- Restore initial open positions from a previous live session SQLite `strategy_state_events`
  and/or `open_positions`.
- Supports two modes:
  * aggregate: emulate the current live runner restart behavior: restore aggregate positions
    into SimBook, but leave strategy internal lot-state fresh after warmup.
  * brief: restore strategy brief state from `strategy_state_events.state_after_json`.
           Works only when lots_count <= len(lots_tail); queues/recent-fill counters are still approximate.

This is not a full forensic replay. Full parity needs persistent full snapshots.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
import yaml

try:
    from backtester_dual_core_dynamic_v5 import (
        Position, SimBook,
        build_row, execution_price, import_by_path,
        parse_iso_to_epoch_s, pick_symbol_block,
        predict_adverse_slippage_bp,
        _dca_open_order_type, _load_dynamic_slippage,
        _snapshot_limit_price, _strategy_restore, _strategy_snapshot,
    )
except ImportError:
    from backtester_dual_core_dynamic_v2 import (
        Position, SimBook,
        build_row, execution_price, import_by_path,
        parse_iso_to_epoch_s, pick_symbol_block,
        predict_adverse_slippage_bp,
        _dca_open_order_type, _load_dynamic_slippage,
        _snapshot_limit_price, _strategy_restore, _strategy_snapshot,
    )


# --- live_group_amount_v1 slippage patch ---
_core_load_dynamic_slippage = _load_dynamic_slippage
_core_predict_adverse_slippage_bp = predict_adverse_slippage_bp

def _load_dynamic_slippage(cfg: dict, model_override: Optional[dict] = None) -> dict:
    """Preserve custom live_group_amount_v1 model instead of letting core simplify it."""
    if model_override and str(model_override.get("kind", "")) == "live_group_amount_v1":
        return dict(model_override)
    pf = (cfg.get("portfolio") or {})
    m = dict((pf.get("dynamic_slippage_model") or {}))
    if str(m.get("kind", "")) == "live_group_amount_v1":
        if model_override:
            m.update(model_override)
        return m
    return _core_load_dynamic_slippage(cfg, model_override=model_override)


def predict_adverse_slippage_bp(row: dict, side: str, action: str, qty: float, model: dict) -> float:
    """Group + amount slippage.

    key = SIDE|OPEN_or_CLOSE.
    amount dependency = slope * (log1p(qty * close) - group_center).
    This is deliberately conservative; it models measured live slippage, not the whole PnL gap.
    """
    if str((model or {}).get("kind", "")) != "live_group_amount_v1":
        return _core_predict_adverse_slippage_bp(row, side, action, qty, model)

    side_u = str(side).upper()
    action_u = "OPEN" if str(action).upper() == "OPEN" else "CLOSE"
    key = f"{side_u}|{action_u}"
    g = ((model or {}).get("groups") or {}).get(key) or {}
    close_px = float((row or {}).get("close", 0.0) or 0.0)
    notional = abs(float(qty or 0.0)) * max(close_px, 1e-12)
    log_notional = math.log1p(max(notional, 0.0))

    base = float(g.get("base_bp", (model or {}).get("fallback_bp", 0.0)) or 0.0)
    center = float(g.get("log_notional_center", log_notional) or log_notional)
    slope = float(g.get("log_notional_slope_bp", 0.0) or 0.0)
    val = base + slope * (log_notional - center)

    clip_min = float(g.get("clip_min_bp", (model or {}).get("global_clip_min_bp", 0.0)) or 0.0)
    clip_max = float(g.get("clip_max_bp", (model or {}).get("global_clip_max_bp", 25.0)) or 25.0)
    return float(np.clip(val, clip_min, clip_max))
# --- end live_group_amount_v1 slippage patch ---


def _dt_iso(ts_s: int) -> str:
    return pd.to_datetime(int(ts_s), unit='s', utc=True).strftime('%Y-%m-%dT%H:%M:%S+00:00')


def _json_loads_safe(x, default=None):
    if x in (None, ''):
        return default
    try:
        return json.loads(x)
    except Exception:
        return default


def _parse_time_to_epoch_s(s: str) -> int:
    return parse_iso_to_epoch_s(s)


def _load_latest_brief_state(session_db: str, symbol: str, init_ts_s: int) -> Dict[str, dict]:
    """Return latest state_after_json per side <= initial timestamp."""
    out = {}
    if not session_db:
        return out
    p = Path(session_db)
    if not p.exists():
        return out
    con = sqlite3.connect(str(p))
    try:
        # strategy_state_events may not exist in older sessions.
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'strategy_state_events' not in tables:
            return out
        q = """
        SELECT side, ts_utc, state_after_json, strategy_event, order_action, qty, entry, fill_price, delta_qty
        FROM strategy_state_events
        WHERE symbol = ?
          AND CAST(strftime('%s', replace(substr(ts_utc, 1, 19), 'T', ' ')) AS INTEGER) <= ?
        ORDER BY ts_utc ASC, id ASC
        """
        rows = con.execute(q, (symbol, int(init_ts_s))).fetchall()
        for side, ts_utc, state_after_json, strategy_event, order_action, qty, entry, fill_price, delta_qty in rows:
            st = _json_loads_safe(state_after_json, {}) or {}
            out[str(side).upper()] = {
                'ts_utc': ts_utc,
                'state': st,
                'strategy_event': strategy_event,
                'order_action': order_action,
                'qty': qty,
                'entry': entry,
                'fill_price': fill_price,
                'delta_qty': delta_qty,
            }
        return out
    finally:
        con.close()


def _load_latest_open_positions(session_db: str, symbol: str, init_ts_s: int) -> Dict[str, dict]:
    """Fallback aggregate positions from open_positions if state events are missing.

    This table is not a historical snapshot table in all runner versions, so this is fallback only.
    """
    out = {}
    if not session_db or not Path(session_db).exists():
        return out
    con = sqlite3.connect(session_db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'open_positions' not in tables:
            return out
        cols = [r[1] for r in con.execute("PRAGMA table_info(open_positions)").fetchall()]
        # Be defensive: schemas differ.
        df = pd.read_sql_query("SELECT * FROM open_positions", con)
        if df.empty:
            return out
        if 'symbol' in df.columns:
            df = df[df['symbol'] == symbol]
        if 'side' not in df.columns:
            return out
        # Prefer rows not explicitly closed if status exists.
        if 'status' in df.columns:
            df2 = df[df['status'].astype(str).str.upper().isin(['OPEN', ''])]
            if len(df2):
                df = df2
        for side in ['LONG', 'SHORT']:
            sub = df[df['side'].astype(str).str.upper() == side]
            if not len(sub):
                continue
            r = sub.iloc[-1].to_dict()
            qty = float(r.get('qty') or r.get('position_qty') or 0.0)
            entry = float(r.get('entry') or r.get('avg_entry') or r.get('entry_price') or 0.0)
            if qty > 0 and entry > 0:
                out[side] = {'qty': qty, 'entry': entry}
        return out
    finally:
        con.close()


def _latest_realized_before(session_db: str, init_ts_s: int) -> float:
    if not session_db or not Path(session_db).exists():
        return 0.0
    con = sqlite3.connect(session_db)
    try:
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'live_pnl_ledger' in tables:
            q = """
            SELECT realized_pnl_cum
            FROM live_pnl_ledger
            WHERE CAST(strftime('%s', replace(substr(ts_utc, 1, 19), 'T', ' ')) AS INTEGER) <= ?
            ORDER BY ts_utc DESC, id DESC
            LIMIT 1
            """
            row = con.execute(q, (int(init_ts_s),)).fetchone()
            if row and row[0] is not None:
                return float(row[0])
        if 'equity' in tables:
            q = """
            SELECT realized_pnl_cum
            FROM equity
            WHERE CAST(strftime('%s', replace(substr(ts_utc, 1, 19), 'T', ' ')) AS INTEGER) <= ?
            ORDER BY ts_utc DESC
            LIMIT 1
            """
            row = con.execute(q, (int(init_ts_s),)).fetchone()
            if row and row[0] is not None:
                return float(row[0])
    finally:
        con.close()
    return 0.0


def _brief_lots(st: dict) -> list:
    lots = st.get('lots_tail') or []
    out = []
    for lot in lots:
        if not isinstance(lot, dict):
            continue
        q = lot.get('qty')
        px = lot.get('price')
        try:
            q = float(q)
            px = float(px)
        except Exception:
            continue
        if q > 0 and px > 0:
            out.append((q, px))
    return out


def _state_to_initial_position(st: dict) -> Optional[Position]:
    qty = float(st.get('pos_size') or st.get('lots_total_qty') or 0.0)
    entry = st.get('avg_price')
    if entry in (None, ''):
        lots = _brief_lots(st)
        if lots:
            qty2 = sum(q for q, _ in lots)
            entry = sum(q * p for q, p in lots) / max(qty2, 1e-12)
            qty = qty or qty2
    try:
        entry = float(entry)
    except Exception:
        entry = 0.0
    if qty > 1e-12 and entry > 0:
        return Position('', entry, qty)
    return None


def _restore_strategy_brief_state(strat, symbol: str, st: dict) -> None:
    """Best-effort restore of the fields available in state_after_json brief snapshot."""
    if not st:
        return
    try:
        state = strat._get_state(symbol)
    except Exception:
        return

    direct = [
        'pos_size', 'pos_value_usdt', 'avg_price', 'num_fills', 'last_fill_price',
        'next_level_price', 'trailing_active', 'trailing_ref', 'reset_pending',
        'cycle_base_qty_coin', 'pending_new_entry', 'cycle_start_ts', 'last_fill_ts',
    ]
    for k in direct:
        if k in st:
            try:
                setattr(state, k, st[k])
            except Exception:
                pass
    lots = _brief_lots(st)
    lots_count = int(float(st.get('lots_count') or len(lots) or 0))
    if lots and lots_count == len(lots):
        try:
            state.lots = list(lots)
        except Exception:
            pass
    # If lots are truncated, do not pretend we have the full stack.
    # The caller should use aggregate mode instead for that case.


def _init_from_live_state(
    strat_long, strat_short, book: SimBook, *,
    session_db: str,
    symbol: str,
    init_ts_s: int,
    mode: str,
    initial_realized: Optional[float] = None,
) -> Tuple[Optional[Position], Optional[Position], dict]:
    """Initialize Position objects + SimBook from live state.

    mode='aggregate':
      Restore aggregate positions into SimBook lots, but do not restore strategy internal state.
      This best matches the current live runner restart semantics.

    mode='brief':
      Restore strategy brief state too. This is closer to ideal stateful parity but can diverge from
      the actual runner if the runner did not restore those states.
    """
    mode = str(mode or 'aggregate').lower().strip()
    brief = _load_latest_brief_state(session_db, symbol, init_ts_s)
    fallback_pos = _load_latest_open_positions(session_db, symbol, init_ts_s)

    init_info = {
        'mode': mode,
        'session_db': session_db,
        'symbol': symbol,
        'initial_time_utc': _dt_iso(init_ts_s),
        'source': 'strategy_state_events' if brief else ('open_positions' if fallback_pos else 'none'),
        'sides': {},
    }

    realized0 = _latest_realized_before(session_db, init_ts_s) if initial_realized is None else float(initial_realized)
    book.realized = float(realized0)

    pos_long = None
    pos_short = None

    for side, strat in [('LONG', strat_long), ('SHORT', strat_short)]:
        st = (brief.get(side) or {}).get('state') or {}
        pos = _state_to_initial_position(st) if st else None
        lots = _brief_lots(st) if st else []

        if pos is None and side in fallback_pos:
            q = float(fallback_pos[side]['qty'])
            e = float(fallback_pos[side]['entry'])
            pos = Position(side, e, q)
            lots = [(q, e)]

        if pos is None:
            init_info['sides'][side] = {'present': False}
            continue

        pos.side = side
        if mode == 'brief' and st:
            # Only trust lots_tail when it is complete.
            lots_count = int(float(st.get('lots_count') or len(lots) or 0))
            if not lots or lots_count != len(lots):
                lots = [(float(pos.qty), float(pos.entry))]
                init_info['sides'][side] = {'present': True, 'restore': 'aggregate_fallback_truncated_lots'}
            else:
                init_info['sides'][side] = {'present': True, 'restore': 'brief_state'}
            _restore_strategy_brief_state(strat, symbol, st)
        else:
            lots = [(float(pos.qty), float(pos.entry))]
            init_info['sides'][side] = {'present': True, 'restore': 'aggregate_position_only'}

        if side == 'LONG':
            book.lots_long = [[float(q), float(px)] for q, px in lots]
            pos_long = Position(side, float(pos.entry), float(pos.qty))
        else:
            book.lots_short = [[float(q), float(px)] for q, px in lots]
            pos_short = Position(side, float(pos.entry), float(pos.qty))

        init_info['sides'][side].update({
            'qty': float(pos.qty),
            'entry': float(pos.entry),
            'lots_count': len(lots),
            'lots': [{'qty': float(q), 'price': float(px)} for q, px in lots],
            'state_event_ts_utc': (brief.get(side) or {}).get('ts_utc'),
            'state_event': (brief.get(side) or {}).get('strategy_event'),
        })

    return pos_long, pos_short, init_info


def simulate_live_start(
    cfg: dict,
    ts_s: np.ndarray,
    close: np.ndarray,
    open_: Optional[np.ndarray] = None,
    high: Optional[np.ndarray] = None,
    low: Optional[np.ndarray] = None,
    volume: Optional[np.ndarray] = None,
    extras: Optional[Dict[str, np.ndarray]] = None,
    market_symbol: str = 'ENA/USDT:USDT',
    model_override: Optional[dict] = None,
    export_curves: bool = False,
    trade_start_ts_s: Optional[int] = None,
    initial_session_db: str = '',
    initial_mode: str = 'aggregate',
    initial_realized: Optional[float] = None,
):
    StratLong = import_by_path(cfg['strategy_class_long'])
    StratShort = import_by_path(cfg['strategy_class_short'])
    strat_long = StratLong(cfg)
    strat_short = StratShort(cfg)

    pf = cfg.get('portfolio', {}) or {}
    eq0_leg = float(pf.get('initial_equity_per_leg', 100.0))
    fee = float(pf.get('fee_rate', 0.0))
    maker_fee = float(pf.get('maker_fee_rate', fee))
    max_notional_frac = float(pf.get('max_notional_frac', 1.0))
    equity_start_total = 2 * eq0_leg
    slip_model = _load_dynamic_slippage(cfg, model_override=model_override)
    dca_order_type = _dca_open_order_type(cfg)

    def _entry_order_details(sig, side, px):
        ot = 'market'
        lp = None
        mf = maker_fee
        try:
            if hasattr(sig, 'order_type'):
                ot = str(getattr(sig, 'order_type') or 'market').lower().strip()
            elif isinstance(sig, dict):
                ot = str(sig.get('order_type', 'market') or 'market').lower().strip()
        except Exception:
            ot = 'market'
        try:
            if hasattr(sig, 'limit_price'):
                lp = getattr(sig, 'limit_price')
            elif isinstance(sig, dict):
                lp = sig.get('limit_price')
            lp = float(lp) if lp not in (None, '') else None
        except Exception:
            lp = None
        if lp is None and ot in {'limit', 'maker', 'maker_limit'}:
            lp = float(px)
        try:
            if hasattr(sig, 'maker_fee_rate') and getattr(sig, 'maker_fee_rate') is not None:
                mf = float(getattr(sig, 'maker_fee_rate'))
        except Exception:
            pass
        return ot, lp, mf

    def _limit_entry_touched(side, row, limit_px):
        if limit_px is None:
            return False
        if str(side).upper() == 'LONG':
            return float(row.get('low', row.get('close', limit_px))) <= float(limit_px)
        return float(row.get('high', row.get('close', limit_px))) >= float(limit_px)

    open_ = close if open_ is None else open_
    high = close if high is None else high
    low = close if low is None else low
    volume = np.zeros_like(close) if volume is None else volume
    extras = extras or {}

    warmup_bars_seen = 0

    def _warm_strategy(strat, row):
        fn = getattr(strat, 'warmup_history', None)
        if callable(fn):
            try:
                fn(market_symbol, [row], ctx={'source': 'backtester_live_start_prewarm'})
            except Exception:
                pass

    book = SimBook(fee_rate=fee, maker_fee_rate=maker_fee)
    pos_long = None
    pos_short = None
    init_info = {'mode': initial_mode, 'source': 'none'}

    trades_long = trades_short = wins_long = wins_short = 0
    eq_real = []
    eq_mtm = []
    curve_rows = []
    event_counts = {
        'open_long': 0, 'open_short': 0, 'open_limit_long': 0, 'open_limit_short': 0, 'open_limit_miss_long': 0, 'open_limit_miss_short': 0,
        'close_long': 0, 'close_short': 0,
        'partial_long': 0, 'partial_short': 0,
        'dca_long': 0, 'dca_short': 0,
        'dca_limit_long': 0, 'dca_limit_short': 0,
    }
    close_reason_counts = {}
    margin_call_events_total = bars_in_margin_call = 0
    prev_in_margin = False
    initialized_live_state = False

    for i, ts in enumerate(ts_s):
        row = build_row(ts, i, open_, high, low, close, volume, extras)
        px = float(row['close'])

        if trade_start_ts_s is not None and int(ts) < int(trade_start_ts_s):
            _warm_strategy(strat_long, row)
            _warm_strategy(strat_short, row)
            warmup_bars_seen += 1
            continue

        if not initialized_live_state:
            pos_long, pos_short, init_info = _init_from_live_state(
                strat_long, strat_short, book,
                session_db=initial_session_db,
                symbol=market_symbol,
                init_ts_s=int(trade_start_ts_s or ts),
                mode=initial_mode,
                initial_realized=initial_realized,
            )
            initialized_live_state = True

        # LONG manage
        if pos_long is not None:
            long_snapshot = _strategy_snapshot(strat_long, market_symbol)
            before_qty = pos_long.qty
            before_entry = pos_long.entry
            ex = strat_long.manage_position(market_symbol, row, pos_long, ctx=None)
            if ex and getattr(ex, 'action', None) in ('TP', 'SL', 'EXIT'):
                slip_bp = predict_adverse_slippage_bp(row, 'LONG', 'CLOSE', pos_long.qty, slip_model)
                exit_px = execution_price(float(getattr(ex, 'exit_price', px) or px), 'LONG', 'CLOSE', slip_bp)
                before_real = book.realized
                qty_close = pos_long.qty
                book.close_fill('LONG', qty_close, exit_px)
                if hasattr(strat_long, 'sync_after_external_fill'):
                    strat_long.sync_after_external_fill(market_symbol, qty=0.0, entry=0.0, fill_price=exit_px, delta_qty=qty_close, event='close')
                pnl_trade = book.realized - before_real
                trades_long += 1
                wins_long += 1 if pnl_trade > 0 else 0
                event_counts['close_long'] += 1
                _reason = str(getattr(ex, 'reason', getattr(ex, 'action', 'close_long')) or getattr(ex, 'action', 'close_long'))
                close_reason_counts[_reason] = int(close_reason_counts.get(_reason, 0)) + 1
                pos_long = None
            elif ex and getattr(ex, 'action', None) == 'TP_PARTIAL':
                qty_close = before_qty * float(getattr(ex, 'qty_frac', 0.0) or 0.0)
                if qty_close > 1e-12:
                    slip_bp = predict_adverse_slippage_bp(row, 'LONG', 'CLOSE', qty_close, slip_model)
                    exit_px = execution_price(float(getattr(ex, 'exit_price', px) or px), 'LONG', 'CLOSE', slip_bp)
                    before_real = book.realized
                    book.close_fill('LONG', qty_close, exit_px)
                    pos_long.qty = max(0.0, before_qty - qty_close)
                    rem_qty = pos_long.qty
                    rem_entry = book.avg_entry('LONG') if rem_qty > 1e-12 else 0.0
                    if hasattr(strat_long, 'sync_after_external_fill'):
                        strat_long.sync_after_external_fill(market_symbol, qty=rem_qty, entry=rem_entry, fill_price=exit_px, delta_qty=qty_close, event='partial')
                    pnl_trade = book.realized - before_real
                    trades_long += 1
                    wins_long += 1 if pnl_trade > 0 else 0
                    event_counts['partial_long'] += 1
                    _reason = str(getattr(ex, 'reason', 'partial_long') or 'partial_long')
                    close_reason_counts[_reason] = int(close_reason_counts.get(_reason, 0)) + 1
                    if pos_long.qty <= 1e-12:
                        pos_long = None
            elif pos_long is not None and pos_long.qty > before_qty + 1e-12:
                add_qty = pos_long.qty - before_qty
                if dca_order_type in {'limit', 'maker', 'maker_limit'}:
                    limit_px = _snapshot_limit_price(long_snapshot, px)
                    touched = float(row.get('low', px)) <= float(limit_px)
                    if touched:
                        exec_px = float(limit_px)
                        book.open_fill('LONG', add_qty, exec_px)
                        event_counts['dca_limit_long'] += 1
                        if hasattr(strat_long, 'sync_after_external_fill'):
                            strat_long.sync_after_external_fill(market_symbol, qty=pos_long.qty, entry=book.avg_entry('LONG'), fill_price=exec_px, delta_qty=add_qty, event='dca_limit')
                    else:
                        _strategy_restore(strat_long, market_symbol, long_snapshot)
                        pos_long.qty = before_qty
                        pos_long.entry = before_entry
                else:
                    slip_bp = predict_adverse_slippage_bp(row, 'LONG', 'OPEN', add_qty, slip_model)
                    exec_px = execution_price(px, 'LONG', 'OPEN', slip_bp)
                    book.open_fill('LONG', add_qty, exec_px)
                    event_counts['dca_long'] += 1
                    if hasattr(strat_long, 'sync_after_external_fill'):
                        strat_long.sync_after_external_fill(market_symbol, qty=pos_long.qty, entry=book.avg_entry('LONG'), fill_price=exec_px, delta_qty=add_qty, event='dca')

        # SHORT manage
        if pos_short is not None:
            short_snapshot = _strategy_snapshot(strat_short, market_symbol)
            before_qty = pos_short.qty
            before_entry = pos_short.entry
            ex = strat_short.manage_position(market_symbol, row, pos_short, ctx=None)
            if ex and getattr(ex, 'action', None) in ('TP', 'SL', 'EXIT'):
                slip_bp = predict_adverse_slippage_bp(row, 'SHORT', 'CLOSE', pos_short.qty, slip_model)
                exit_px = execution_price(float(getattr(ex, 'exit_price', px) or px), 'SHORT', 'CLOSE', slip_bp)
                before_real = book.realized
                qty_close = pos_short.qty
                book.close_fill('SHORT', qty_close, exit_px)
                if hasattr(strat_short, 'sync_after_external_fill'):
                    strat_short.sync_after_external_fill(market_symbol, qty=0.0, entry=0.0, fill_price=exit_px, delta_qty=qty_close, event='close')
                pnl_trade = book.realized - before_real
                trades_short += 1
                wins_short += 1 if pnl_trade > 0 else 0
                event_counts['close_short'] += 1
                _reason = str(getattr(ex, 'reason', getattr(ex, 'action', 'close_short')) or getattr(ex, 'action', 'close_short'))
                close_reason_counts[_reason] = int(close_reason_counts.get(_reason, 0)) + 1
                pos_short = None
            elif ex and getattr(ex, 'action', None) == 'TP_PARTIAL':
                qty_close = before_qty * float(getattr(ex, 'qty_frac', 0.0) or 0.0)
                if qty_close > 1e-12:
                    slip_bp = predict_adverse_slippage_bp(row, 'SHORT', 'CLOSE', qty_close, slip_model)
                    exit_px = execution_price(float(getattr(ex, 'exit_price', px) or px), 'SHORT', 'CLOSE', slip_bp)
                    before_real = book.realized
                    book.close_fill('SHORT', qty_close, exit_px)
                    pos_short.qty = max(0.0, before_qty - qty_close)
                    rem_qty = pos_short.qty
                    rem_entry = book.avg_entry('SHORT') if rem_qty > 1e-12 else 0.0
                    if hasattr(strat_short, 'sync_after_external_fill'):
                        strat_short.sync_after_external_fill(market_symbol, qty=rem_qty, entry=rem_entry, fill_price=exit_px, delta_qty=qty_close, event='partial')
                    pnl_trade = book.realized - before_real
                    trades_short += 1
                    wins_short += 1 if pnl_trade > 0 else 0
                    event_counts['partial_short'] += 1
                    _reason = str(getattr(ex, 'reason', 'partial_short') or 'partial_short')
                    close_reason_counts[_reason] = int(close_reason_counts.get(_reason, 0)) + 1
                    if pos_short.qty <= 1e-12:
                        pos_short = None
            elif pos_short is not None and pos_short.qty > before_qty + 1e-12:
                add_qty = pos_short.qty - before_qty
                if dca_order_type in {'limit', 'maker', 'maker_limit'}:
                    limit_px = _snapshot_limit_price(short_snapshot, px)
                    touched = float(row.get('high', px)) >= float(limit_px)
                    if touched:
                        exec_px = float(limit_px)
                        book.open_fill('SHORT', add_qty, exec_px)
                        event_counts['dca_limit_short'] += 1
                        if hasattr(strat_short, 'sync_after_external_fill'):
                            strat_short.sync_after_external_fill(market_symbol, qty=pos_short.qty, entry=book.avg_entry('SHORT'), fill_price=exec_px, delta_qty=add_qty, event='dca_limit')
                    else:
                        _strategy_restore(strat_short, market_symbol, short_snapshot)
                        pos_short.qty = before_qty
                        pos_short.entry = before_entry
                else:
                    slip_bp = predict_adverse_slippage_bp(row, 'SHORT', 'OPEN', add_qty, slip_model)
                    exec_px = execution_price(px, 'SHORT', 'OPEN', slip_bp)
                    book.open_fill('SHORT', add_qty, exec_px)
                    event_counts['dca_short'] += 1
                    if hasattr(strat_short, 'sync_after_external_fill'):
                        strat_short.sync_after_external_fill(market_symbol, qty=pos_short.qty, entry=book.avg_entry('SHORT'), fill_price=exec_px, delta_qty=add_qty, event='dca')

        # LONG entry
        if pos_long is None:
            long_entry_snapshot = _strategy_snapshot(strat_long, market_symbol)
            sig = strat_long.entry_signal(True, market_symbol, row, ctx=None)
            if sig is not None:
                qty = float(getattr(sig, 'qty', 0.0) or 0.0)
                if qty > 0:
                    order_type, limit_px, maker_fr = _entry_order_details(sig, 'LONG', px)
                    if order_type in {'limit', 'maker', 'maker_limit'}:
                        if _limit_entry_touched('LONG', row, limit_px):
                            exec_px = float(limit_px)
                            book.open_fill('LONG', qty, exec_px, fee_rate=maker_fr)
                            event_counts['open_long'] += 1
                            event_counts['open_limit_long'] += 1
                            pos_long = Position('LONG', exec_px, qty)
                            if hasattr(strat_long, 'sync_after_external_fill'):
                                strat_long.sync_after_external_fill(market_symbol, qty=qty, entry=book.avg_entry('LONG'), fill_price=exec_px, delta_qty=qty, event='open_limit')
                        else:
                            _strategy_restore(strat_long, market_symbol, long_entry_snapshot)
                            event_counts['open_limit_miss_long'] += 1
                    else:
                        slip_bp = predict_adverse_slippage_bp(row, 'LONG', 'OPEN', qty, slip_model)
                        exec_px = execution_price(px, 'LONG', 'OPEN', slip_bp)
                        book.open_fill('LONG', qty, exec_px)
                        event_counts['open_long'] += 1
                        pos_long = Position('LONG', px, qty)
                        if hasattr(strat_long, 'sync_after_external_fill'):
                            strat_long.sync_after_external_fill(market_symbol, qty=qty, entry=book.avg_entry('LONG'), fill_price=exec_px, delta_qty=qty, event='open')

        # SHORT entry
        if pos_short is None:
            short_entry_snapshot = _strategy_snapshot(strat_short, market_symbol)
            sig = strat_short.entry_signal(True, market_symbol, row, ctx=None)
            if sig is not None:
                qty = float(getattr(sig, 'qty', 0.0) or 0.0)
                if qty > 0:
                    order_type, limit_px, maker_fr = _entry_order_details(sig, 'SHORT', px)
                    if order_type in {'limit', 'maker', 'maker_limit'}:
                        if _limit_entry_touched('SHORT', row, limit_px):
                            exec_px = float(limit_px)
                            book.open_fill('SHORT', qty, exec_px, fee_rate=maker_fr)
                            event_counts['open_short'] += 1
                            event_counts['open_limit_short'] += 1
                            pos_short = Position('SHORT', exec_px, qty)
                            if hasattr(strat_short, 'sync_after_external_fill'):
                                strat_short.sync_after_external_fill(market_symbol, qty=qty, entry=book.avg_entry('SHORT'), fill_price=exec_px, delta_qty=qty, event='open_limit')
                        else:
                            _strategy_restore(strat_short, market_symbol, short_entry_snapshot)
                            event_counts['open_limit_miss_short'] += 1
                    else:
                        slip_bp = predict_adverse_slippage_bp(row, 'SHORT', 'OPEN', qty, slip_model)
                        exec_px = execution_price(px, 'SHORT', 'OPEN', slip_bp)
                        book.open_fill('SHORT', qty, exec_px)
                        event_counts['open_short'] += 1
                        pos_short = Position('SHORT', px, qty)
                        if hasattr(strat_short, 'sync_after_external_fill'):
                            strat_short.sync_after_external_fill(market_symbol, qty=qty, entry=book.avg_entry('SHORT'), fill_price=exec_px, delta_qty=qty, event='open')

        eq_r = equity_start_total + book.realized
        eq_u = eq_r + book.unrealized(px)
        gross_long = sum(q * e for q, e in book.lots_long)
        gross_short = sum(q * e for q, e in book.lots_short)
        effective = abs(gross_long - gross_short)
        allowed = max_notional_frac * max(eq_u, 0.0)
        in_margin = effective > allowed
        if in_margin:
            bars_in_margin_call += 1
        if in_margin and not prev_in_margin:
            margin_call_events_total += 1
        prev_in_margin = in_margin

        eq_real.append(eq_r)
        eq_mtm.append(eq_u)
        if export_curves:
            unreal_long = book.unrealized_side('LONG', px)
            unreal_short = book.unrealized_side('SHORT', px)
            curve_rows.append({
                'bar_ts': _dt_iso(int(ts)),
                'bar_ts_s': int(ts),
                'realized_pnl': book.realized,
                'realized_pnl_long': book.realized_long,
                'realized_pnl_short': book.realized_short,
                'unrealized_pnl': unreal_long + unreal_short,
                'unrealized_pnl_long': unreal_long,
                'unrealized_pnl_short': unreal_short,
                'total_pnl': book.realized + unreal_long + unreal_short,
                'equity_realized_total': eq_r,
                'equity_realized_long': eq0_leg + book.realized_long,
                'equity_realized_short': eq0_leg + book.realized_short,
                'equity_mtm_total': eq_u,
                'long_notional': gross_long,
                'short_notional': gross_short,
                'effective_notional': effective,
                'allowed_notional': allowed,
                'margin_excess': effective - allowed,
                'in_margin_call': int(in_margin),
                'mark_close': px,
            })

    if not eq_real:
        return {'error': 'no bars simulated'}

    eq_real = np.asarray(eq_real, dtype=float)
    eq_mtm = np.asarray(eq_mtm, dtype=float)
    peak_mtm = np.maximum.accumulate(eq_mtm)
    dd_mtm = (eq_mtm - peak_mtm) / np.maximum(peak_mtm, 1e-12)
    peak_real = np.maximum.accumulate(eq_real)
    dd_real = (eq_real - peak_real) / np.maximum(peak_real, 1e-12)

    out = {
        'equity_start_total': equity_start_total,
        'equity_end_realized_total': float(eq_real[-1]),
        'realized_pnl_total': float(book.realized),
        'realized_pnl_long': float(book.realized_long),
        'realized_pnl_short': float(book.realized_short),
        'final_unrealized_pnl': float(book.unrealized(float(close[-1]))),
        'final_total_pnl': float(book.realized + book.unrealized(float(close[-1]))),
        'trades_long': int(trades_long),
        'trades_short': int(trades_short),
        'trades_total': int(trades_long + trades_short),
        'win_rate_long_%': float(100.0 * wins_long / trades_long) if trades_long else 0.0,
        'win_rate_short_%': float(100.0 * wins_short / trades_short) if trades_short else 0.0,
        'mdd_mtm_frac': float(dd_mtm.min()),
        'mdd_mtm_%': float(100.0 * dd_mtm.min()),
        'mdd_realized_frac': float(dd_real.min()),
        'mdd_realized_%': float(100.0 * dd_real.min()),
        'margin_call_events_total': int(margin_call_events_total),
        'bars_in_margin_call': int(bars_in_margin_call),
        'dynamic_slippage_model': slip_model,
        'maker_fee_rate': maker_fee,
        'warmup_bars_seen': int(warmup_bars_seen),
        'trade_start_ts_s': int(trade_start_ts_s or 0),
        'initial_live_state': init_info,
        'order_event_counts': event_counts,
        'close_reason_counts': close_reason_counts,
        'total_order_events': int(sum(event_counts.values())),
    }
    if export_curves:
        out['curves'] = pd.DataFrame(curve_rows)
    return out


def save_plot_bundle(curves: pd.DataFrame, plots_dir: str, prefix: str = 'dual_live_start') -> dict:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    plots_path = Path(plots_dir)
    plots_path.mkdir(parents=True, exist_ok=True)
    df = curves.copy()
    if df.empty:
        return {'plots_dir': str(plots_path), 'plots': []}
    df['bar_ts'] = pd.to_datetime(df['bar_ts'], utc=True, errors='coerce')
    df = df.dropna(subset=['bar_ts']).sort_values('bar_ts')
    generated = []

    def _save(fig, name):
        out = plots_path / name
        fig.tight_layout()
        fig.savefig(out, dpi=160, bbox_inches='tight')
        plt.close(fig)
        generated.append(str(out))

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df['bar_ts'], df['total_pnl'], label='Total PnL')
    ax.plot(df['bar_ts'], df['realized_pnl'], label='Realized')
    ax.plot(df['bar_ts'], df['unrealized_pnl'], label='Unrealized')
    ax.set_title('Live-start backtest PnL')
    ax.set_xlabel('Time (UTC)')
    ax.set_ylabel('PnL')
    ax.grid(True, alpha=0.3)
    ax.legend()
    _save(fig, f'{prefix}_pnl.png')

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df['bar_ts'], df['long_notional'], label='Long notional')
    ax.plot(df['bar_ts'], df['short_notional'], label='Short notional')
    ax.set_title('Live-start backtest notional')
    ax.set_xlabel('Time (UTC)')
    ax.set_ylabel('USDT')
    ax.grid(True, alpha=0.3)
    ax.legend()
    _save(fig, f'{prefix}_notional.png')

    return {'plots_dir': str(plots_path), 'plots': generated}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--npz', required=True)
    ap.add_argument('--symbol', default='')
    ap.add_argument('--limit-bars', type=int, default=0)
    ap.add_argument('--time-from', default='')
    ap.add_argument('--warmup-bars', type=int, default=0)
    ap.add_argument('--warmup-hours', type=float, default=0.0)
    ap.add_argument('--time-to', default='')
    ap.add_argument('--export-curves', default='')
    ap.add_argument('--plots', default='')
    ap.add_argument('--dynamic-slippage-json', default='')
    ap.add_argument('--initial-live-session', default='', help='session.sqlite to restore initial live state from')
    ap.add_argument('--initial-live-time', default='', help='state restore time; defaults to --time-from')
    ap.add_argument('--initial-live-mode', default='aggregate', choices=['aggregate', 'brief'])
    ap.add_argument('--initial-realized-pnl', type=float, default=None)
    args = ap.parse_args()

    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg, 'r', encoding='utf-8'))
    model_override = json.loads(args.dynamic_slippage_json) if args.dynamic_slippage_json else None
    data = np.load(args.npz, allow_pickle=True)
    market_symbol, ts_s, open_, high, low, close, volume, extras = pick_symbol_block(data, args.symbol)

    trade_start_ts_s = None
    if args.time_from:
        tf = parse_iso_to_epoch_s(args.time_from)
        trade_start_ts_s = int(tf)
        warmup_bars = max(0, int(args.warmup_bars or 0))
        if args.warmup_hours:
            try:
                bar_sec = float(np.median(np.diff(ts_s[:min(len(ts_s), 10000)]))) if len(ts_s) >= 2 else 60.0
                warmup_bars = max(warmup_bars, int(np.ceil(float(args.warmup_hours) * 3600.0 / max(1.0, bar_sec))))
            except Exception:
                pass
        start_ts = int(tf)
        if warmup_bars > 0 and len(ts_s) >= 2:
            try:
                bar_sec = float(np.median(np.diff(ts_s[:min(len(ts_s), 10000)])))
            except Exception:
                bar_sec = 60.0
            start_ts = int(tf - warmup_bars * max(1.0, bar_sec))
        m = ts_s >= start_ts
        ts_s, close = ts_s[m], close[m]
        open_ = open_[m] if open_ is not None else None
        high = high[m] if high is not None else None
        low = low[m] if low is not None else None
        volume = volume[m] if volume is not None else None
        extras = {k: v[m] for k, v in extras.items()}
    if args.time_to:
        tt = parse_iso_to_epoch_s(args.time_to)
        m = ts_s <= tt
        ts_s, close = ts_s[m], close[m]
        open_ = open_[m] if open_ is not None else None
        high = high[m] if high is not None else None
        low = low[m] if low is not None else None
        volume = volume[m] if volume is not None else None
        extras = {k: v[m] for k, v in extras.items()}
    if args.limit_bars and args.limit_bars > 0:
        ts_s, close = ts_s[-args.limit_bars:], close[-args.limit_bars:]
        open_ = open_[-args.limit_bars:] if open_ is not None else None
        high = high[-args.limit_bars:] if high is not None else None
        low = low[-args.limit_bars:] if low is not None else None
        volume = volume[-args.limit_bars:] if volume is not None else None
        extras = {k: v[-args.limit_bars:] for k, v in extras.items()}

    init_time_s = parse_iso_to_epoch_s(args.initial_live_time) if args.initial_live_time else (trade_start_ts_s or int(ts_s[0]))
    # The simulator restores state at `trade_start_ts_s`, so if user gave initial-live-time explicitly,
    # we set trade_start to it for consistent behavior.
    if args.initial_live_time:
        trade_start_ts_s = int(init_time_s)

    need_curves = bool(args.export_curves or args.plots)
    out = simulate_live_start(
        cfg, ts_s, close, open_=open_, high=high, low=low, volume=volume, extras=extras,
        market_symbol=market_symbol, model_override=model_override,
        export_curves=need_curves, trade_start_ts_s=trade_start_ts_s,
        initial_session_db=args.initial_live_session,
        initial_mode=args.initial_live_mode,
        initial_realized=args.initial_realized_pnl,
    )
    out['elapsed_sec'] = time.time() - t0
    curves = out.pop('curves', None)
    if curves is not None:
        if args.export_curves:
            Path(args.export_curves).parent.mkdir(parents=True, exist_ok=True)
            curves.to_csv(args.export_curves, index=False)
            out['curves_csv'] = args.export_curves
        if args.plots:
            out.update(save_plot_bundle(curves, args.plots, prefix='dual_live_start'))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
