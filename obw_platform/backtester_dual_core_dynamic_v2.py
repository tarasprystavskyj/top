#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import math
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

    def open_fill(self, side: str, qty: float, exec_px: float) -> None:
        self._lots(side).append([float(qty), float(exec_px)])
        self._apply_realized_delta(side, -self.fee_rate * float(exec_px) * float(qty))

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
    if 'symbols' not in data:
        ts_s = data['timestamp_s'].astype(np.int64)
        close = data['close'].astype(np.float64)
        open_ = data['open'].astype(np.float64) if 'open' in data else None
        high = data['high'].astype(np.float64) if 'high' in data else None
        low = data['low'].astype(np.float64) if 'low' in data else None
        volume = data['volume'].astype(np.float64) if 'volume' in data else None
        extras = {k: data[k].astype(np.float64) for k in data.files if k not in {'symbol','timestamp_s','open','high','low','close','volume'}}
        market_symbol = str(data['symbol']) if 'symbol' in data else 'ENA/USDT:USDT'
        return market_symbol, ts_s, open_, high, low, close, volume, extras

    symbols = [str(s) for s in data['symbols']]
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
    open_ = data['open'][a:b].astype(np.float64) if 'open' in data else None
    high = data['high'][a:b].astype(np.float64) if 'high' in data else None
    low = data['low'][a:b].astype(np.float64) if 'low' in data else None
    volume = data['volume'][a:b].astype(np.float64) if 'volume' in data else None
    extras = {k: data[k][a:b].astype(np.float64) for k in data.files if k not in {'symbols','offsets','timestamp_s','open','high','low','close','volume'}}
    return market_symbol, ts_s, open_, high, low, close, volume, extras


