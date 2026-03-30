#!/usr/bin/env python3
import argparse, sqlite3, importlib, time, sys, os, pathlib as _p, shutil, json
from dataclasses import dataclass
from typing import Dict, Any, List
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
    search_paths = [
        os.path.join('.', filename),
        os.path.join('..', filename),
        os.path.join('..', 'DB', filename),
    ]
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


def _open_notional(positions: Dict[str, Position]) -> float:
    return float(sum(p.entry * p.qty for p in positions.values()))


def _run_leg(name: str, strat, slices, initial_equity: float, pos_notional: float, fee: float, slippage: float, max_notional_frac: float):
    equity_realized = initial_equity
    positions: Dict[str, Position] = {}
    pos_time: Dict[str, str] = {}
    tr_rows: List[Dict[str, Any]] = []
    ts_list: List[str] = []
    eq_real_list: List[float] = []
    eq_mtm_list: List[float] = []
    unreal_list: List[float] = []
    eq_curve_vals = [initial_equity]
    wins = losses = trades = 0
    pnl_pos = pnl_neg = fees_cum = 0.0
    sub_pnl_cum = 0.0

    for t, bucket_all in slices:
        px_map = {sym: close for (sym, close, *_rest) in bucket_all}
        md_map_all = {
            sym: {"close": close, "atr_ratio": atr, "dp6h": dp6, "dp12h": dp12, "quote_volume": qv1h, "qv_24h": qv24, "datetime_utc": t}
            for (sym, close, atr, dp6, dp12, qv1h, qv24) in bucket_all
        }

        for sym, pos in list(positions.items()):
            row = md_map_all.get(sym)
            if row is None:
                continue
            ex = strat.manage_position(sym, row, pos, ctx=None)
            if ex and ex.action in ('TP', 'SL', 'EXIT'):
                px = float(ex.exit_price if ex.exit_price is not None else row['close'])
                notional = pos.entry * pos.qty
                gross_ret = (px - pos.entry) / pos.entry if pos.side == 'LONG' else (pos.entry - px) / pos.entry
                net_ret = gross_ret - 2 * slippage - 2 * fee
                pnl_amt = net_ret * notional
                trades += 1
                fees_cum += fee * 2 * notional
                if pnl_amt > 0:
                    wins += 1; pnl_pos += pnl_amt
                else:
                    losses += 1; pnl_neg += pnl_amt
                equity_realized += pnl_amt
                tr_rows.append({
                    'leg': name, 'symbol': sym, 'side': pos.side,
                    'entry_time': pos_time.get(sym, t), 'exit_time': t,
                    'entry': pos.entry, 'exit': px,
                    'action': ex.action, 'reason': ex.reason or ex.action,
                    'notional': notional, 'realized_pnl': pnl_amt, 'unrealized_pnl': 0.0,
                })
                del positions[sym]
                pos_time.pop(sym, None)
            elif ex and ex.action == 'TP_PARTIAL':
                px = float(ex.exit_price if ex.exit_price is not None else row['close'])
                part = max(0.0, min(1.0, float(getattr(ex, 'qty_frac', 0.5))))
                qty_close = pos.qty * part
                notional_entry = qty_close * pos.entry
                gross_ret = (px - pos.entry) / pos.entry if pos.side == 'LONG' else (pos.entry - px) / pos.entry
                net_ret = gross_ret - 2 * slippage - 2 * fee
                pnl_amt = net_ret * notional_entry
                trades += 1
                fees_cum += fee * 2 * notional_entry
                if pnl_amt > 0:
                    wins += 1; pnl_pos += pnl_amt
                else:
                    losses += 1; pnl_neg += pnl_amt
                equity_realized += pnl_amt
                sub_pnl_cum += pnl_amt
                tr_rows.append({
                    'leg': name, 'symbol': sym, 'side': pos.side,
                    'entry_time': pos_time.get(sym, t), 'exit_time': t,
                    'entry': pos.entry, 'exit': px,
                    'action': 'TP_PARTIAL', 'reason': getattr(ex, 'reason', 'TP_PARTIAL'),
                    'notional': notional_entry, 'realized_pnl': pnl_amt, 'unrealized_pnl': 0.0,
                })
                pos.qty -= qty_close

        unrealized = _compute_unrealized(positions, px_map, fee, slippage)
        equity_mtm = equity_realized + unrealized
        ts_list.append(str(t))
        eq_real_list.append(float(equity_realized))
        eq_mtm_list.append(float(equity_mtm))
        unreal_list.append(float(unrealized))

        universe_syms = strat.universe(t, md_map_all)
        ranked_syms = strat.rank(t, md_map_all, universe_syms)
        for sym in ranked_syms:
            if sym in positions:
                continue
            current_open = _open_notional(positions)
            if (current_open + pos_notional) > max_notional_frac * equity_mtm:
                break
            row = md_map_all.get(sym)
            if not row:
                continue
            sig = strat.entry_signal(True, sym, row, ctx=None)
            if sig is None:
                continue
            tp = getattr(sig, 'take_profit', getattr(sig, 'tp_price', getattr(sig, 'tp', None)))
            sl = getattr(sig, 'stop_price', getattr(sig, 'sl_price', getattr(sig, 'sl', None)))
            if not isinstance(tp, (int, float)):
                raise RuntimeError(f"Strategy must supply numeric take_profit for {sym} ({name})")
            if not isinstance(sl, (int, float)):
                allow_no_sl = True
                if not allow_no_sl:
                    raise RuntimeError(f"Strategy must supply numeric stop_price for {sym} ({name})")
                sl_fallback_pct = 99.99
                entry_px_tmp = float(row.get('close') or 0.0)
                if sig.side == 'LONG':
                    sl = max(1e-12, entry_px_tmp * (1.0 - sl_fallback_pct / 100.0))
                else:
                    sl = max(1e-12, entry_px_tmp * (1.0 + sl_fallback_pct / 100.0))
            entry_px = float(row['close'])
            qty = pos_notional / max(entry_px, 1e-12)
            positions[sym] = Position(sig.side, entry_px, float(sl), float(tp), qty)
            pos_time[sym] = t

        eq_curve_vals.append(equity_mtm)

    if slices and positions:
        last_t = slices[-1][0]
        last_px = {sym: close for (sym, close, *_rest) in slices[-1][1]}
        for sym, pos in list(positions.items()):
            px = last_px.get(sym)
            if px is None:
                continue
            notional = pos.entry * pos.qty
            gross_ret = (px - pos.entry) / pos.entry if pos.side == 'LONG' else (pos.entry - px) / pos.entry
            net_ret = gross_ret - 2 * slippage - 2 * fee
            unreal_amt = net_ret * notional
            tr_rows.append({
                'leg': name, 'symbol': sym, 'side': pos.side,
                'entry_time': pos_time.get(sym, last_t), 'exit_time': last_t,
                'entry': pos.entry, 'exit': px, 'action': 'EOD', 'reason': 'EOD (unrealized only)',
                'notional': notional, 'realized_pnl': 0.0, 'unrealized_pnl': unreal_amt,
            })
            del positions[sym]
            pos_time.pop(sym, None)

        ts_list.append(str(last_t))
        eq_real_list.append(float(equity_realized))
        eq_mtm_list.append(float(equity_realized))
        unreal_list.append(0.0)

    return {
        'name': name,
        'equity_start': initial_equity,
        'equity_end_realized': equity_realized,
        'realized_pnl_total': equity_realized - initial_equity,
        'trades': trades,
        'win_rate_%': (wins * 100.0 / max(1, trades)) if trades else 0.0,
        'profit_factor': (pnl_pos / max(1e-12, -pnl_neg)) if (pnl_pos > 0 and pnl_neg < 0) else 0.0,
        'total_fees_realized': fees_cum,
        'sub_trade_pnl_total': sub_pnl_cum,
        'ts_list': ts_list,
        'eq_real_list': eq_real_list,
        'eq_mtm_list': eq_mtm_list,
        'unreal_list': unreal_list,
        'trades_rows': tr_rows,
    }


