
# runners/paper_api_runner.py  — patched to be tolerant to strategy signal shapes.
# Key changes:
#  - Normalize Sig: ensure .tags (list), .take_profit/.stop_price (convert from tp/tp_price, sl/sl_price), .size, .confidence, .reason.
#  - Call adapters for entry_signal/manage_position to support multiple signatures.
#  - Keep your existing engine/common interfaces; fall back if package-relative imports fail.
from __future__ import annotations

import os, sys, json, time, uuid, importlib
from datetime import datetime, timezone
import pandas as pd
from typing import Any, Mapping

# --- imports from runners.common (with fallbacks) ----------------------------------
try:
    # package-style
    from .common import (
        EnginePortfolio, CCXTFetcher,
        ensure_orders_db, ensure_session_dbs, write_config_snapshot,
        read_hour_cache_row, cache_out_upsert, compute_feats,
        write_equity, write_decisions, insert_order_row,
        cprint as _cprint, dot as _dot,
        _tf_to_seconds, _align_bar_close, _print_heat_from_strategy
    )
except Exception:
    # module-style
    from common import (
        EnginePortfolio, CCXTFetcher,
        ensure_orders_db, ensure_session_dbs, write_config_snapshot,
        read_hour_cache_row, cache_out_upsert, compute_feats,
        write_equity, write_decisions, insert_order_row,
        cprint as _cprint, dot as _dot,
        _tf_to_seconds, _align_bar_close, _print_heat_from_strategy
    )

def cprint(*parts, fg: str = "", bold: bool = False, dim: bool = False, file=None, end="\n", flush=False):
    try:
        return _cprint(*parts, fg=fg, bold=bold, dim=dim, file=file, end=end, flush=flush)
    except Exception:
        print(" ".join(str(p) for p in parts), file=file, end=end, flush=flush)

def dot():
    try:
        return _dot()
    except Exception:
        print(".", end="", flush=True)


def _fmt_float(val: Any) -> str:
    """Format float to at most two decimal places without trailing zeros."""
    try:
        return ("{:.3f}".format(float(val))).rstrip("0").rstrip(".")
    except Exception:
        return str(val)

# --- strategy loader ---------------------------------------------------------------
def load_strategy(path_cls: str, cfg: Mapping[str, Any]):
    mod_path, cls_name = path_cls.rsplit('.', 1)
    # make project root importable
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    if root not in sys.path:
        sys.path.insert(0, root)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(cfg)

# --- adapters & normalizers --------------------------------------------------------
def _normalize_sig(sig):
    """Attach commonly expected attributes to a signal object to avoid runtime errors."""
    # side
    if not hasattr(sig, "side"):
        setattr(sig, "side", "LONG")
    else:
        try:
            setattr(sig, "side", str(getattr(sig, "side")).upper())
        except Exception:
            setattr(sig, "side", "LONG")

    # TP: take_profit <- tp_price/tp
    if not hasattr(sig, "take_profit"):
        for k in ("tp_price", "tp"):
            if hasattr(sig, k):
                try:
                    setattr(sig, "take_profit", float(getattr(sig, k)))
                    break
                except Exception:
                    pass

    # SL: stop_price <- sl_price/sl
    if not hasattr(sig, "stop_price"):
        for k in ("sl_price", "sl"):
            if hasattr(sig, k):
                try:
                    setattr(sig, "stop_price", float(getattr(sig, k)))
                    break
                except Exception:
                    pass

    # Optional, but many engines expect these to exist
    if not hasattr(sig, "size"):
        setattr(sig, "size", None)
    if not hasattr(sig, "confidence"):
        setattr(sig, "confidence", 0.0)
    if not hasattr(sig, "reason"):
        setattr(sig, "reason", None)
    if not hasattr(sig, "tags"):
        setattr(sig, "tags", [])  # <--- critical: avoid 'Sig has no attribute tags'

    # Numeric coercion (best-effort; don't raise)
    try:
        if getattr(sig, "take_profit", None) is not None:
            sig.take_profit = float(sig.take_profit)
    except Exception:
        pass
    try:
        if getattr(sig, "stop_price", None) is not None:
            sig.stop_price = float(sig.stop_price)
    except Exception:
        pass
    return sig

def _call_entry_signal(strat, bar_close, sym, row, pf):
    """Try compatible signatures for entry_signal."""
    # Preferred: entry_signal(bar_close: bool, symbol, row, ctx={...})
    try:
        return strat.entry_signal(True, sym, row, ctx={'portfolio': pf})
    except TypeError:
        pass
    # Variant: entry_signal(symbol, row, ctx)
    try:
        return strat.entry_signal(sym, row, ctx={'portfolio': pf})
    except Exception:
        return None