def build_row(ts: int, i: int, open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, volume: np.ndarray, extras: Dict[str, np.ndarray]) -> Dict[str, Any]:
    iso = pd.to_datetime(int(ts), unit='s', utc=True).strftime('%Y-%m-%dT%H:%M:%S+00:00')
    row: Dict[str, Any] = {
        'datetime_utc': iso,
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
    pf = (cfg.get('portfolio') or {})
    model = dict((pf.get('dynamic_slippage_model') or {}))
    if model_override:
        model.update(model_override)
    if not model:
        base = float(pf.get('slippage_per_side', 0.0) or 0.0)
        return {
            'kind': 'constant',
            'base_bp': base * 10000.0 if base < 1 else base,
        }
    if str(model.get('kind','')) == 'directional_knn_linear':
        return model
    if 'base_bp' in model and 'coefficients' not in model:
        return model
    return {
        'kind': 'linear_bp',
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


def simulate(cfg: dict, ts_s: np.ndarray, close: np.ndarray, open_: Optional[np.ndarray] = None, high: Optional[np.ndarray] = None, low: Optional[np.ndarray] = None, volume: Optional[np.ndarray] = None, extras: Optional[Dict[str, np.ndarray]] = None, market_symbol: str = 'ENA/USDT:USDT', model_override: Optional[dict] = None, export_curves: bool = False):
    StratLong = import_by_path(cfg['strategy_class_long'])
    StratShort = import_by_path(cfg['strategy_class_short'])
    strat_long = StratLong(cfg)
    strat_short = StratShort(cfg)

    pf = cfg.get('portfolio', {}) or {}
    eq0_leg = float(pf.get('initial_equity_per_leg', 100.0))
    fee = float(pf.get('fee_rate', 0.0))
    max_notional_frac = float(pf.get('max_notional_frac', 1.0))
    equity_start_total = 2 * eq0_leg
    slip_model = _load_dynamic_slippage(cfg, model_override=model_override)

    open_ = close if open_ is None else open_
    high = close if high is None else high
    low = close if low is None else low
    volume = np.zeros_like(close) if volume is None else volume
    extras = extras or {}

    pos_long = None
    pos_short = None
    book = SimBook(fee_rate=fee)
    trades_long = trades_short = wins_long = wins_short = 0
    eq_real = []
    eq_mtm = []
    curve_rows = []
    margin_call_events_total = bars_in_margin_call = 0
    prev_in_margin = False

    for i, ts in enumerate(ts_s):
        row = build_row(ts, i, open_, high, low, close, volume, extras)
        px = float(row['close'])

        # LONG manage
        if pos_long is not None:
            before_qty = pos_long.qty
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
                    if pos_long.qty <= 1e-12:
                        pos_long = None
            elif pos_long is not None and pos_long.qty > before_qty + 1e-12:
                add_qty = pos_long.qty - before_qty
                slip_bp = predict_adverse_slippage_bp(row, 'LONG', 'OPEN', add_qty, slip_model)
                exec_px = execution_price(px, 'LONG', 'OPEN', slip_bp)
                book.open_fill('LONG', add_qty, exec_px)
                if hasattr(strat_long, 'sync_after_external_fill'):
                    strat_long.sync_after_external_fill(market_symbol, qty=pos_long.qty, entry=book.avg_entry('LONG'), fill_price=exec_px, delta_qty=add_qty, event='dca')

        # SHORT manage
        if pos_short is not None:
            before_qty = pos_short.qty
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
                    if pos_short.qty <= 1e-12:
                        pos_short = None
            elif pos_short is not None and pos_short.qty > before_qty + 1e-12:
                add_qty = pos_short.qty - before_qty
                slip_bp = predict_adverse_slippage_bp(row, 'SHORT', 'OPEN', add_qty, slip_model)
                exec_px = execution_price(px, 'SHORT', 'OPEN', slip_bp)
                book.open_fill('SHORT', add_qty, exec_px)
                if hasattr(strat_short, 'sync_after_external_fill'):
                    strat_short.sync_after_external_fill(market_symbol, qty=pos_short.qty, entry=book.avg_entry('SHORT'), fill_price=exec_px, delta_qty=add_qty, event='dca')

        # LONG entry
        if pos_long is None:
            sig = strat_long.entry_signal(True, market_symbol, row, ctx=None)
            if sig is not None:
                qty = float(getattr(sig, 'qty', 0.0) or 0.0)
                if qty > 0:
                    slip_bp = predict_adverse_slippage_bp(row, 'LONG', 'OPEN', qty, slip_model)
                    exec_px = execution_price(px, 'LONG', 'OPEN', slip_bp)
                    book.open_fill('LONG', qty, exec_px)
                    pos_long = Position('LONG', px, qty)
                    if hasattr(strat_long, 'sync_after_external_fill'):
                        strat_long.sync_after_external_fill(market_symbol, qty=qty, entry=book.avg_entry('LONG'), fill_price=exec_px, delta_qty=qty, event='open')

        # SHORT entry
        if pos_short is None:
            sig = strat_short.entry_signal(True, market_symbol, row, ctx=None)
            if sig is not None:
                qty = float(getattr(sig, 'qty', 0.0) or 0.0)
                if qty > 0:
                    slip_bp = predict_adverse_slippage_bp(row, 'SHORT', 'OPEN', qty, slip_model)
                    exec_px = execution_price(px, 'SHORT', 'OPEN', slip_bp)
                    book.open_fill('SHORT', qty, exec_px)
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
                'bar_ts': pd.to_datetime(int(ts), unit='s', utc=True),
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
    out = {
        'equity_start_total': equity_start_total,
        'equity_end_realized_total': arr_r[-1] if len(arr_r) else equity_start_total,
        'equity_end_realized_long': eq0_leg + book.realized_long,
        'equity_end_realized_short': eq0_leg + book.realized_short,
        'realized_pnl_long': book.realized_long,
        'realized_pnl_short': book.realized_short,
        'realized_pnl_total': book.realized,
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
    }
    if export_curves:
        out['curves'] = pd.DataFrame(curve_rows)
    return out
