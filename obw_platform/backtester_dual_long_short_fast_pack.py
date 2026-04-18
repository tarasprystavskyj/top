#!/usr/bin/env python3
from __future__ import annotations
import argparse, importlib, json, time, yaml
from dataclasses import dataclass
import numpy as np
import pandas as pd
from typing import Optional, Dict, Any

@dataclass
class Position:
    side: str
    entry: float
    qty: float


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


def unrealized(pos: Optional[Position], px: float, fee: float, slippage: float) -> float:
    if pos is None or pos.qty <= 0:
        return 0.0
    gross_ret = ((px - pos.entry) / max(pos.entry, 1e-12)) if pos.side == 'LONG' else ((pos.entry - px) / max(pos.entry, 1e-12))
    net_ret = gross_ret - 2 * slippage - 2 * fee
    return net_ret * (pos.entry * pos.qty)


def run_side(strat, sym: str, pos: Optional[Position], row: dict, fee: float, slippage: float, realized: float, trades: int, wins: int):
    px = float(row['close'])
    if pos is not None:
        ex = strat.manage_position(sym, row, pos, ctx=None)
        if ex and getattr(ex, 'action', None) in ('TP', 'SL', 'EXIT'):
            exit_px = float(getattr(ex, 'exit_price', px) or px)
            notional = pos.entry * pos.qty
            gross_ret = ((exit_px - pos.entry) / max(pos.entry,1e-12)) if pos.side == 'LONG' else ((pos.entry - exit_px) / max(pos.entry,1e-12))
            pnl = (gross_ret - 2*slippage - 2*fee) * notional
            realized += pnl; trades += 1; wins += 1 if pnl > 0 else 0
            pos = None
        elif ex and getattr(ex, 'action', None) == 'TP_PARTIAL':
            frac = max(0.0, min(1.0, float(getattr(ex, 'qty_frac', 0.0) or 0.0)))
            qty_close = pos.qty * frac
            if qty_close > 0:
                exit_px = float(getattr(ex, 'exit_price', px) or px)
                notional = pos.entry * qty_close
                gross_ret = ((exit_px - pos.entry) / max(pos.entry,1e-12)) if pos.side == 'LONG' else ((pos.entry - exit_px) / max(pos.entry,1e-12))
                pnl = (gross_ret - 2*slippage - 2*fee) * notional
                realized += pnl; trades += 1; wins += 1 if pnl > 0 else 0
                pos.qty -= qty_close
                if pos.qty <= 1e-12:
                    pos = None
    if pos is None:
        sig = strat.entry_signal(True, sym, row, ctx=None)
        if sig is not None:
            qty = getattr(sig, 'qty', None)
            if not isinstance(qty, (int, float)) or qty <= 0:
                first_usdt = float(getattr(strat, 'first_usdt', 0.0) or 0.0)
                qty = first_usdt / max(px, 1e-12)
            pos = Position(str(sig.side).upper(), px, float(qty))
    return pos, realized, trades, wins


def simulate(
    cfg: dict,
    ts_s: np.ndarray,
    close: np.ndarray,
    open_: Optional[np.ndarray] = None,
    high: Optional[np.ndarray] = None,
    low: Optional[np.ndarray] = None,
    volume: Optional[np.ndarray] = None,
    extras: Optional[Dict[str, np.ndarray]] = None,
    market_symbol: str = 'ENA/USDT:USDT',
):
    StratLong = import_by_path(cfg['strategy_class_long'])
    StratShort = import_by_path(cfg['strategy_class_short'])
    strat_long = StratLong(cfg)
    strat_short = StratShort(cfg)

    pf = cfg.get('portfolio', {}) or {}
    eq0_leg = float(pf.get('initial_equity_per_leg', 100.0))
    fee = float(pf.get('fee_rate', 0.0))
    slippage = float(pf.get('slippage_per_side', 0.0))
    max_notional_frac = float(pf.get('max_notional_frac', 1.0))
    equity_start_total = 2 * eq0_leg

    open_ = close if open_ is None else open_
    high = close if high is None else high
    low = close if low is None else low
    volume = np.zeros_like(close) if volume is None else volume
    extras = extras or {}

    pos_long = None
    pos_short = None
    realized_long = realized_short = 0.0
    trades_long = trades_short = wins_long = wins_short = 0
    eq_real=[]; eq_mtm=[]
    margin_call_events_total = bars_in_margin_call = 0
    prev_in_margin = False

    for i, ts in enumerate(ts_s):
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
            row[key] = float(arr[i]) if arr is not None else None

        pos_long, realized_long, trades_long, wins_long = run_side(strat_long, market_symbol, pos_long, row, fee, slippage, realized_long, trades_long, wins_long)
        pos_short, realized_short, trades_short, wins_short = run_side(strat_short, market_symbol, pos_short, row, fee, slippage, realized_short, trades_short, wins_short)
        px = float(close[i])
        eq_r = equity_start_total + realized_long + realized_short
        eq_u = eq_r + unrealized(pos_long, px, fee, slippage) + unrealized(pos_short, px, fee, slippage)
        gross_long = pos_long.entry * pos_long.qty if pos_long else 0.0
        gross_short = pos_short.entry * pos_short.qty if pos_short else 0.0
        effective = abs(gross_long - gross_short)
        allowed = max_notional_frac * max(eq_u, 0.0)
        in_margin = effective > allowed
        if in_margin: bars_in_margin_call += 1
        if in_margin and not prev_in_margin: margin_call_events_total += 1
        prev_in_margin = in_margin
        eq_real.append(eq_r); eq_mtm.append(eq_u)

    arr_r = np.asarray(eq_real, dtype=float)
    arr_m = np.asarray(eq_mtm, dtype=float)
    peaks_r = np.maximum.accumulate(arr_r) if len(arr_r) else np.asarray([], dtype=float)
    peaks_m = np.maximum.accumulate(arr_m) if len(arr_m) else np.asarray([], dtype=float)
    mdd_r = float(((arr_r - peaks_r) / peaks_r).min()) if len(arr_r) else 0.0
    mdd_m = float(((arr_m - peaks_m) / peaks_m).min()) if len(arr_m) else 0.0
    return {
        'equity_start_total': equity_start_total,
        'equity_end_realized_total': arr_r[-1] if len(arr_r) else equity_start_total,
        'equity_end_realized_long': eq0_leg + realized_long,
        'equity_end_realized_short': eq0_leg + realized_short,
        'realized_pnl_long': realized_long,
        'realized_pnl_short': realized_short,
        'realized_pnl_total': realized_long + realized_short,
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
    }


