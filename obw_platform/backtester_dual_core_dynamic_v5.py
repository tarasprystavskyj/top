#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import math
import datetime as _dt
from dataclasses import dataclass
from typing import Any, Dict, Optional, Tuple

import numpy as np
import pandas as pd
from slippage_directional_model_v1 import predict_directional_slippage_bp


@dataclass
class Position:
    side: str
    entry: float
    qty: float


@dataclass
class SimBook:
    fee_rate: float
    maker_fee_rate: Optional[float] = None
    realized: float = 0.0
    realized_long: float = 0.0
    realized_short: float = 0.0
    lots_long: list = None
    lots_short: list = None

    def __post_init__(self):
        self.lots_long = [] if self.lots_long is None else self.lots_long
        self.lots_short = [] if self.lots_short is None else self.lots_short

    def _lots(self, side: str):
        return self.lots_long if str(side).upper() == 'LONG' else self.lots_short

    def _apply_realized_delta(self, side: str, delta: float) -> None:
        delta = float(delta)
        self.realized += delta
        if str(side).upper() == 'LONG':
            self.realized_long += delta
        else:
            self.realized_short += delta

    def open_fill(self, side: str, qty: float, exec_px: float, fee_rate: Optional[float] = None) -> None:
        self._lots(side).append([float(qty), float(exec_px)])
        fr = self.fee_rate if fee_rate is None else float(fee_rate)
        self._apply_realized_delta(side, -fr * float(exec_px) * float(qty))

    def close_fill(self, side: str, qty: float, exec_px: float) -> None:
        rem = float(qty)
        book = self._lots(side)
        pnl = 0.0
        fees = 0.0
        while rem > 1e-12 and book:
            lot_qty, entry_px = book[-1]
            take = min(lot_qty, rem)
            pnl += ((exec_px - entry_px) * take) if str(side).upper() == 'LONG' else ((entry_px - exec_px) * take)
            fees += self.fee_rate * float(exec_px) * take
            lot_qty -= take
            rem -= take
            if lot_qty <= 1e-12:
                book.pop()
            else:
                book[-1][0] = lot_qty
        self._apply_realized_delta(side, pnl - fees)

    def avg_entry(self, side: str) -> float:
        book = self._lots(side)
        if not book:
            return 0.0
        qty = sum(float(q) for q, _ in book)
        if qty <= 1e-12:
            return 0.0
        return sum(float(q) * float(px) for q, px in book) / qty

    def unrealized_side(self, side: str, mark_px: float) -> float:
        u = 0.0
        for qty, entry_px in self._lots(side):
            u += (mark_px - entry_px) * qty if str(side).upper() == 'LONG' else (entry_px - mark_px) * qty
        return float(u)

    def unrealized(self, mark_px: float) -> float:
        return float(self.unrealized_side('LONG', mark_px) + self.unrealized_side('SHORT', mark_px))


