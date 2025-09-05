from .common import *
# експліцитні "приватні" утиліти
try:
    from .common import _tf_to_seconds, _align_bar_close, _print_heat_from_strategy
except Exception:
    from datetime import datetime, timezone
    def _tf_to_seconds(tf: str) -> int:
        tf = (tf or "1h").strip().lower()
        unit = tf[-1]
        try: n = int(tf[:-1])
        except Exception: n = 1
        mult = {'m': 60, 'h': 3600, 'd': 86400, 'w': 7*86400}
        return n * mult.get(unit, 3600)
    def _align_bar_close(now_dt, tf_seconds: int):
        epoch = int(now_dt.timestamp())
        aligned = epoch - (epoch % tf_seconds)
        return datetime.fromtimestamp(aligned, tz=timezone.utc)
    def _print_heat_from_strategy(*args, **kwargs):
        return False

# кольоровий друк (fallback)
try:
    from .common import cprint as _cprint, dot as _dot
except Exception:
    _cprint, _dot = None, None
def cprint(*parts, fg: str = "", bold: bool = False, dim: bool = False, file=None, end="\n", flush=False):
    if _cprint:
        return _cprint(*parts, fg=fg, bold=bold, dim=dim, file=file, end=end, flush=flush)
    print(" ".join(str(p) for p in parts), file=file, end=end, flush=flush)
def dot():
    if _dot:
        return _dot()
    print(".", end="", flush=True)

import importlib
import sys
import uuid
import os
import json
import time
from datetime import datetime, timezone

