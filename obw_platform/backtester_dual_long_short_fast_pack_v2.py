#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, time, yaml
import numpy as np
import pandas as pd
from pathlib import Path

from backtester_dual_core_dynamic_v2 import pick_symbol_block, parse_iso_to_epoch_s, simulate


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--cfg', required=True)
    ap.add_argument('--npz', required=True)
    ap.add_argument('--symbol', default='')
    ap.add_argument('--limit-bars', type=int, default=0)
    ap.add_argument('--time-from', default='')
    ap.add_argument('--time-to', default='')
    ap.add_argument('--export-curves', default='')
    ap.add_argument('--dynamic-slippage-json', default='')
    args = ap.parse_args()
    t0 = time.time()
    cfg = yaml.safe_load(open(args.cfg, 'r', encoding='utf-8'))
    model_override = json.loads(args.dynamic_slippage_json) if args.dynamic_slippage_json else None
    data = np.load(args.npz, allow_pickle=True)
    market_symbol, ts_s, open_, high, low, close, volume, extras = pick_symbol_block(data, args.symbol)
    if args.time_from:
        tf = parse_iso_to_epoch_s(args.time_from)
        m = ts_s >= tf
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

    out = simulate(cfg, ts_s, close, open_=open_, high=high, low=low, volume=volume, extras=extras, market_symbol=market_symbol, model_override=model_override, export_curves=bool(args.export_curves))
    out['elapsed_sec'] = time.time() - t0
    curves = out.pop('curves', None)
    if args.export_curves and curves is not None:
        Path(args.export_curves).parent.mkdir(parents=True, exist_ok=True)
        curves.to_csv(args.export_curves, index=False)
        out['curves_csv'] = args.export_curves
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