def import_by_path(path: str):
    mod_name, cls_name = path.rsplit('.', 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


def parse_iso_to_epoch_s(s: str) -> int:
    import datetime as _dt
    dt = _dt.datetime.fromisoformat(str(s).replace('Z', '+00:00'))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    else:
        dt = dt.astimezone(_dt.timezone.utc)
    return int(dt.timestamp())


def pick_symbol_block(data, symbol_filter: str = ''):
    files = set(getattr(data, 'files', []))

    if 'symbols' not in files:
        ts_s = data['timestamp_s'].astype(np.int64)
        close = data['close'].astype(np.float64)
        open_ = data['open'].astype(np.float64) if 'open' in files else None
        high = data['high'].astype(np.float64) if 'high' in files else None
        low = data['low'].astype(np.float64) if 'low' in files else None
        volume = data['volume'].astype(np.float64) if 'volume' in files else None
        extras = {
            k: data[k].astype(np.float64)
            for k in files
            if k not in {'symbol', 'symbols', 'offsets', 'timestamp_s', 'open', 'high', 'low', 'close', 'volume'}
        }

        market_symbol = symbol_filter or 'ENA/USDT:USDT'
        if 'symbol' in files:
            try:
                sym = data['symbol']
                market_symbol = str(sym.item() if hasattr(sym, 'item') else sym)
            except Exception:
                # Fallback keeps the run alive when symbol.npy was saved as
                # an incompatible pickled object array (e.g. NumPy 2.x -> 1.x).
                pass

        return market_symbol, ts_s, open_, high, low, close, volume, extras

    try:
        raw_symbols = data['symbols']
        symbols = [str(x.item() if hasattr(x, 'item') else x) for x in raw_symbols.tolist()]
    except Exception as e:
        raise SystemExit(
            "NPZ field 'symbols' was saved as a pickled object array with an incompatible NumPy version. "
            "Either upgrade NumPy in the venv to >=2,<3 or rebuild the NPZ with plain string symbols."
        ) from e

    offsets = data['offsets'].astype(np.int64)
    if len(offsets) == len(symbols):
        offsets = np.concatenate([offsets, np.asarray([len(data['timestamp_s'])], dtype=np.int64)])
    if symbol_filter:
        try:
            idx = symbols.index(symbol_filter)
        except ValueError as e:
            raise SystemExit(f'symbol not found in NPZ: {symbol_filter}') from e
    else:
        if len(symbols) != 1:
            raise SystemExit(f'multi-symbol NPZ requires --symbol, found: {symbols[:10]}')
        idx = 0
    a, b = int(offsets[idx]), int(offsets[idx + 1])
    market_symbol = symbols[idx]
    ts_s = data['timestamp_s'][a:b].astype(np.int64)
    close = data['close'][a:b].astype(np.float64)
    open_ = data['open'][a:b].astype(np.float64) if 'open' in files else None
    high = data['high'][a:b].astype(np.float64) if 'high' in files else None
    low = data['low'][a:b].astype(np.float64) if 'low' in files else None
    volume = data['volume'][a:b].astype(np.float64) if 'volume' in files else None
    extras = {
        k: data[k][a:b].astype(np.float64)
        for k in files
        if k not in {'symbol', 'symbols', 'offsets', 'timestamp_s', 'open', 'high', 'low', 'close', 'volume'}
    }
    return market_symbol, ts_s, open_, high, low, close, volume, extras


def build_row(ts: int, i: int, open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, extras: Dict[str, np.ndarray]) -> Dict[str, Any]:
    ts_i = int(ts)
    dt = _dt.datetime.fromtimestamp(ts_i, tz=_dt.timezone.utc)
    row: Dict[str, Any] = {
        'ts_s': ts_i,
        'timestamp_s': ts_i,
        'ts': ts_i,
        'datetime_utc': dt,
        'time': dt,
        'datetime': dt,
        'timestamp': ts_i,
        'open': float(open_[i]),
        'high': float(high[i]),
        'low': float(low[i]),
        'close': float(close[i]),
        'volume': float(volume[i]),
    }
    for key, arr in extras.items():
        try:
            row[key] = float(arr[i])
        except Exception:
            row[key] = arr[i]
    if 'quote_volume' not in row:
        row['quote_volume'] = row['close'] * row['volume']
    return row


def _load_dynamic_slippage(cfg: dict, model_override: Optional[dict] = None) -> dict:
    """Load exactly ONE slippage model for the backtest.

    Priority:
    1. CLI model_override, if provided.
    2. cfg.backtest.slippage section.
    3. legacy cfg.portfolio.dynamic_slippage_model.
    4. legacy cfg.portfolio.slippage_per_side as constant model.
    5. no slippage.

    Static and dynamic slippage are deliberately mutually exclusive here.
    They are not summed.
    """
    pf = (cfg.get('portfolio') or {})
    bt = (cfg.get('backtest') or {})
    sl = (bt.get('slippage') or {})

    if model_override:
        model = dict(model_override)
    elif sl:
        enabled = bool(sl.get('enabled', True))
        if not enabled:
            return {'kind': 'constant', 'base_bp': 0.0}
        mode = str(sl.get('mode', 'static')).lower().strip()
        if mode in ('none', 'off', 'disabled'):
            return {'kind': 'constant', 'base_bp': 0.0}
        if mode in ('static', 'constant'):
            base = float(sl.get('static_bp', sl.get('base_bp', pf.get('slippage_per_side', 0.0))) or 0.0)
            return {'kind': 'constant', 'base_bp': base * 10000.0 if base < 1 else base}
        if mode in ('dynamic', 'model'):
            model = dict(sl.get('model') or sl.get('dynamic_model') or {})
        else:
            model = dict(sl.get('model') or {})
    else:
        model = dict((pf.get('dynamic_slippage_model') or {}))

    if not model:
        base = float(pf.get('slippage_per_side', 0.0) or 0.0)
        return {'kind': 'constant', 'base_bp': base * 10000.0 if base < 1 else base}
    if str(model.get('kind','')) == 'directional_knn_linear':
        return model
    if 'base_bp' in model and 'coefficients' not in model:
        return model
    return {
        'kind': str(model.get('kind', 'linear_bp')),
        'base_bp': float(model.get('base_bp', 0.0)),
        'coefficients': {str(k): float(v) for k, v in (model.get('coefficients') or {}).items()},
        'clip_min_bp': float(model.get('clip_min_bp', 0.0)),
        'clip_max_bp': float(model.get('clip_max_bp', 1000.0)),
    }



def predict_adverse_slippage_bp(row: dict, side: str, action: str, qty: float, model: dict) -> float:
    kind = str(model.get('kind', 'constant'))
    if kind == 'directional_knn_linear':
        return float(predict_directional_slippage_bp(model, row, side, action, qty))
    if kind == 'constant':
        return max(0.0, float(model.get('base_bp', 0.0)))
    open_px = max(float(row.get('open', row.get('close', 0.0)) or 0.0), 1e-12)
    close_px = float(row.get('close', open_px) or open_px)
    high_px = float(row.get('high', close_px) or close_px)
    low_px = float(row.get('low', close_px) or close_px)
    volume = float(row.get('volume', 0.0) or 0.0)
    quote_volume = float(row.get('quote_volume', close_px * volume) or (close_px * volume))
    notional = float(qty) * close_px
    participation = notional / max(quote_volume, 1e-12)
    signed_body_bp = 10000.0 * (close_px - open_px) / open_px
    range_bp = 10000.0 * (high_px - low_px) / open_px
    features = {
        'log_volume': math.log1p(max(volume, 0.0)),
        'log_quote_volume': math.log1p(max(quote_volume, 0.0)),
        'signed_body_bp': signed_body_bp,
        'range_bp': range_bp,
        'participation': participation,
        'log_participation': math.log(max(participation, 1e-12)),
        'side_long': 1.0 if str(side).upper() == 'LONG' else 0.0,
        'side_short': 1.0 if str(side).upper() == 'SHORT' else 0.0,
        'is_open': 1.0 if str(action).upper() == 'OPEN' else 0.0,
        'is_exit': 0.0 if str(action).upper() == 'OPEN' else 1.0,
        'side_x_body_signed_bp': signed_body_bp * (1.0 if str(side).upper() == 'LONG' else -1.0),
        'side_x_range_bp': range_bp * (1.0 if str(side).upper() == 'LONG' else -1.0),
    }
    val = float(model.get('base_bp', 0.0))
    for k, w in (model.get('coefficients') or {}).items():
        val += float(w) * float(features.get(k, 0.0))
    return float(np.clip(val, float(model.get('clip_min_bp', 0.0)), float(model.get('clip_max_bp', 1000.0))))


def execution_price(close_px: float, side: str, action: str, slip_bp: float) -> float:
    s = float(slip_bp) / 10000.0
    if str(action).upper() == 'OPEN':
        return float(close_px) * (1.0 + s) if str(side).upper() == 'LONG' else float(close_px) * (1.0 - s)
    return float(close_px) * (1.0 - s) if str(side).upper() == 'LONG' else float(close_px) * (1.0 + s)


def _strategy_snapshot(strat, sym: str):
    if hasattr(strat, 'export_state_snapshot'):
        try:
            return strat.export_state_snapshot(sym)
        except Exception:
            pass
    try:
        import copy as _copy
        return _copy.deepcopy(getattr(strat, '_states', {}).get(sym))
    except Exception:
        return None


def _strategy_restore(strat, sym: str, snapshot):
    if hasattr(strat, 'restore_state_snapshot'):
        try:
            strat.restore_state_snapshot(sym, snapshot)
            return
        except Exception:
            pass
    try:
        import copy as _copy
        states = getattr(strat, '_states', None)
        if isinstance(states, dict):
            if snapshot is None:
                states.pop(sym, None)
            else:
                states[sym] = _copy.deepcopy(snapshot)
    except Exception:
        pass


def _snapshot_limit_price(snapshot, fallback=None):
    try:
        if isinstance(snapshot, dict) and snapshot.get('next_level_price') not in (None, ''):
            return float(snapshot.get('next_level_price'))
    except Exception:
        pass
    try:
        if snapshot is not None and getattr(snapshot, 'next_level_price', None) not in (None, ''):
            return float(getattr(snapshot, 'next_level_price'))
    except Exception:
        pass
    try:
        return float(fallback) if fallback is not None else None
    except Exception:
        return None



def _bt_debug_close_reasons(cfg: dict) -> bool:
    """Return True only when detailed close reasons should be included in JSON output.

    Detailed reasons can be enormous because strict LIFO/BE reasons include entry,
    min_allowed and qty. Keep them in debug mode only.
    """
    bt = (cfg.get('backtest') or {})
    return bool(
        bt.get('debug', False)
        or bt.get('debug_close_reasons', False)
        or bt.get('include_close_reason_counts', False)
    )


def _reason_bucket(reason: str) -> str:
    """Compact reason bucket safe for normal output."""
    r = str(reason or '')
    if 'Sub-sell last lot' in r:
        if 'mode=normal_lot_tp_floor' in r:
            return 'Sub-sell last lot | normal_lot_tp_floor'
        if 'mode=deleverage_breakeven' in r:
            return 'Sub-sell last lot | deleverage_breakeven'
        return 'Sub-sell last lot'
    if 'Sub-cover last lot' in r:
        if 'mode=normal_lot_tp_floor' in r:
            return 'Sub-cover last lot | normal_lot_tp_floor'
        if 'mode=deleverage_breakeven' in r:
            return 'Sub-cover last lot | deleverage_breakeven'
        return 'Sub-cover last lot'
    if 'TP Full' in r:
        return 'TP Full'
    if 'SL' in r:
        return 'SL'
    if 'EXIT' in r:
        return 'EXIT'
    return r[:80]


def _record_close_reason(reason_counts: dict, reason_summary: dict, reason: str, *, debug_close_reasons: bool) -> None:
    bucket = _reason_bucket(reason)
    reason_summary[bucket] = int(reason_summary.get(bucket, 0)) + 1
    if debug_close_reasons:
        reason_counts[str(reason)] = int(reason_counts.get(str(reason), 0)) + 1


def _dca_open_order_type(cfg: dict) -> str:
    for path in ('backtest.dca_open_order_type', 'live.dca_open_order_type', 'runner.dca_open_order_type', 'dca_open_order_type'):
        cur = cfg
        ok = True
        for part in path.split('.'):
            if not isinstance(cur, dict) or part not in cur:
                ok = False
                break
            cur = cur[part]
        if ok:
            return str(cur or 'market').lower().strip()
    return 'market'


def simulate(cfg: dict, ts_s: np.ndarray, close: np.ndarray, open_: Optional[np.ndarray] = None, high: Optional[np.ndarray] = None, low: Optional[np.ndarray] = None, volume: Optional[np.ndarray] = None, extras: Optional[Dict[str, np.ndarray]] = None, market_symbol: str = 'ENA/USDT:USDT', model_override: Optional[dict] = None, export_curves: bool = False, trade_start_ts_s: Optional[int] = None):
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
    bt_cfg = (cfg.get('backtest') or {})
    use_live_sync = bool(bt_cfg.get('use_live_sync', False))
    debug_close_reasons = _bt_debug_close_reasons(cfg)
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
                fn(market_symbol, [row], ctx={'source': 'backtester_prewarm'})
            except Exception:
                pass

    pos_long = None
    pos_short = None
    book = SimBook(fee_rate=fee, maker_fee_rate=maker_fee)
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
    close_reason_summary = {}
    margin_call_events_total = bars_in_margin_call = 0
    prev_in_margin = False

    for i, ts in enumerate(ts_s):
        row = build_row(ts, i, open_, high, low, close, volume, extras)
        px = float(row['close'])

        if trade_start_ts_s is not None and int(ts) < int(trade_start_ts_s):
            _warm_strategy(strat_long, row)
            _warm_strategy(strat_short, row)
            warmup_bars_seen += 1
            continue

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
                if use_live_sync and hasattr(strat_long, 'sync_after_external_fill'):
                    strat_long.sync_after_external_fill(market_symbol, qty=0.0, entry=0.0, fill_price=exit_px, delta_qty=qty_close, event='close')
                pnl_trade = book.realized - before_real
                trades_long += 1
                wins_long += 1 if pnl_trade > 0 else 0
                event_counts['close_long'] += 1
                _reason = str(getattr(ex, 'reason', getattr(ex, 'action', 'close_long')) or getattr(ex, 'action', 'close_long'))
                _record_close_reason(close_reason_counts, close_reason_summary, _reason, debug_close_reasons=debug_close_reasons)
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
                    if use_live_sync and hasattr(strat_long, 'sync_after_external_fill'):
                        strat_long.sync_after_external_fill(market_symbol, qty=rem_qty, entry=rem_entry, fill_price=exit_px, delta_qty=qty_close, event='partial')
                    pnl_trade = book.realized - before_real
                    trades_long += 1
                    wins_long += 1 if pnl_trade > 0 else 0
                    event_counts['partial_long'] += 1
                    _reason = str(getattr(ex, 'reason', 'partial_long') or 'partial_long')
                    _record_close_reason(close_reason_counts, close_reason_summary, _reason, debug_close_reasons=debug_close_reasons)
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
                        if use_live_sync and hasattr(strat_long, 'sync_after_external_fill'):
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
                    if use_live_sync and hasattr(strat_long, 'sync_after_external_fill'):
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
                if use_live_sync and hasattr(strat_short, 'sync_after_external_fill'):
                    strat_short.sync_after_external_fill(market_symbol, qty=0.0, entry=0.0, fill_price=exit_px, delta_qty=qty_close, event='close')
                pnl_trade = book.realized - before_real
                trades_short += 1
                wins_short += 1 if pnl_trade > 0 else 0
                event_counts['close_short'] += 1
                _reason = str(getattr(ex, 'reason', getattr(ex, 'action', 'close_short')) or getattr(ex, 'action', 'close_short'))
                _record_close_reason(close_reason_counts, close_reason_summary, _reason, debug_close_reasons=debug_close_reasons)
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
                    if use_live_sync and hasattr(strat_short, 'sync_after_external_fill'):
                        strat_short.sync_after_external_fill(market_symbol, qty=rem_qty, entry=rem_entry, fill_price=exit_px, delta_qty=qty_close, event='partial')
                    pnl_trade = book.realized - before_real
                    trades_short += 1
                    wins_short += 1 if pnl_trade > 0 else 0
                    event_counts['partial_short'] += 1
                    _reason = str(getattr(ex, 'reason', 'partial_short') or 'partial_short')
                    _record_close_reason(close_reason_counts, close_reason_summary, _reason, debug_close_reasons=debug_close_reasons)
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
                        if use_live_sync and hasattr(strat_short, 'sync_after_external_fill'):
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
                    if use_live_sync and hasattr(strat_short, 'sync_after_external_fill'):
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
                            if use_live_sync and hasattr(strat_long, 'sync_after_external_fill'):
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
                        if use_live_sync and hasattr(strat_long, 'sync_after_external_fill'):
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
                            if use_live_sync and hasattr(strat_short, 'sync_after_external_fill'):
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
                        if use_live_sync and hasattr(strat_short, 'sync_after_external_fill'):
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

    arr_r = np.asarray(eq_real, dtype=float)
    arr_m = np.asarray(eq_mtm, dtype=float)
    peaks_r = np.maximum.accumulate(arr_r) if len(arr_r) else np.asarray([], dtype=float)
    peaks_m = np.maximum.accumulate(arr_m) if len(arr_m) else np.asarray([], dtype=float)
    mdd_r = float(((arr_r - peaks_r) / peaks_r).min()) if len(arr_r) else 0.0
    mdd_m = float(((arr_m - peaks_m) / peaks_m).min()) if len(arr_m) else 0.0
    final_mark_px = float(close[-1]) if len(close) else 0.0
    final_unrealized_long = book.unrealized_side('LONG', final_mark_px)
    final_unrealized_short = book.unrealized_side('SHORT', final_mark_px)
    final_unrealized_total = final_unrealized_long + final_unrealized_short
    final_total_pnl_mtm = book.realized + final_unrealized_total
    final_equity_mtm_total = equity_start_total + final_total_pnl_mtm

    out = {
        'equity_start_total': equity_start_total,
        'equity_end_realized_total': arr_r[-1] if len(arr_r) else equity_start_total,
        'equity_end_mtm_total': final_equity_mtm_total,
        'equity_end_realized_long': eq0_leg + book.realized_long,
        'equity_end_realized_short': eq0_leg + book.realized_short,
        'realized_pnl_long': book.realized_long,
        'realized_pnl_short': book.realized_short,
        'realized_pnl_total': book.realized,
        'unrealized_pnl_long': final_unrealized_long,
        'unrealized_pnl_short': final_unrealized_short,
        'unrealized_pnl_total': final_unrealized_total,
        'total_pnl_mtm': final_total_pnl_mtm,
        'total_pnl': final_total_pnl_mtm,
        'return_mtm_pct_on_start': final_total_pnl_mtm * 100.0 / max(equity_start_total, 1e-12),
        'terminal_unrealized_to_realized_ratio': final_unrealized_total / max(abs(book.realized), 1e-12),
        'final_mark_px': final_mark_px,
        'trades_long': trades_long,
        'trades_short': trades_short,
        'trades_total': trades_long + trades_short,
        'win_rate_long_%': wins_long * 100.0 / max(1, trades_long) if trades_long else 0.0,
        'win_rate_short_%': wins_short * 100.0 / max(1, trades_short) if trades_short else 0.0,
        'mdd_mtm_frac': mdd_m,
        'mdd_mtm_%': mdd_m * 100.0,
        'mdd_realized_frac': mdd_r,
        'mdd_realized_%': mdd_r * 100.0,
        'margin_call_events_total': margin_call_events_total,
        'bars_in_margin_call': bars_in_margin_call,
        'dynamic_slippage_model': slip_model,
        'backtest_use_live_sync': bool(use_live_sync),
        'backtest_slippage_config': (cfg.get('backtest') or {}).get('slippage', None),
        'maker_fee_rate': maker_fee,
        'warmup_bars_seen': int(warmup_bars_seen),
        'trade_start_ts_s': int(trade_start_ts_s) if trade_start_ts_s is not None else None,
        'order_event_counts': event_counts,
        # Compact buckets are always safe to print.
        'close_reason_summary': close_reason_summary,
        # Full high-cardinality reasons are debug-only.
        'close_reason_counts_debug_enabled': bool(debug_close_reasons),
        'total_order_events': int(sum(event_counts.values())),
    }
    if debug_close_reasons:
        out['close_reason_counts'] = close_reason_counts
    if export_curves:
        df_curves = pd.DataFrame(curve_rows)
        if not df_curves.empty and 'bar_ts_s' in df_curves.columns:
            df_curves['bar_ts'] = pd.to_datetime(df_curves['bar_ts_s'], unit='s', utc=True)
            cols = ['bar_ts'] + [c for c in df_curves.columns if c != 'bar_ts']
            df_curves = df_curves[cols]
        out['curves'] = df_curves
    return out
