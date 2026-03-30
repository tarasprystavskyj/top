#!/usr/bin/env python3
import argparse, sqlite3, importlib, time, sys, os, pathlib as _p, shutil, json
from dataclasses import dataclass
from typing import Dict, Any, List, Tuple
import yaml
import pandas as pd
import datetime as _dt


def import_by_path(path: str):
    mod_name, cls_name = path.rsplit('.', 1)
    root = str((_p.Path(__file__).parent).resolve())
    if root not in sys.path:
        sys.path.insert(0, root)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


def _db_connect(path: str):
    con = sqlite3.connect(path)
    con.row_factory = sqlite3.Row
    return con


@dataclass
class Position:
    side: str
    entry: float
    sl: float
    tp: float
    qty: float


def find_db_file(filename: str):
    filename = filename.strip()
    if os.path.isabs(filename) and os.path.exists(filename):
        return os.path.abspath(filename)
    if os.path.dirname(filename) and os.path.exists(filename):
        return os.path.abspath(filename)
    search_paths = [os.path.join('.', filename), os.path.join('.', 'DB', filename), os.path.join('..', filename), os.path.join('..', 'DB', filename)]
    for path in search_paths:
        if os.path.exists(path):
            return os.path.abspath(path)
    raise FileNotFoundError(f"DB file '{filename}' not found in {search_paths}")


def _norm_iso(ts: str):
    if not ts:
        return ts
    try:
        return _dt.datetime.fromisoformat(ts.replace('Z', '+00:00')).isoformat()
    except Exception:
        return ts


def _compute_unrealized(positions: Dict[str, Position], px_map: Dict[str, float], fee: float, slippage: float) -> float:
    unreal = 0.0
    for sym, pos in positions.items():
        px = px_map.get(sym)
        if px is None:
            continue
        if pos.side == 'LONG':
            gross_ret = (px - pos.entry) / max(pos.entry, 1e-12)
        else:
            gross_ret = (pos.entry - px) / max(pos.entry, 1e-12)
        net_ret = gross_ret - 2 * slippage - 2 * fee
        unreal += net_ret * (pos.entry * pos.qty)
    return float(unreal)


def _gross_notional(positions: Dict[str, Position]) -> float:
    return float(sum(p.entry * p.qty for p in positions.values()))


def _signed_notional_map(long_pos: Dict[str, Position], short_pos: Dict[str, Position]) -> Dict[str, float]:
    out: Dict[str, float] = {}
    for sym, pos in long_pos.items():
        out[sym] = out.get(sym, 0.0) + pos.entry * pos.qty
    for sym, pos in short_pos.items():
        out[sym] = out.get(sym, 0.0) - pos.entry * pos.qty
    return out


def _effective_notional(long_pos: Dict[str, Position], short_pos: Dict[str, Position]) -> float:
    signed = _signed_notional_map(long_pos, short_pos)
    return float(sum(abs(v) for v in signed.values()))


def _prospective_effective_notional(long_pos: Dict[str, Position], short_pos: Dict[str, Position], sym: str, delta_notional: float) -> float:
    signed = _signed_notional_map(long_pos, short_pos)
    signed[sym] = signed.get(sym, 0.0) + delta_notional
    return float(sum(abs(v) for v in signed.values()))


def _timeframe_to_minutes(tf: str) -> float:
    s = str(tf).strip().lower()
    if s.endswith('m'):
        return float(s[:-1])
    if s.endswith('h'):
        return float(s[:-1]) * 60.0
    if s.endswith('d'):
        return float(s[:-1]) * 1440.0
    return float(s)


def _annualized_returns(equity_end: float, equity_start: float, total_days: float) -> Tuple[float, float, float]:
    if equity_start <= 0 or total_days <= 0 or equity_end <= 0:
        return 0.0, 0.0, 0.0
    total_return = equity_end / equity_start
    if total_return <= 0:
        return 0.0, 0.0, 0.0
    daily_ret = total_return ** (1.0 / total_days) - 1.0
    monthly_ret = total_return ** (30.0 / total_days) - 1.0
    yearly_ret = total_return ** (365.0 / total_days) - 1.0
    return float(daily_ret), float(monthly_ret), float(yearly_ret)


def _max_drawdown(eq_curve: List[float]) -> Tuple[float, float]:
    import numpy as _np
    arr = _np.array(eq_curve, dtype=float)
    if arr.size < 2:
        return 0.0, 0.0
    peaks = _np.maximum.accumulate(arr)
    dd_arr = (arr - peaks) / peaks
    mdd_frac = float(dd_arr.min())
    return mdd_frac, mdd_frac * 100.0