def _call_manage_position(strat, bar_close, sym, pos, row, pf):
    """Try compatible signatures for manage_position."""
    # Preferred: manage_position(symbol, row, pos, ctx)
    try:
        return strat.manage_position(sym, row, pos, ctx={'portfolio': pf})
    except TypeError:
        pass
    # Variant: manage_position(bar_close, symbol, pos, row, ctx)
    try:
        return strat.manage_position(True, sym, pos, row, ctx={'portfolio': pf})
    except Exception:
        return None

# --- main loop --------------------------------------------------------------------
def run_paper_api(cfg: Mapping[str, Any], args):
    assert EnginePortfolio is not None, 'EnginePortfolio unavailable'
    strat_path = cfg.get('strategy_class')
    if not strat_path:
        raise RuntimeError("cfg.strategy_class is required")

    strat = load_strategy(strat_path, cfg)

    # Portfolio settings (use your cfg keys; keep fallbacks for compatibility)
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

    # Strategy-owned knobs (fallbacks only if cfg doesn't provide them at strategy level)
    sp = (cfg.get('strategy_params') or {})
    top_n = int(cfg.get('top_n', sp.get('top_n', 8)))
    tf = str(cfg.get('timeframe', '1h'))
    tf_seconds = _tf_to_seconds(tf)

    cprint('[paper-api]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s; orders -> {orders_db}', fg='cyan')
    last_bar_ts = None

    while True:
        now = pd.Timestamp.now(tz="UTC")
        bar_close = pd.Timestamp(_align_bar_close(now.to_pydatetime(), tf_seconds))

        # trigger on closed bar (+delay)
        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close

            # Universe (allow-list only; deny via cfg/env if needed)
            allow = []
            allow_env = os.getenv('RS_UNIVERSE_ALLOW', '')
            if allow_env:
                allow = [s.strip() for s in allow_env.split(',') if s.strip()]
            if not allow:
                allow = list((cfg.get('universe', {}) or {}).get('allow', []) or [])

            # Fetch md for allowed symbols
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
                    feats_df = compute_feats(df, tf_seconds=tf_seconds)
                    if args.hour_cache in ('save', 'load'):
                        cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                    feats = feats_df.iloc[-1].to_dict()
                md[ccxt_sym] = feats
            price_map = {s: float(md[s].get('close') or 0.0) for s in md}

            # ---- Exits (strategy) ----
            for pos in list(pf.positions):
                row = md.get(pos.symbol)
                if row is None:
                    continue
                adj = _call_manage_position(strat, bar_close, pos.symbol, pos, row, pf)
                if getattr(adj, 'action', None) in ('TP', 'SL', 'EXIT'):
                    # derive exit price (prefer explicit)
                    exit_price = getattr(adj, 'exit_price', None)
                    if exit_price is None:
                        exit_price = float(row.get('close') or 0.0)
                    px = float(exit_price) * (1 - port_cfg['slippage_per_side'])

                    side = str(getattr(pos, 'side', 'LONG')).upper()
                    qty = float(getattr(pos, 'qty', 0.0))
                    reason = getattr(adj, 'action', 'exit')

                    pf.close(pos, bar_close, px, reason=reason)

                    cprint(
                        "[close]",
                        bar_close.isoformat(),
                        pos.symbol,
                        side,
                        f"qty={_fmt_float(qty)}",
                        f"exit={_fmt_float(px)}",
                        f"reason={reason}",
                        fg="magenta",
                        bold=True,
                    )

                    insert_order_row(orders_db, {
                        'order_id': str(uuid.uuid4()),
                        'ts_utc': datetime.utcnow().isoformat(),
                        'bar_time_utc': bar_close.isoformat(),
                        'mode': 'paper_api',
                        'symbol': pos.symbol,
                        'side': 'sell' if side == 'LONG' else 'buy',
                        'type': 'market',
                        'price': float(px),
                        'qty': float(qty),
                        'status': 'filled',
                        'reason': reason,
                        'run_id': run_id,
                        'extra': json.dumps({'sim': True})
                    })

            # ---- Entries (strategy) ----
            uni = strat.universe(bar_close, md)
            ranked = strat.rank(bar_close, md, uni)[:top_n]

            # save decisions (for UI)
            try:
                write_decisions(session_db_path, run_id, bar_close, ranked, ranked)
            except Exception:
                pass

            for sym in ranked:
                if not pf.can_open(port_cfg, price_map=price_map):
                    break
                row = md.get(sym)
                if not row:
                    continue
                sig = _call_entry_signal(strat, bar_close, sym, row, pf)
                if sig is None:
                    continue
                sig = _normalize_sig(sig)  # ensure .tags, .take_profit, .stop_price exist

                entry_px = float(row.get('close') or 0.0) * (1 + port_cfg['slippage_per_side'])
                sig_map = {
                    'side': str(getattr(sig, 'side', 'LONG')).upper(),
                    'take_profit': getattr(sig, 'take_profit', None),
                    'stop_price': getattr(sig, 'stop_price', None),
                    'reason': getattr(sig, 'reason', None),
                }
                pos = pf.open(symbol=sym, signal=sig_map, t=bar_close, last_price=entry_px)
                qty = getattr(pos, 'qty', None)
                if qty is None:
                    try:
                        qty = float(getattr(pos, 'notional', 0.0)) / max(float(getattr(pos, 'entry_price', entry_px)), 1e-12)
                    except Exception:
                        qty = 0.0
                    setattr(pos, 'qty', qty)

                insert_order_row(orders_db, {
                    'order_id': str(uuid.uuid4()),
                    'ts_utc': datetime.utcnow().isoformat(),
                    'bar_time_utc': bar_close.isoformat(),
                    'mode': 'paper_api',
                    'symbol': sym,
                    'side': 'buy' if str(sig.side).upper()=='LONG' else 'sell',
                    'type': 'market',
                    'price': float(entry_px),
                    'qty': float(qty),
                    'status': 'filled',
                    'reason': 'entry',
                    'run_id': run_id,
                    'extra': json.dumps({'sim': True, 'tags': getattr(sig, 'tags', [])})
                })

            # Equity snapshot (mark-to-market)
            try:
                pf.mark_equity(price_map)
                cash_val = float(getattr(pf, 'equity', getattr(pf, 'cash', 0.0)))
                eq = {
                    'equity': cash_val + float(getattr(pf, 'unrealized_pnl', 0.0)),
                    'cash': cash_val,
                    'position_value': float(getattr(pf, 'position_value', 0.0)),
                    'realized_pnl_cum': float(getattr(pf, 'realized_pnl_cum', 0.0)),
                    'unrealized_pnl': float(getattr(pf, 'unrealized_pnl', 0.0)),
                }
                write_equity(session_db_path, run_id, bar_close, eq)
            except Exception:
                pass

            # Persist CSVs
            try:
                trades_csv = os.path.join(args.results_dir, 'trades.csv')
                summary_csv = os.path.join(args.results_dir, 'summary.csv')
                pf.save_trades(trades_csv)
                pf.save_summary(summary_csv)
            except Exception:
                pass

            # Pretty-print currently open positions in YELLOW (diagnostics)
            try:
                npos = len(getattr(pf, 'positions', []))
                if npos > 0:
                    for _pos in list(pf.positions):
                        _sym = getattr(_pos, 'symbol', '?')
                        _side = str(getattr(_pos, 'side', '?')).upper()
                        _qty = getattr(_pos, 'qty', None)
                        if _qty is None:
                            try:
                                _qty = float(getattr(_pos, 'notional', 0.0)) / max(float(getattr(_pos, 'entry_price', getattr(_pos, 'entry', 0.0))), 1e-12)
                            except Exception:
                                _qty = None
                        _entry = (getattr(_pos, 'entry', None)
                                  if hasattr(_pos, 'entry') else getattr(_pos, 'entry_price', getattr(_pos, 'price', None)))
                        _tp = (getattr(_pos, 'tp', None)
                               if hasattr(_pos, 'tp') else getattr(_pos, 'take_profit', getattr(_pos, 'tp_price', None)))
                        _sl = (getattr(_pos, 'sl', None)
                               if hasattr(_pos, 'sl') else getattr(_pos, 'stop_price', getattr(_pos, 'sl_price', None)))
                        cprint(
                            "[open]",
                            bar_close.isoformat(),
                            _sym,
                            _side,
                            f"qty={_fmt_float(_qty)}",
                            f"entry={_fmt_float(_entry)}",
                            f"tp={_fmt_float(_tp)}",
                            f"sl={_fmt_float(_sl)}",
                            fg="yellow",
                            bold=True,
                        )
            except Exception:
                pass

            if getattr(args, 'heat_report', False) and len(pf.positions) == 0:
                try:
                    _print_heat_from_strategy(strat, 'paper-api', bar_close, md, uni)
                except Exception:
                    pass

            cprint("[paper-api]", f"bar {bar_close.isoformat()} processed: positions={len(pf.positions)}", fg="cyan")
        else:
            dot()
        time.sleep(getattr(args, 'poll_sec', 10))
