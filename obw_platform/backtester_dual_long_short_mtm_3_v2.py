#!/usr/bin/env python3
from __future__ import annotations
import argparse, json, sqlite3, time, yaml
from pathlib import Path
import numpy as np
import pandas as pd

try:
    from backtester_dual_core_dynamic_v5 import simulate
except ImportError:
    from backtester_dual_core_dynamic_v2 import simulate


def save_plot_bundle(curves: pd.DataFrame, plots_dir: str, prefix: str = 'dual') -> dict:
    from pathlib import Path
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

    def _save(fig, name: str):
        out = plots_path / name
        fig.tight_layout()
        fig.savefig(out, dpi=160, bbox_inches='tight')
        plt.close(fig)
        generated.append(str(out))

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df['bar_ts'], df.get('realized_pnl', pd.Series(np.zeros(len(df)))), label='Realized total')
    if 'realized_pnl_long' in df.columns:
        ax.plot(df['bar_ts'], df['realized_pnl_long'], label='Realized long', alpha=0.9)
    if 'realized_pnl_short' in df.columns:
        ax.plot(df['bar_ts'], df['realized_pnl_short'], label='Realized short', alpha=0.9)
    ax.set_title('Dual realized PnL')
    ax.set_xlabel('Time (UTC)')
    ax.set_ylabel('PnL')
    ax.grid(True, alpha=0.3)
    ax.legend()
    _save(fig, f'{prefix}_realized_pnl.png')

    fig, ax = plt.subplots(figsize=(12, 5))
    ax.plot(df['bar_ts'], df.get('total_pnl', pd.Series(np.zeros(len(df)))), label='Total PnL (MTM)')
    if 'unrealized_pnl' in df.columns:
        ax.plot(df['bar_ts'], df['unrealized_pnl'], label='Unrealized', alpha=0.8)
    if 'realized_pnl' in df.columns:
        ax.plot(df['bar_ts'], df['realized_pnl'], label='Realized', alpha=0.8)
    ax.set_title('Dual MTM PnL')
    ax.set_xlabel('Time (UTC)')
    ax.set_ylabel('PnL')
    ax.grid(True, alpha=0.3)
    ax.legend()
    _save(fig, f'{prefix}_mtm_pnl.png')

    fig, axes = plt.subplots(4, 1, figsize=(13, 12), sharex=True)
    axes[0].plot(df['bar_ts'], df.get('realized_pnl', pd.Series(np.zeros(len(df)))))
    axes[0].set_title('Realized PnL')
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(df['bar_ts'], df.get('unrealized_pnl', pd.Series(np.zeros(len(df)))))
    axes[1].set_title('Unrealized PnL')
    axes[1].grid(True, alpha=0.3)
    axes[2].plot(df['bar_ts'], df.get('total_pnl', pd.Series(np.zeros(len(df)))))
    axes[2].set_title('Total PnL (MTM)')
    axes[2].grid(True, alpha=0.3)
    axes[3].plot(df['bar_ts'], df.get('long_notional', pd.Series(np.zeros(len(df)))), label='Long notional')
    axes[3].plot(df['bar_ts'], df.get('short_notional', pd.Series(np.zeros(len(df)))), label='Short notional')
    axes[3].set_title('Long / Short notional')
    axes[3].grid(True, alpha=0.3)
    axes[3].legend()
    axes[3].set_xlabel('Time (UTC)')
    _save(fig, f'{prefix}_pnl_panels_all.png')

    if 'effective_notional' in df.columns and 'allowed_notional' in df.columns:
        fig, ax = plt.subplots(figsize=(12, 5))
        ax.plot(df['bar_ts'], df['effective_notional'], label='Effective notional')
        ax.plot(df['bar_ts'], df['allowed_notional'], label='Allowed notional')
        if 'margin_excess' in df.columns:
            ax.fill_between(df['bar_ts'], 0, np.maximum(df['margin_excess'].astype(float).to_numpy(), 0.0), alpha=0.25, label='Excess over limit')
        ax.set_title('Margin call excess')
        ax.set_xlabel('Time (UTC)')
        ax.set_ylabel('Notional')
        ax.grid(True, alpha=0.3)
        ax.legend()
        _save(fig, f'{prefix}_margin_call_excess.png')

    return {'plots_dir': str(plots_path), 'plots': generated}



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
    ap.add_argument('--plots', default='')
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
    if args.plots:
        out.update(save_plot_bundle(curves, args.plots, prefix='dual'))
    print(json.dumps(out, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