def main():
    ap = argparse.ArgumentParser(description='Dual long+short backtester with separate and total PnL curves')
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
        q = [
            'SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h FROM price_indicators WHERE 1=1'
        ]
        params = []
        if t_from:
            q.append('AND datetime_utc >= ?'); params.append(t_from)
        if t_to:
            q.append('AND datetime_utc <= ?'); params.append(t_to)
        q.append('ORDER BY datetime_utc ASC, symbol ASC')
        rows = con.execute(' '.join(q), params).fetchall()
    else:
        th_row = con.execute(
            'SELECT MIN(datetime_utc) FROM (SELECT DISTINCT datetime_utc FROM price_indicators ORDER BY datetime_utc DESC LIMIT ?)',
            (int(args.limit_bars),),
        ).fetchone()
        if not th_row or not th_row[0]:
            raise RuntimeError('No bars.')
        min_time = th_row[0]
        rows = con.execute(
            'SELECT symbol, datetime_utc, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h FROM price_indicators WHERE datetime_utc >= ? ORDER BY datetime_utc ASC, symbol ASC',
            (min_time,),
        ).fetchall()
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
        bucket.append((
            r['symbol'],
            float(r['close'] or 0.0),
            float(r['atr_ratio'] or 0.0),
            float(r['dp6h'] or 0.0),
            float(r['dp12h'] or 0.0),
            float(r['quote_volume'] or 0.0),
            float(r['qv_24h'] or 0.0),
        ))
    if bucket:
        slices.append((cur_t, bucket))

    StratLong = import_by_path(cfg['strategy_class_long'])
    StratShort = import_by_path(cfg['strategy_class_short'])
    strat_long = StratLong(cfg)
    strat_short = StratShort(cfg)

    portfolio = cfg.get('portfolio', {})
    initial_equity_per_leg = float(portfolio.get('initial_equity_per_leg', 100.0))
    pos_notional = float(portfolio.get('position_notional', 5.0))
    fee = float(portfolio.get('fee_rate', 0.0))
    slippage = float(portfolio.get('slippage_per_side', 0.0))
    max_notional_frac = float(portfolio.get('max_notional_frac', 1.0))

    res_long = _run_leg('LONG', strat_long, slices, initial_equity_per_leg, pos_notional, fee, slippage, max_notional_frac)
    res_short = _run_leg('SHORT', strat_short, slices, initial_equity_per_leg, pos_notional, fee, slippage, max_notional_frac)

    ts = pd.to_datetime(pd.Series(res_long['ts_list'], dtype=str), errors='coerce', utc=True)
    long_real_pnl = [x - res_long['equity_start'] for x in res_long['eq_real_list']]
    short_real_pnl = [x - res_short['equity_start'] for x in res_short['eq_real_list']]
    total_real_pnl = [a + b for a, b in zip(long_real_pnl, short_real_pnl)]
    long_mtm_pnl = [x - res_long['equity_start'] for x in res_long['eq_mtm_list']]
    short_mtm_pnl = [x - res_short['equity_start'] for x in res_short['eq_mtm_list']]
    total_mtm_pnl = [a + b for a, b in zip(long_mtm_pnl, short_mtm_pnl)]

    cfg_name = os.path.splitext(os.path.basename(args.cfg))[0]
    time_id = time.strftime('%Y%m%d_%H%M%S')
    report_dir = os.path.abspath(os.path.join('_reports', '_backtest', f'dual_{cfg_name}_{time_id}'))
    os.makedirs(report_dir, exist_ok=True)

    summary = {
        'equity_start_total': initial_equity_per_leg * 2.0,
        'equity_end_realized_long': res_long['equity_end_realized'],
        'equity_end_realized_short': res_short['equity_end_realized'],
        'equity_end_realized_total': res_long['equity_end_realized'] + res_short['equity_end_realized'],
        'realized_pnl_long': res_long['realized_pnl_total'],
        'realized_pnl_short': res_short['realized_pnl_total'],
        'realized_pnl_total': res_long['realized_pnl_total'] + res_short['realized_pnl_total'],
        'trades_long': res_long['trades'],
        'trades_short': res_short['trades'],
        'win_rate_long_%': res_long['win_rate_%'],
        'win_rate_short_%': res_short['win_rate_%'],
        'profit_factor_long': res_long['profit_factor'],
        'profit_factor_short': res_short['profit_factor'],
        'elapsed_sec': time.time() - t0,
        'symbol_count': len(set(r[0] for _, b in slices for r in b)),
        'time_start': slices[0][0],
        'time_end': slices[-1][0],
    }

    pd.DataFrame([summary]).to_csv(os.path.join(report_dir, 'dual_summary.csv'), index=False)
    pd.DataFrame(res_long['trades_rows'] + res_short['trades_rows']).to_csv(os.path.join(report_dir, 'dual_trades.csv'), index=False)
    with open(os.path.join(report_dir, 'dual_summary.json'), 'w', encoding='utf-8') as f:
        json.dump(summary, f, indent=2, default=str)

    if args.plots_dir:
        run_plots_dir = args.plots_dir
        os.makedirs(run_plots_dir, exist_ok=True)
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        def _save_three(y1, y2, y3, title, ylabel, fname):
            plt.figure(figsize=(12, 6))
            plt.plot(ts, y1, label='Long')
            plt.plot(ts, y2, label='Short')
            plt.plot(ts, y3, label='Total')
            ax = plt.gca()
            ax.xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
            plt.xticks(rotation=45)
            plt.title(title)
            plt.xlabel('Time')
            plt.ylabel(ylabel)
            plt.grid(True, alpha=0.2)
            plt.legend()
            plt.tight_layout()
            plt.savefig(os.path.join(run_plots_dir, fname), dpi=160)
            plt.close()

        _save_three(long_real_pnl, short_real_pnl, total_real_pnl, 'Realized PnL vs Time', 'Realized PnL', 'dual_realized_pnl.png')
        _save_three(long_mtm_pnl, short_mtm_pnl, total_mtm_pnl, 'MTM PnL vs Time', 'MTM PnL', 'dual_mtm_pnl.png')

        fig, axes = plt.subplots(3, 1, figsize=(12, 14), sharex=True)
        axes[0].plot(ts, long_real_pnl, label='Long realized PnL')
        axes[0].plot(ts, short_real_pnl, label='Short realized PnL')
        axes[0].plot(ts, total_real_pnl, label='Total realized PnL')
        axes[0].set_title('Realized PnL components')
        axes[0].legend()
        axes[0].grid(True, alpha=0.2)

        axes[1].plot(ts, long_mtm_pnl, label='Long MTM PnL')
        axes[1].plot(ts, short_mtm_pnl, label='Short MTM PnL')
        axes[1].plot(ts, total_mtm_pnl, label='Total MTM PnL')
        axes[1].set_title('MTM PnL components')
        axes[1].legend()
        axes[1].grid(True, alpha=0.2)

        axes[2].plot(ts, res_long['eq_real_list'], label='Long equity')
        axes[2].plot(ts, res_short['eq_real_list'], label='Short equity')
        axes[2].plot(ts, [a+b for a,b in zip(res_long['eq_real_list'], res_short['eq_real_list'])], label='Total equity')
        axes[2].set_title('Equity curves (realized)')
        axes[2].legend()
        axes[2].grid(True, alpha=0.2)
        axes[-1].xaxis.set_major_formatter(mdates.DateFormatter('%Y-%m'))
        plt.xticks(rotation=45)
        plt.tight_layout()
        fig.savefig(os.path.join(run_plots_dir, 'dual_pnl_panels_all.png'), dpi=160)
        plt.close(fig)

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
