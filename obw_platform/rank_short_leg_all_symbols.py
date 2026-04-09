#!/usr/bin/env python3
import argparse, sqlite3, importlib, json
from dataclasses import dataclass
from typing import Dict, Any, List
import yaml, pandas as pd, math

@dataclass
class Position:
    side: str
    entry: float
    sl: float
    tp: float
    qty: float


def import_by_path(path: str):
    mod_name, cls_name = path.rsplit('.', 1)
    mod = importlib.import_module(mod_name)
    return getattr(mod, cls_name)


def run_short_symbol(rows: List[dict], cfg: dict) -> dict:
    Strat = import_by_path(cfg['strategy_class_short'])
    strat = Strat(cfg)
    portfolio = cfg.get('portfolio', {})
    initial_equity = float(portfolio.get('initial_equity_per_leg', 100.0))
    fee = float(portfolio.get('fee_rate', 0.0))
    slippage = float(portfolio.get('slippage_per_side', 0.0))
    equity_realized = initial_equity
    position = None
    trades = wins = losses = 0
    pnl_pos = pnl_neg = 0.0
    eq_real_curve = [initial_equity]
    eq_mtm_curve = [initial_equity]

    def unreal(px: float):
        nonlocal position
        if position is None:
            return 0.0
        gross_ret = (position.entry - px) / max(position.entry, 1e-12)
        net_ret = gross_ret - 2*slippage - 2*fee
        return net_ret * (position.entry * position.qty)

    for row in rows:
        px = float(row['close'])
        if position is not None:
            ex = strat.manage_position(row['symbol'], row, position, ctx=None)
            if ex and getattr(ex, 'action', None) in ('TP','SL','EXIT'):
                exit_px = float(getattr(ex, 'exit_price', px) or px)
                notional = position.entry * position.qty
                gross_ret = (position.entry - exit_px) / max(position.entry, 1e-12)
                net_ret = gross_ret - 2*slippage - 2*fee
                pnl = net_ret * notional
                equity_realized += pnl
                trades += 1
                if pnl > 0: wins += 1; pnl_pos += pnl
                else: losses += 1; pnl_neg += pnl
                position = None
            elif ex and getattr(ex, 'action', None) == 'TP_PARTIAL':
                qty_frac = max(0.0, min(1.0, float(getattr(ex, 'qty_frac', 0.0) or 0.0)))
                qty_close = position.qty * qty_frac
                if qty_close > 0:
                    exit_px = float(getattr(ex, 'exit_price', px) or px)
                    notional = position.entry * qty_close
                    gross_ret = (position.entry - exit_px) / max(position.entry, 1e-12)
                    net_ret = gross_ret - 2*slippage - 2*fee
                    pnl = net_ret * notional
                    equity_realized += pnl
                    trades += 1
                    if pnl > 0: wins += 1; pnl_pos += pnl
                    else: losses += 1; pnl_neg += pnl
                    position.qty -= qty_close
                    if position.qty <= 1e-12:
                        position = None
        if position is None:
            sig = strat.entry_signal(True, row['symbol'], row, ctx=None)
            if sig is not None:
                tp = getattr(sig, 'tp', None)
                sl = getattr(sig, 'sl', None)
                entry_px = px
                first_usdt = float(getattr(strat, 'first_usdt', 0.0) or 0.0)
                qty = first_usdt / max(entry_px, 1e-12)
                position = Position('SHORT', entry_px, float(sl) if sl is not None else math.nan, float(tp), qty)
        eq_real_curve.append(equity_realized)
        eq_mtm_curve.append(equity_realized + unreal(px))

    # EOD unrealized not realized, but we use MTM MDD too
    import numpy as np
    arr_r = np.asarray(eq_real_curve, dtype=float)
    arr_m = np.asarray(eq_mtm_curve, dtype=float)
    mdd_r = float(((arr_r - np.maximum.accumulate(arr_r)) / np.maximum.accumulate(arr_r)).min()) if len(arr_r) > 1 else 0.0
    mdd_m = float(((arr_m - np.maximum.accumulate(arr_m)) / np.maximum.accumulate(arr_m)).min()) if len(arr_m) > 1 else 0.0
    pf = (pnl_pos / max(1e-12, -pnl_neg)) if pnl_pos > 0 and pnl_neg < 0 else 0.0
    return {
        'symbol': rows[0]['symbol'] if rows else '',
        'bars': len(rows),
        'equity_end_realized': equity_realized,
        'realized_pnl': equity_realized - initial_equity,
        'return_pct': (equity_realized / initial_equity - 1.0) * 100.0 if initial_equity else 0.0,
        'trades': trades,
        'win_rate_pct': wins * 100.0 / max(1, trades),
        'profit_factor': pf,
        'mdd_realized_pct': mdd_r * 100.0,
        'mdd_mtm_pct': mdd_m * 100.0,
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--top', type=int, default=10)
    ap.add_argument('--out', default='short_leg_ranking.csv')
    args = ap.parse_args()

    cfg = yaml.safe_load(open(args.cfg, 'r'))
    con = sqlite3.connect(args.db)
    con.row_factory = sqlite3.Row
    rows = con.execute("SELECT symbol, datetime_utc, open, high, low, close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h FROM price_indicators ORDER BY symbol, datetime_utc").fetchall()
    groups: Dict[str, List[dict]] = {}
    for r in rows:
        groups.setdefault(r['symbol'], []).append({k:r[k] for k in r.keys()})
    results = []
    for i, (sym, srows) in enumerate(groups.items(), 1):
        results.append(run_short_symbol(srows, cfg))
        if i % 50 == 0:
            print(f'[progress] {i}/{len(groups)}')
    df = pd.DataFrame(results).sort_values(['realized_pnl','return_pct','mdd_mtm_pct'], ascending=[False, False, False])
    df.to_csv(args.out, index=False)
    print(df.head(args.top).to_string(index=False))
    print(f'\\n[files] {args.out}')

if __name__ == '__main__':
    main()