def pick_symbol_block(data, symbol_filter: str = ''):
    if 'symbols' not in data:
        ts_s = data['timestamp_s'].astype(np.int64)
        close = data['close'].astype(np.float64)
        open_ = data['open'].astype(np.float64) if 'open' in data else None
        high = data['high'].astype(np.float64) if 'high' in data else None
        low = data['low'].astype(np.float64) if 'low' in data else None
        volume = data['volume'].astype(np.float64) if 'volume' in data else None
        extras = {k: data[k].astype(np.float64) for k in ('trend_ma', 'trend_ma_prev', 'trend_slope_pct', 'trend_target_pct_long', 'trend_target_pct_short') if k in data}
        market_symbol = str(data['symbol']) if 'symbol' in data else 'ENA/USDT:USDT'
        return market_symbol, ts_s, open_, high, low, close, volume, extras

    symbols = [str(s) for s in data['symbols']]
    offsets = data['offsets'].astype(np.int64)
    if len(offsets) == len(symbols):
        offsets = np.concatenate([offsets, np.asarray([len(data['timestamp_s'])], dtype=np.int64)])
    if symbol_filter:
        try:
            idx = symbols.index(symbol_filter)
        except ValueError:
            raise SystemExit(f'symbol not found in NPZ: {symbol_filter}')
    else:
        if len(symbols) != 1:
            raise SystemExit(f'multi-symbol NPZ requires --symbol, found: {symbols[:10]}')
        idx = 0
    a, b = int(offsets[idx]), int(offsets[idx+1])
    market_symbol = symbols[idx]
    ts_s = data['timestamp_s'][a:b].astype(np.int64)
    close = data['close'][a:b].astype(np.float64)
    open_ = data['open'][a:b].astype(np.float64) if 'open' in data else None
    high = data['high'][a:b].astype(np.float64) if 'high' in data else None
    low = data['low'][a:b].astype(np.float64) if 'low' in data else None
    volume = data['volume'][a:b].astype(np.float64) if 'volume' in data else None
    extras = {k: data[k][a:b].astype(np.float64) for k in ('trend_ma', 'trend_ma_prev', 'trend_slope_pct', 'trend_target_pct_long', 'trend_target_pct_short') if k in data}
    return market_symbol, ts_s, open_, high, low, close, volume, extras


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--npz', required=True)
    ap.add_argument('--symbol', default='')
    ap.add_argument('--limit-bars', type=int, default=0)
    ap.add_argument('--time-from', default='')
    ap.add_argument('--time-to', default='')
    ap.add_argument('--debug', action='store_true', help='Verbose progress output')
    args = ap.parse_args()
    t0=time.time()
    cfg = yaml.safe_load(open(args.cfg, 'r'))
    data = np.load(args.npz, allow_pickle=True)
    market_symbol, ts_s, open_, high, low, close, volume, extras = pick_symbol_block(data, args.symbol)
    if args.debug:
        print(f'[cfg] symbol={market_symbol} bars={len(ts_s)} extras={sorted(extras.keys())}', flush=True)
    if args.time_from:
        tf = parse_iso_to_epoch_s(args.time_from)
        m=ts_s>=tf
        ts_s=ts_s[m]; close=close[m]
        open_ = open_[m] if open_ is not None else None
        high = high[m] if high is not None else None
        low = low[m] if low is not None else None
        volume = volume[m] if volume is not None else None
        extras = {k: v[m] for k, v in extras.items()}
    if args.time_to:
        tt = parse_iso_to_epoch_s(args.time_to)
        m=ts_s<=tt
        ts_s=ts_s[m]; close=close[m]
        open_ = open_[m] if open_ is not None else None
        high = high[m] if high is not None else None
        low = low[m] if low is not None else None
        volume = volume[m] if volume is not None else None
        extras = {k: v[m] for k, v in extras.items()}
    if args.limit_bars and args.limit_bars>0:
        ts_s=ts_s[-args.limit_bars:]; close=close[-args.limit_bars:]
        open_ = open_[-args.limit_bars:] if open_ is not None else None
        high = high[-args.limit_bars:] if high is not None else None
        low = low[-args.limit_bars:] if low is not None else None
        volume = volume[-args.limit_bars:] if volume is not None else None
        extras = {k: v[-args.limit_bars:] for k, v in extras.items()}
    if args.debug:
        print(f'[run] bars_after_filters={len(ts_s)}', flush=True)
    summary = simulate(cfg, ts_s, close, open_=open_, high=high, low=low, volume=volume, extras=extras, market_symbol=market_symbol)
    summary['elapsed_sec']=time.time()-t0
    print(json.dumps(summary, indent=2, default=str), flush=True)

if __name__ == '__main__':
    main()