def main():
    ap = argparse.ArgumentParser(description='Dual long+short backtester with shared margin, MDD, CAGR-like returns, and margin-call diagnostics')
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--limit-bars', type=int, default=500)
    ap.add_argument('--time-from', dest='time_from', type=str, default=None)
    ap.add_argument('--time-to', dest='time_to', type=str, default=None)
    ap.add_argument('--cache_db', dest='cache_db')
    ap.add_argument('--plots', dest='plots_dir', type=str, default=None)
    args = ap.parse_args()

    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg, 'r'))
    cache_db = args.cache_db or cfg.get('cache_db') or cfg.get('cache_db_path')
    if not cache_db:
        raise KeyError('cfg must include cache_db or cache_db_path (or pass --cache_db)')
    db_file = find_db_file(cache_db)
    con = _db_connect(db_file)

    t_from = _norm_iso(getattr(args, 'time_from', None))
    t_to = _norm_iso(getattr(args, 'time_to', None))
    if t_from or t_to:
        q = ['SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h FROM price_indicators WHERE 1=1']
        params = []
        if t_from:
            q.append('AND datetime_utc >= ?'); params.append(t_from)
        if t_to:
            q.append('AND datetime_utc <= ?'); params.append(t_to)
        q.append('ORDER BY datetime_utc ASC, symbol ASC')
        rows = con.execute(' '.join(q), params).fetchall()
    else:
        th_row = con.execute('SELECT MIN(datetime_utc) FROM (SELECT DISTINCT datetime_utc FROM price_indicators ORDER BY datetime_utc DESC LIMIT ?)', (int(args.limit_bars),)).fetchone()
        if not th_row or not th_row[0]:
            raise RuntimeError('No bars.')
        min_time = th_row[0]
        rows = con.execute('SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h FROM price_indicators WHERE datetime_utc >= ? ORDER BY datetime_utc ASC, symbol ASC', (min_time,)).fetchall()
    if not rows:
        raise RuntimeError('No bars.')
    print(f"[time range] {rows[0]['datetime_utc']} -> {rows[-1]['datetime_utc']}")

    slices = []
    cur_t, bucket = None, []
    for r in rows:
        t = r['datetime_utc']
        if cur_t is None:
            cur_t = t
        if t != cur_t:
            slices.append((cur_t, bucket)); bucket = []; cur_t = t
        bucket.append((r['symbol'], float(r['close'] or 0.0), float(r['atr_ratio'] or 0.0), float(r['dp6h'] or 0.0), float(r['dp12h'] or 0.0), float(r['quote_volume'] or 0.0), float(r['qv_24h'] or 0.0)))
    if bucket:
        slices.append((cur_t, bucket))

    StratLong = import_by_path(cfg['strategy_class_long'])
    StratShort = import_by_path(cfg['strategy_class_short'])
    strat_long = StratLong(cfg)
    strat_short = StratShort(cfg)

    portfolio = cfg.get('portfolio', {})
    initial_equity_total = float(portfolio.get('initial_equity_total', portfolio.get('initial_equity_per_leg', 100.0) * 2.0))
    pos_notional_long = float(portfolio.get('position_notional_long', portfolio.get('position_notional', 5.0)))
    pos_notional_short = float(portfolio.get('position_notional_short', portfolio.get('position_notional', 5.0)))
    fee = float(portfolio.get('fee_rate', 0.0))
    slippage = float(portfolio.get('slippage_per_side', 0.0))
    max_notional_frac = float(portfolio.get('max_notional_frac', 1.0))

    first_buy = float(cfg.get('strategy_params_long', {}).get('firstBuyUSDT', pos_notional_long) or 0.0)
    first_sell = float(cfg.get('strategy_params_short', {}).get('firstSellUSDT', pos_notional_short) or 0.0)

    positions_long: Dict[str, Position] = {}
    positions_short: Dict[str, Position] = {}
    pos_time_long: Dict[str, str] = {}
    pos_time_short: Dict[str, str] = {}

    realized_long = 0.0
    realized_short = 0.0
    fees_long = 0.0
    fees_short = 0.0
    trades_long = 0
    trades_short = 0
    wins_long = 0
    wins_short = 0

    tr_rows: List[Dict[str, Any]] = []
    ts_list: List[str] = []
    long_real_pnl_list: List[float] = []
    short_real_pnl_list: List[float] = []
    total_real_pnl_list: List[float] = []
    long_mtm_pnl_list: List[float] = []
    short_mtm_pnl_list: List[float] = []
    total_mtm_pnl_list: List[float] = []
    total_equity_mtm_list: List[float] = []
    total_equity_realized_list: List[float] = []
    effective_notional_list: List[float] = []
    gross_notional_total_list: List[float] = []
    gross_notional_long_list: List[float] = []
    gross_notional_short_list: List[float] = []
    margin_call_excess_list: List[float] = []
    margin_call_events_total = 0
    bars_in_margin_call = 0
    prev_in_margin_call = False

    max_gross_long = 0.0
    max_gross_short = 0.0
    max_gross_total = 0.0
    max_effective = 0.0

    for t, bucket_all in slices:
        px_map = {sym: close for (sym, close, *_rest) in bucket_all}
        md_map_all = {sym: {'close': close, 'atr_ratio': atr, 'dp6h': dp6, 'dp12h': dp12, 'quote_volume': qv1h, 'qv_24h': qv24, 'datetime_utc': t}
                     for (sym, close, atr, dp6, dp12, qv1h, qv24) in bucket_all}

        # --- exits long ---
        for sym, pos in list(positions_long.items()):
            row = md_map_all.get(sym)
            if row is None:
                continue
            ex = strat_long.manage_position(sym, row, pos, ctx=None)
            if ex and ex.action in ('TP', 'SL', 'EXIT'):
                px = float(ex.exit_price if ex.exit_price is not None else row['close'])
                notional = pos.entry * pos.qty
                gross_ret = (px - pos.entry) / max(pos.entry, 1e-12)
                net_ret = gross_ret - 2 * slippage - 2 * fee
                pnl_amt = net_ret * notional
                realized_long += pnl_amt
                fees_long += fee * 2 * notional
                trades_long += 1
                if pnl_amt > 0:
                    wins_long += 1
                tr_rows.append({'leg': 'LONG', 'symbol': sym, 'side': pos.side, 'entry_time': pos_time_long.get(sym, t), 'exit_time': t, 'entry': pos.entry, 'exit': px, 'action': ex.action, 'reason': ex.reason or ex.action, 'notional': notional, 'realized_pnl': pnl_amt, 'unrealized_pnl': 0.0})
                del positions_long[sym]; pos_time_long.pop(sym, None)
            elif ex and ex.action == 'TP_PARTIAL':
                px = float(ex.exit_price if ex.exit_price is not None else row['close'])
                part = max(0.0, min(1.0, float(getattr(ex, 'qty_frac', 0.5))))
                qty_close = pos.qty * part
                notional_entry = qty_close * pos.entry
                gross_ret = (px - pos.entry) / max(pos.entry, 1e-12)
                net_ret = gross_ret - 2 * slippage - 2 * fee
                pnl_amt = net_ret * notional_entry
                realized_long += pnl_amt
                fees_long += fee * 2 * notional_entry
                trades_long += 1
                if pnl_amt > 0:
                    wins_long += 1
                tr_rows.append({'leg': 'LONG', 'symbol': sym, 'side': pos.side, 'entry_time': pos_time_long.get(sym, t), 'exit_time': t, 'entry': pos.entry, 'exit': px, 'action': 'TP_PARTIAL', 'reason': getattr(ex, 'reason', 'TP_PARTIAL'), 'notional': notional_entry, 'realized_pnl': pnl_amt, 'unrealized_pnl': 0.0})
                pos.qty -= qty_close

        # --- exits short ---
        for sym, pos in list(positions_short.items()):
            row = md_map_all.get(sym)
            if row is None:
                continue
            ex = strat_short.manage_position(sym, row, pos, ctx=None)
            if ex and ex.action in ('TP', 'SL', 'EXIT'):
                px = float(ex.exit_price if ex.exit_price is not None else row['close'])
                notional = pos.entry * pos.qty
                gross_ret = (pos.entry - px) / max(pos.entry, 1e-12)
                net_ret = gross_ret - 2 * slippage - 2 * fee
                pnl_amt = net_ret * notional
                realized_short += pnl_amt
                fees_short += fee * 2 * notional
                trades_short += 1
                if pnl_amt > 0:
                    wins_short += 1
                tr_rows.append({'leg': 'SHORT', 'symbol': sym, 'side': pos.side, 'entry_time': pos_time_short.get(sym, t), 'exit_time': t, 'entry': pos.entry, 'exit': px, 'action': ex.action, 'reason': ex.reason or ex.action, 'notional': notional, 'realized_pnl': pnl_amt, 'unrealized_pnl': 0.0})
                del positions_short[sym]; pos_time_short.pop(sym, None)
            elif ex and ex.action == 'TP_PARTIAL':
                px = float(ex.exit_price if ex.exit_price is not None else row['close'])
                part = max(0.0, min(1.0, float(getattr(ex, 'qty_frac', 0.5))))
                qty_close = pos.qty * part
                notional_entry = qty_close * pos.entry
                gross_ret = (pos.entry - px) / max(pos.entry, 1e-12)
                net_ret = gross_ret - 2 * slippage - 2 * fee
                pnl_amt = net_ret * notional_entry
                realized_short += pnl_amt
                fees_short += fee * 2 * notional_entry
                trades_short += 1
                if pnl_amt > 0:
                    wins_short += 1
                tr_rows.append({'leg': 'SHORT', 'symbol': sym, 'side': pos.side, 'entry_time': pos_time_short.get(sym, t), 'exit_time': t, 'entry': pos.entry, 'exit': px, 'action': 'TP_PARTIAL', 'reason': getattr(ex, 'reason', 'TP_PARTIAL'), 'notional': notional_entry, 'realized_pnl': pnl_amt, 'unrealized_pnl': 0.0})
                pos.qty -= qty_close

        unreal_long = _compute_unrealized(positions_long, px_map, fee, slippage)
        unreal_short = _compute_unrealized(positions_short, px_map, fee, slippage)
        equity_realized_total = initial_equity_total + realized_long + realized_short
        equity_mtm_total = equity_realized_total + unreal_long + unreal_short

        gross_long = _gross_notional(positions_long)
        gross_short = _gross_notional(positions_short)
        gross_total = gross_long + gross_short
        effective_notional = _effective_notional(positions_long, positions_short)
        allowed_notional = max_notional_frac * max(equity_mtm_total, 0.0)
        margin_call_excess = max(0.0, effective_notional - allowed_notional)
        in_margin_call = margin_call_excess > 0.0
        if in_margin_call:
            bars_in_margin_call += 1
        if in_margin_call and not prev_in_margin_call:
            margin_call_events_total += 1
        prev_in_margin_call = in_margin_call

        max_gross_long = max(max_gross_long, gross_long)
        max_gross_short = max(max_gross_short, gross_short)
        max_gross_total = max(max_gross_total, gross_total)
        max_effective = max(max_effective, effective_notional)

        ts_list.append(str(t))
        long_real_pnl_list.append(realized_long)
        short_real_pnl_list.append(realized_short)
        total_real_pnl_list.append(realized_long + realized_short)
        long_mtm_pnl_list.append(realized_long + unreal_long)
        short_mtm_pnl_list.append(realized_short + unreal_short)
        total_mtm_pnl_list.append(realized_long + realized_short + unreal_long + unreal_short)
        total_equity_mtm_list.append(equity_mtm_total)
        total_equity_realized_list.append(equity_realized_total)
        effective_notional_list.append(effective_notional)
        gross_notional_total_list.append(gross_total)
        gross_notional_long_list.append(gross_long)
        gross_notional_short_list.append(gross_short)
        margin_call_excess_list.append(margin_call_excess)

        # --- openings with shared margin ---
        for sym, row in md_map_all.items():
            if sym not in positions_long:
                sig = strat_long.entry_signal(True, sym, row, ctx=None)
                if sig is not None:
                    tp = getattr(sig, 'take_profit', getattr(sig, 'tp_price', getattr(sig, 'tp', None)))
                    sl = getattr(sig, 'stop_price', getattr(sig, 'sl_price', getattr(sig, 'sl', None)))
                    entry_px = float(row['close'])
                    prospective = _prospective_effective_notional(positions_long, positions_short, sym, +pos_notional_long)
                    if prospective <= allowed_notional + 1e-12:
                        if not isinstance(sl, (int, float)):
                            sl = max(1e-12, entry_px * 0.0001)
                        positions_long[sym] = Position(sig.side, entry_px, float(sl), float(tp), pos_notional_long / max(entry_px, 1e-12))
                        pos_time_long[sym] = t
            if sym not in positions_short:
                sig = strat_short.entry_signal(True, sym, row, ctx=None)
                if sig is not None:
                    tp = getattr(sig, 'take_profit', getattr(sig, 'tp_price', getattr(sig, 'tp', None)))
                    sl = getattr(sig, 'stop_price', getattr(sig, 'sl_price', getattr(sig, 'sl', None)))
                    entry_px = float(row['close'])
                    prospective = _prospective_effective_notional(positions_long, positions_short, sym, -pos_notional_short)
                    if prospective <= allowed_notional + 1e-12:
                        if not isinstance(sl, (int, float)):
                            sl = max(1e-12, entry_px * 999.0)
                        positions_short[sym] = Position(sig.side, entry_px, float(sl), float(tp), pos_notional_short / max(entry_px, 1e-12))
                        pos_time_short[sym] = t

    # EOD unrealized rows
    if slices:
        last_t = slices[-1][0]
        last_px = {sym: close for (sym, close, *_rest) in slices[-1][1]}
        for name, positions, pos_time in [('LONG', positions_long, pos_time_long), ('SHORT', positions_short, pos_time_short)]:
            for sym, pos in list(positions.items()):
                px = last_px.get(sym)
                if px is None:
                    continue
                notional = pos.entry * pos.qty
                gross_ret = (px - pos.entry) / max(pos.entry, 1e-12) if pos.side == 'LONG' else (pos.entry - px) / max(pos.entry, 1e-12)
                net_ret = gross_ret - 2 * slippage - 2 * fee
                unreal_amt = net_ret * notional
                tr_rows.append({'leg': name, 'symbol': sym, 'side': pos.side, 'entry_time': pos_time.get(sym, last_t), 'exit_time': last_t, 'entry': pos.entry, 'exit': px, 'action': 'EOD', 'reason': 'EOD (unrealized only)', 'notional': notional, 'realized_pnl': 0.0, 'unrealized_pnl': unreal_amt})

    total_minutes = _timeframe_to_minutes(cfg.get('timeframe', 0)) * len(slices)
    total_days = total_minutes / (60.0 * 24.0) if total_minutes else 0.0
    daily_ret, monthly_ret, yearly_ret = _annualized_returns(initial_equity_total + realized_long + realized_short, initial_equity_total, total_days)
    mdd_frac, mdd_pct = _max_drawdown(total_equity_mtm_list)

    summary = {
        'equity_start_total': initial_equity_total,
        'equity_end_realized_long': initial_equity_total / 2.0 + realized_long,
        'equity_end_realized_short': initial_equity_total / 2.0 + realized_short,
        'equity_end_realized_total': initial_equity_total + realized_long + realized_short,
        'realized_pnl_long': realized_long,
        'realized_pnl_short': realized_short,
        'realized_pnl_total': realized_long + realized_short,
        'trades_long': trades_long,
        'trades_short': trades_short,
        'trades_total': trades_long + trades_short,
        'win_rate_long_%': (wins_long * 100.0 / max(1, trades_long)) if trades_long else 0.0,
        'win_rate_short_%': (wins_short * 100.0 / max(1, trades_short)) if trades_short else 0.0,
        'elapsed_sec': time.time() - t0,
        'time_start': slices[0][0],
        'time_end': slices[-1][0],
        'mdd_total_frac': mdd_frac,
        'mdd_total_%': mdd_pct,
        'daily_return_total_%': daily_ret * 100.0,
        'monthly_return_total_%': monthly_ret * 100.0,
        'yearly_return_total_%': yearly_ret * 100.0,
        'margin_call_events_total': margin_call_events_total,
        'bars_in_margin_call': bars_in_margin_call,
        'margin_call_excess_max': max(margin_call_excess_list) if margin_call_excess_list else 0.0,
        'max_gross_notional_long': max_gross_long,
        'max_gross_notional_short': max_gross_short,
        'max_gross_notional_total': max_gross_total,
        'max_effective_notional_total': max_effective,
        'max_invested_long_in_firstBuyUSDT': (max_gross_long / first_buy) if first_buy else None,
        'max_invested_short_in_firstSellUSDT': (max_gross_short / first_sell) if first_sell else None,
        'firstBuyUSDT': first_buy,
        'firstSellUSDT': first_sell,
    }

    cfg_name = os.path.splitext(os.path.basename(args.cfg))[0]
    time_id = time.strftime('%Y%m%d_%H%M%S')
    report_dir = os.path.abspath(os.path.join('_reports', '_backtest', f'dual_{cfg_name}_{time_id}'))
    os.makedirs(report_dir, exist_ok=True)
    dual_summary_csv = os.path.join(report_dir, 'dual_summary.csv')
    dual_summary_json = os.path.join(report_dir, 'dual_summary.json')
    dual_trades_csv = os.path.join(report_dir, 'dual_trades.csv')
    pd.DataFrame([summary]).to_csv(dual_summary_csv, index=False)
    with open(dual_summary_json, 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, default=str)
    pd.DataFrame(tr_rows).to_csv(dual_trades_csv, index=False)
    print(f"[files] dual_summary={dual_summary_json} dual_trades={dual_trades_csv}")

    if args.plots_dir:
        run_plots_dir = args.plots_dir
        os.makedirs(run_plots_dir, exist_ok=True)
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
        ts = pd.to_datetime(pd.Series(ts_list, dtype=str), errors='coerce', utc=True)

        def _save_three(y1, y2, y3, title, ylabel, fname):
            plt.figure(figsize=(12, 6))
            plt.plot(ts, y1, label='Long')
            plt.plot(ts, y2, label='Short')
            plt.plot(ts, y3, label='Total')
            ax = plt.gca(); ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
            plt.xticks(rotation=45); plt.title(title); plt.xlabel('Time'); plt.ylabel(ylabel); plt.grid(True, alpha=0.2); plt.legend(); plt.tight_layout(); plt.savefig(os.path.join(run_plots_dir, fname), dpi=160); plt.close()

        _save_three(long_real_pnl_list, short_real_pnl_list, total_real_pnl_list, 'Realized PnL vs Time', 'Realized PnL', 'dual_realized_pnl.png')
        _save_three(long_mtm_pnl_list, short_mtm_pnl_list, total_mtm_pnl_list, 'MTM PnL vs Time', 'MTM PnL', 'dual_mtm_pnl.png')
        plt.figure(figsize=(12, 5))
        plt.plot(ts, margin_call_excess_list, label='Margin-call excess')
        ax = plt.gca(); ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45); plt.title('Margin-call excess vs Time'); plt.grid(True, alpha=0.2); plt.legend(); plt.tight_layout(); plt.savefig(os.path.join(run_plots_dir, 'dual_margin_call_excess.png'), dpi=160); plt.close()

        fig, axes = plt.subplots(4, 1, figsize=(12, 18), sharex=True)
        axes[0].plot(ts, long_real_pnl_list, label='Long realized PnL')
        axes[0].plot(ts, short_real_pnl_list, label='Short realized PnL')
        axes[0].plot(ts, total_real_pnl_list, label='Total realized PnL')
        axes[0].set_title('Realized PnL components'); axes[0].legend(); axes[0].grid(True, alpha=0.2)
        axes[1].plot(ts, long_mtm_pnl_list, label='Long MTM PnL')
        axes[1].plot(ts, short_mtm_pnl_list, label='Short MTM PnL')
        axes[1].plot(ts, total_mtm_pnl_list, label='Total MTM PnL')
        axes[1].set_title('MTM PnL components'); axes[1].legend(); axes[1].grid(True, alpha=0.2)
        axes[2].plot(ts, total_equity_mtm_list, label='Total MTM equity')
        axes[2].plot(ts, total_equity_realized_list, label='Total realized equity')
        axes[2].set_title('Shared-margin equity'); axes[2].legend(); axes[2].grid(True, alpha=0.2)
        axes[3].plot(ts, effective_notional_list, label='Effective notional (hedged)')
        axes[3].plot(ts, gross_notional_total_list, label='Gross notional')
        axes[3].plot(ts, margin_call_excess_list, label='Margin-call excess')
        axes[3].set_title('Exposure & margin'); axes[3].legend(); axes[3].grid(True, alpha=0.2)
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45); plt.tight_layout(); fig.savefig(os.path.join(run_plots_dir, 'dual_pnl_panels_all.png'), dpi=160); plt.close(fig)

        dst_plots = os.path.join(report_dir, 'plots')
        os.makedirs(dst_plots, exist_ok=True)
        for item in os.listdir(run_plots_dir):
            s = os.path.join(run_plots_dir, item)
            d = os.path.join(dst_plots, item)
            if os.path.isfile(s):
                shutil.copy2(s, d)

    print(f"[reports] saved to {report_dir}")
    print(json.dumps(summary, indent=2, default=str))


if __name__ == '__main__':
    main()