def load_strategy(path_cls: str, cfg: dict):
    mod_path, cls_name = path_cls.rsplit('.', 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(cfg)

def run_paper_api(cfg: dict, args):
    assert EnginePortfolio is not None, 'engine.portfolio.Portfolio unavailable'

    strat_path = cfg.get('strategy_class', 'strategies.cross_sectional_rs.CrossSectionalRS')
    strat = load_strategy(strat_path, cfg)

    port_cfg = {
        'initial_equity': float(cfg.get('initial_equity', cfg.get('start_cash', 200.0))),
        'fee_rate': float(cfg.get('fee_rate', 0.0006)),
        'slippage_per_side': float(cfg.get('slippage_per_side', cfg.get('slip_bps', 1.5) / 10000.0
                                           if isinstance(cfg.get('slip_bps', 0), (int, float)) else 0.0003)),
        'tick_pct': float(cfg.get('tick_pct', 0.0001)),
        'position_notional': float(cfg.get('notional', 2.2)),
        'max_notional_frac': float(cfg.get('max_notional_frac', 0.5)),
        'funding_rate_hour': float(cfg.get('funding_rate_hour', 0.0)),
    }
    pf = EnginePortfolio(port_cfg)

    fetcher = CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=args.debug)

    os.makedirs(args.results_dir, exist_ok=True)
    orders_db = args.orders_db or os.path.join(args.results_dir, 'orders.sqlite')
    ensure_orders_db(orders_db)
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, args.session_db, args.cache_out)

    run_id = datetime.utcnow().strftime('PA_%Y%m%d_%H%M%S')
    write_config_snapshot(session_db_path, run_id, cfg)

    top_n = int(cfg.get('top_n', 4))
    tf = str(cfg.get('timeframe', '1h'))
    tf_sec = _tf_to_seconds(tf)
    cprint('[paper-api]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s; orders -> {orders_db}', fg='cyan')
    last_bar_ts = None
    iters_left = getattr(args, 'iterations', None) if getattr(args, 'dry_run', False) else None

    while True:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        bar_close = _align_bar_close(now, tf_sec)
        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close

            # Build universe with allow-list filtering (ENV or cfg['universe'].allow)
            allow = []
            try:
                allow_env = os.getenv('RS_UNIVERSE_ALLOW', '')
                if allow_env:
                    allow = [s.strip() for s in allow_env.split(',') if s.strip()]
                if not allow:
                    allow = list((cfg.get('universe', {}) or {}).get('allow', []) or [])
            except Exception:
                allow = []
            all_syms = sorted(set(fetcher.by_base.values()))
            universe = [s for s in all_syms if (not allow or s in allow)]
            md = {}
            for ccxt_sym in universe:
                feats = {}
                if args.hour_cache == 'load':
                    feats = read_hour_cache_row(cache_out_path, ccxt_sym, bar_close)
                if not feats:
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=tf, limit=max(60, args.limit_klines))
                    if df is None or len(df) < 30:
                        continue
                    feats_df = compute_feats(df, tf_seconds=tf_sec)
                    if args.hour_cache in ('save', 'load'):
                        cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                    feats = feats_df.iloc[-1].to_dict()
                md[ccxt_sym] = feats

            # exits
            for pos in list(pf.positions):
                row = md.get(pos.symbol)
                if row is None:
                    continue
                adj = strat.manage_position(bar_close, pos.symbol, pos, row, ctx={'portfolio': pf})
                if getattr(adj, 'action', None) == 'EXIT':
                    px = float(row.get('close') or 0.0) * (1 - port_cfg['slippage_per_side'])
                    pf.close(pos, bar_close, px, reason=adj.reason)
                    order_side = 'sell' if str(getattr(pos, 'side', 'LONG')).upper() == 'LONG' else 'buy'
                    insert_order_row(orders_db, {
                        'order_id': str(uuid.uuid4()),
                        'ts_utc': datetime.utcnow().isoformat(),
                        'bar_time_utc': bar_close.isoformat(),
                        'mode': 'paper_api',
                        'symbol': pos.symbol,
                        'side': order_side,
                        'type': 'market',
                        'price': float(px),
                        'qty': float(pos.qty),
                        'status': 'filled',
                        'reason': adj.reason or 'exit',
                        'run_id': run_id,
                        'extra': json.dumps({'sim': True})
                    })

            # entries + decisions
            uni = strat.universe(bar_close, md)
            ranked = strat.rank(bar_close, md, uni)[:top_n]
            selected_syms = list(ranked)
            write_decisions(session_db_path, run_id, bar_close, ranked, selected_syms)

            for sym in ranked:
                row = md.get(sym)
                if row is None:
                    continue
                sig = strat.entry_signal(bar_close, sym, row, ctx={'portfolio': pf})
                if sig is None:
                    continue
                if not pf.can_open(port_cfg):
                    continue
                entry_px = float(row.get('close') or 0.0) * (1 + port_cfg['slippage_per_side'])
                pos = pf.open(symbol=sym, signal=sig, t=bar_close, last_price=entry_px)
                insert_order_row(orders_db, {
                    'order_id': str(uuid.uuid4()),
                    'ts_utc': datetime.utcnow().isoformat(),
                    'bar_time_utc': bar_close.isoformat(),
                    'mode': 'paper_api',
                    'symbol': sym,
                    'side': 'buy',
                    'type': 'market',
                    'price': float(entry_px),
                    'qty': float(getattr(pos, 'qty', 0.0)),
                    'status': 'filled',
                    'reason': 'entry',
                    'run_id': run_id,
                    'extra': json.dumps({'sim': True})
                })

            # equity snapshot
            eq = {
                'equity': getattr(pf, 'equity', 0.0),
                'cash': getattr(pf, 'cash', 0.0),
                'position_value': getattr(pf, 'position_value', 0.0),
                'realized_pnl_cum': getattr(pf, 'realized_pnl_cum', 0.0),
                'unrealized_pnl': getattr(pf, 'unrealized_pnl', 0.0)
            }
            write_equity(session_db_path, run_id, bar_close, eq)

            trades_csv = os.path.join(args.results_dir, 'trades.csv')
            summary_csv = os.path.join(args.results_dir, 'summary.csv')
            pf.save_trades(trades_csv)
            pf.save_summary(summary_csv)

            cprint("[paper-api]", f"bar {bar_close.isoformat()} processed: positions={len(pf.positions)}", fg="cyan")
            if args.heat_report and len(pf.positions) == 0:
                _print_heat_from_strategy(strat, 'paper-api', bar_close, md, uni)

            if iters_left is not None:
                iters_left -= 1
                if iters_left <= 0:
                    break
        else:
            dot()
        time.sleep(args.poll_sec)
