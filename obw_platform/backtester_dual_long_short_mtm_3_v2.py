#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3, time, yaml
from pathlib import Path
import numpy as np
import pandas as pd

from backtester_dual_core_dynamic_v2 import simulate


def load_db_bars(db_path: str, symbol: str):
    con = sqlite3.connect(db_path)
    df = pd.read_sql_query(
        'select symbol, datetime_utc, open, high, low, close, volume, quote_volume from price_indicators where symbol=? order by datetime_utc',
        con, params=(symbol,)
    )
    con.close()
    if df.empty:
        raise SystemExit(f'no bars for symbol {symbol} in {db_path}')
    ts_s = (pd.to_datetime(df['datetime_utc'], utc=True).astype('int64') // 10**9).astype(np.int64).to_numpy()
    extras = {}
    if 'quote_volume' in df.columns:
        extras['quote_volume'] = df['quote_volume'].astype(np.float64).to_numpy()
    return ts_s, df['open'].astype(np.float64).to_numpy(), df['high'].astype(np.float64).to_numpy(), df['low'].astype(np.float64).to_numpy(), df['close'].astype(np.float64).to_numpy(), df['volume'].astype(np.float64).to_numpy(), extras


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--db', required=True)
    ap.add_argument('--symbol', required=True)
    ap.add_argument('--time-from', default='')
    ap.add_argument('--time-to', default='')
    ap.add_argument('--export-curves', default='')
    ap.add_argument('--dynamic-slippage-json', default='')
    args = ap.parse_args()
    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg, 'r', encoding='utf-8'))
    model_override = json.loads(args.dynamic_slippage_json) if args.dynamic_slippage_json else None
    ts_s, open_, high, low, close, volume, extras = load_db_bars(args.db, args.symbol)
    if args.time_from:
        tf = int(pd.Timestamp(args.time_from).tz_localize('UTC').timestamp()) if 'T' not in args.time_from and '+' not in args.time_from and 'Z' not in args.time_from else int(pd.Timestamp(args.time_from).timestamp())
        m = ts_s >= tf
        ts_s, open_, high, low, close, volume = ts_s[m], open_[m], high[m], low[m], close[m], volume[m]
        extras = {k: v[m] for k, v in extras.items()}
    if args.time_to:
        tt = int(pd.Timestamp(args.time_to).tz_localize('UTC').timestamp()) if 'T' not in args.time_to and '+' not in args.time_to and 'Z' not in args.time_to else int(pd.Timestamp(args.time_to).timestamp())
        m = ts_s <= tt
        ts_s, open_, high, low, close, volume = ts_s[m], open_[m], high[m], low[m], close[m], volume[m]
        extras = {k: v[m] for k, v in extras.items()}
    out = simulate(cfg, ts_s, close, open_=open_, high=high, low=low, volume=volume, extras=extras, market_symbol=args.symbol, model_override=model_override, export_curves=True)
    curves = out.pop('curves')
    out['elapsed_sec'] = time.time() - t0
    if args.export_curves:
        Path(args.export_curves).parent.mkdir(parents=True, exist_ok=True)
        curves.to_csv(args.export_curves, index=False)
        out['curves_csv'] = args.export_curves
    else:
        csv_path = Path(args.db).with_suffix('.mtm_curves.csv')
        curves.to_csv(csv_path, index=False)
        out['curves_csv'] = str(csv_path)
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
