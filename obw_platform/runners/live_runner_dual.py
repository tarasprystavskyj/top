# live_runner_dual.py — LIVE mode for dual long+short cryptomine strategy
from .common import *
from .common import _tf_to_seconds, _align_bar_close, load_positions, save_positions, make_bot_id, db_load_open_positions, db_upsert_open_position, db_mark_closed

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
    print('.', end='', flush=True)

import importlib, os, sys, math, uuid, datetime as _dt

DEBUG_OPEN = False


def pos_key(sym: str, side: str) -> str:
    return f"{sym}|{side.upper()}"


def split_pos_key(key: str):
    if '|' in key:
        a, b = key.rsplit('|', 1)
        return a, b.upper()
    return key, 'LONG'




def _dbg(*parts):
    if DEBUG_OPEN:
        cprint('[dual dbg]', *parts, fg='yellow', dim=True)


def _safe_get_state_snapshot(strat, sym: str):
    try:
        st = strat._get_state(sym)
        return {
            'avg_price': getattr(st, 'avg_price', None),
            'num_buys': getattr(st, 'num_buys', None),
            'pos_size': getattr(st, 'pos_size', None),
            'next_level_price': getattr(st, 'next_level_price', None),
            'reset_pending': getattr(st, 'reset_pending', None),
            'trailing_active': getattr(st, 'trailing_active', None),
        }
    except Exception:
        return None


def _cfg_get_nested(cfg: dict, dotted: str, _missing=object()):
    cur = cfg
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _missing
        cur = cur[part]
    return cur


def _cfg_pick(cfg: dict, candidates, default=None):
    _missing = object()
    for key in candidates:
        v = _cfg_get_nested(cfg, key, _missing)
        if v is not _missing:
            return v, f"yaml:{key}"
    return default, 'default'


def _sig_get(sig, key, default=None):
    try:
        if hasattr(sig, key):
            return getattr(sig, key)
    except Exception:
        pass
    try:
        if isinstance(sig, dict) and key in sig:
            return sig.get(key, default)
    except Exception:
        pass
    return default


def _to_pos_like(rec: dict):
    class Pos: pass
    p = Pos()
    p.qty = float(rec.get('qty', 0.0))
    p.entry = float(rec.get('entry', 0.0))
    p.side = str(rec.get('side', 'LONG')).upper()
    p.tp = rec.get('tp_price')
    p.sl = rec.get('sl_price')
    return p


def _update_rec_from_pos(rec: dict, pos_like, strat):
    rec['qty'] = float(pos_like.qty)
    rec['entry'] = float(pos_like.entry)
    try:
        tp, sl = strat._entry_tp_sl(float(pos_like.entry))
        rec['tp_price'] = float(tp) if tp is not None else None
        rec['sl_price'] = float(sl) if sl is not None else None
    except Exception:
        pass
    return rec


def load_strategy_pair(cfg: dict):
    def _load(path_cls: str):
        mod_path, cls_name = path_cls.rsplit('.', 1)
        mod = importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
        return cls(cfg)
    return _load(cfg['strategy_class_long']), _load(cfg['strategy_class_short'])


def qty_for_notional(mkt: dict, notional: float, price: float):
    min_qty = float(mkt.get('limits', {}).get('amount', {}).get('min') or 0.0)
    step = float(mkt.get('precision', {}).get('amount') or 0.0)
    min_notional_req = float(mkt.get('limits', {}).get('cost', {}).get('min') or 0.0)
    if step and step > 0:
        qty = max(min_qty, math.floor(notional / max(price, 1e-9) / step) * step)
    else:
        qty = max(min_qty, notional / max(price, 1e-9))
    return qty, min_notional_req, step, min_qty


def place_open(fetcher: CCXTFetcher, sym: str, side: str, notional: float, price: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    qty, min_notional_req, step, min_qty = qty_for_notional(mkt, notional, price)
    if min_notional_req > notional + 1e-9:
        return {'ok': False, 'skip_reason': f'min_notional {min_notional_req:.6g} > {notional:.6g}', 'qty': qty}
    order_side = 'buy' if side.upper() == 'LONG' else 'sell'
    # Do NOT send reduceOnly on opens. BingX hedge mode rejects even reduceOnly=false.
    params = {}
    if position_mode == 'hedge':
        params['positionSide'] = side.upper()
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'market', order_side, qty, None, params)
        sleep_ms(RATE_MS)
        return {'ok': True, 'order': od, 'qty': qty, 'params': params}
    except Exception as e:
        msg = str(e).lower()
        if ('reduceonly' in msg) or ('reduce only' in msg):
            try:
                p2 = {k: v for k, v in params.items() if k != 'reduceOnly'}
                od = fetcher.ex.create_order(ccxt_sym, 'market', order_side, qty, None, p2)
                sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': p2, 'retry': True, 'note': 'auto: removed reduceOnly on open'}
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty}
        if ('one-way mode' in msg) or ('positionside' in msg):
            try:
                od = fetcher.ex.create_order(ccxt_sym, 'market', order_side, qty, None, {})
                sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': {}, 'retry': True}
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty}
        return {'ok': False, 'error': str(e), 'qty': qty}


def place_open_qty(fetcher: CCXTFetcher, sym: str, side: str, qty: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    order_side = 'buy' if side.upper() == 'LONG' else 'sell'
    params = {}
    if position_mode == 'hedge':
        params['positionSide'] = side.upper()
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'market', order_side, qty, None, params)
        sleep_ms(RATE_MS)
        return {'ok': True, 'order': od, 'qty': qty, 'params': params}
    except Exception as e:
        msg = str(e).lower()
        if ('one-way mode' in msg) or ('positionside' in msg):
            try:
                od = fetcher.ex.create_order(ccxt_sym, 'market', order_side, qty, None, {})
                sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': {}, 'retry': True}
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty}
        return {'ok': False, 'error': str(e), 'qty': qty}


def place_reduce_only(fetcher: CCXTFetcher, sym: str, entry_side: str, qty: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    close_side = 'sell' if entry_side.upper() == 'LONG' else 'buy'
    params = {'reduceOnly': True}
    if position_mode == 'hedge':
        params['positionSide'] = entry_side.upper()
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'market', close_side, qty, None, params)
        sleep_ms(RATE_MS)
        return {'ok': True, 'order': od, 'qty': qty}
    except Exception as e:
        msg = str(e).lower()
        if ('one-way mode' in msg) or ('positionside' in msg):
            try:
                od = fetcher.ex.create_order(ccxt_sym, 'market', close_side, qty, None, {'reduceOnly': True})
                sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'retry': True}
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty}
        return {'ok': False, 'error': str(e), 'qty': qty}


def _resolve_runner_cfg(cfg: dict):
    tf_v, _ = _cfg_pick(cfg, ['timeframe', 'runner.timeframe', 'live.timeframe'], '30s')
    top_n_v, _ = _cfg_pick(cfg, ['top_n', 'runner.top_n', 'live.top_n'], 1)
    position_mode_v, _ = _cfg_pick(cfg, ['position_mode', 'runner.position_mode', 'live.position_mode'], 'hedge')
    notional_long_v, _ = _cfg_pick(cfg, ['portfolio.position_notional_long', 'portfolio.position_notional', 'position_notional_long'], 2.0)
    notional_short_v, _ = _cfg_pick(cfg, ['portfolio.position_notional_short', 'portfolio.position_notional', 'position_notional_short'], 20.0)
    return {
        'timeframe': str(tf_v),
        'top_n': int(top_n_v),
        'position_mode': str(position_mode_v),
        'notional_long': float(notional_long_v),
        'notional_short': float(notional_short_v),
    }


def _maybe_apply_manage_result(fetcher, key: str, rec: dict, row: dict, strat, positions: dict, results_dir: str, position_mode: str, session_db_path: str, bot_id: str):
    sym = rec['symbol']
    side = rec['side'].upper()
    pos_before = _to_pos_like(rec)
    qty_before = float(pos_before.qty)
    entry_before = float(pos_before.entry)

    ex = strat.manage_position(sym, row, pos_before, ctx={})

    # 1) Full close
    if ex and getattr(ex, 'action', None) in ('TP', 'SL', 'EXIT'):
        qty_close = float(rec.get('qty', 0.0))
        if qty_close > 0:
            res = place_reduce_only(fetcher, sym, side, qty_close, position_mode)
            if res.get('ok'):
                cprint('[close OK]', sym, side, f'qty={qty_close:.6g}', getattr(ex, 'reason', ''), fg='green', bold=True)
                try:
                    db_mark_closed(session_db_path, bot_id, rec.get('order_id'), _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat())
                except Exception:
                    pass
                positions.pop(key, None)
                save_positions(results_dir, positions)
                return True
            else:
                cprint('[close FAIL]', sym, side, res, fg='red', file=sys.stderr)
        return False

    # 2) Partial close
    if ex and getattr(ex, 'action', None) == 'TP_PARTIAL':
        qty_frac = max(0.0, min(1.0, float(getattr(ex, 'qty_frac', 0.0) or 0.0)))
        qty_close = float(rec.get('qty', 0.0)) * qty_frac
        if qty_close > 0:
            res = place_reduce_only(fetcher, sym, side, qty_close, position_mode)
            if res.get('ok'):
                rec['qty'] = max(0.0, float(rec.get('qty', 0.0)) - qty_close)
                rec['entry'] = float(pos_before.entry)
                rec = _update_rec_from_pos(rec, pos_before, strat)
                if rec['qty'] <= 1e-12:
                    positions.pop(key, None)
                    try:
                        db_mark_closed(session_db_path, bot_id, rec.get('order_id'), _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat())
                    except Exception:
                        pass
                else:
                    positions[key] = rec
                    try:
                        db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN'})
                    except Exception:
                        pass
                save_positions(results_dir, positions)
                cprint('[partial OK]', sym, side, f'qty={qty_close:.6g}', getattr(ex, 'reason', ''), fg='yellow', bold=True)
                return True
            else:
                cprint('[partial FAIL]', sym, side, res, fg='red', file=sys.stderr)
        return False

    # 3) DCA add inferred by qty increase inside strategy
    qty_after = float(pos_before.qty)
    entry_after = float(pos_before.entry)
    if qty_after > qty_before + 1e-12:
        delta_qty = qty_after - qty_before
        px = float(row.get('close') or 0.0)
        res = place_open_qty(fetcher, sym, side, delta_qty, position_mode)
        if res.get('ok'):
            rec['qty'] = qty_after
            rec['entry'] = entry_after
            rec = _update_rec_from_pos(rec, pos_before, strat)
            positions[key] = rec
            try:
                db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN'})
            except Exception:
                pass
            save_positions(results_dir, positions)
            cprint('[dca OK]', sym, side, f'delta_qty={delta_qty:.6g}', f'new_qty={qty_after:.6g}', fg='cyan', bold=True)
            return True
        else:
            cprint('[dca FAIL]', sym, side, res, fg='red', file=sys.stderr)
            return False

    # 4) Passive entry/avg update (e.g. pending_new_entry after partial)
    if abs(entry_after - entry_before) > 1e-12:
        rec['entry'] = entry_after
        rec = _update_rec_from_pos(rec, pos_before, strat)
        positions[key] = rec
        try:
            db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN'})
        except Exception:
            pass
        save_positions(results_dir, positions)
    return False


def run_live(cfg: dict, args):
    assert ccxt is not None, 'ccxt required for LIVE mode'

    if args.env_file and os.path.exists(args.env_file):
        for line in open(args.env_file, 'r', encoding='utf-8').read().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

    api_k = os.environ.get('BINGX_KEY', '')
    api_s = os.environ.get('BINGX_SECRET', '')
    cprint('[LIVE API]', f'key="{mask(api_k)}", secret="{mask(api_s)}"', fg='cyan')

    fetcher = CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=args.debug)
    strat_long, strat_short = load_strategy_pair(cfg)
    rcfg = _resolve_runner_cfg(cfg)
    tf = rcfg['timeframe']
    tf_sec = _tf_to_seconds(tf)
    top_n = rcfg['top_n']
    notional_long = rcfg['notional_long']
    notional_short = rcfg['notional_short']
    position_mode = rcfg['position_mode']

    cprint('[cfg]', f"timeframe={tf} top_n={top_n} first_usdt_long={getattr(strat_long,'first_usdt',None)} first_usdt_short={getattr(strat_short,'first_usdt',None)} position_mode={position_mode}", fg='magenta')
    _dbg('long params', cfg.get('strategy_params_long', {}))
    _dbg('short params', cfg.get('strategy_params_short', {}))

    os.makedirs(args.results_dir, exist_ok=True)
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, args.session_db, args.cache_out)
    run_id = _dt.datetime.utcnow().strftime('LIVE_DUAL_%Y%m%d_%H%M%S')
    write_config_snapshot(session_db_path, run_id, cfg)
    global DEBUG_OPEN
    DEBUG_OPEN = bool(getattr(args, 'debug', False) or cfg.get('debug_open', False))

    positions = load_positions(args.results_dir)
    bot_id = make_bot_id(args.results_dir, args.exchange, tf)
    try:
        db_positions = db_load_open_positions(session_db_path, bot_id)
        if db_positions:
            positions = db_positions
    except Exception:
        pass

    last_bar_ts = None
    cprint('[live dual]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s', fg='cyan')
    while True:
        now = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)
        bar_close = _align_bar_close(now, tf_sec)

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

        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close
            md = {}
            for ccxt_sym in universe:
                feats = {}
                if args.hour_cache == 'load':
                    feats = read_hour_cache_row(cache_out_path, ccxt_sym, bar_close)
                if not feats:
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=tf, limit=max(60, args.limit_klines))
                    if df is not None and len(df) >= 30:
                        feats_df = compute_feats(df, tf_seconds=tf_sec)
                        if args.hour_cache in ('save', 'load'):
                            try:
                                cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                            except Exception:
                                pass
                        feats = feats_df.iloc[-1].to_dict()
                    else:
                        # Fallback for exchanges/timeframes without OHLCV support (e.g. 30s on some venues).
                        try:
                            px = fetcher.fetch_ticker_price(ccxt_sym)
                        except Exception:
                            px = None
                        if px is not None:
                            feats = {
                                'close': float(px),
                                'atr_ratio': 0.0,
                                'dp6h': 0.0,
                                'dp12h': 0.0,
                                'quote_volume': 0.0,
                                'qv_24h': 0.0,
                            }
                            _dbg('md-fallback', ccxt_sym, 'ticker_close=', px, 'timeframe=', tf)
                if not feats:
                    _dbg('md-miss', ccxt_sym, 'timeframe=', tf)
                    continue
                feats['datetime_utc'] = bar_close.isoformat()
                md[ccxt_sym] = feats
                dot()

            _dbg('bar', bar_close.isoformat(), 'universe=', len(universe), 'md=', len(md), 'symbols=', list(md.keys())[:5])

            # manage existing legs
            for key, rec in list(positions.items()):
                sym, side = split_pos_key(key)
                row = md.get(sym)
                if row is None:
                    continue
                strat = strat_long if side == 'LONG' else strat_short
                _dbg('manage', sym, side, 'qty=', rec.get('qty'), 'entry=', rec.get('entry'), 'close=', row.get('close'))
                _maybe_apply_manage_result(fetcher, key, rec, row, strat, positions, args.results_dir, position_mode, session_db_path, bot_id)

            # new entries, both legs can coexist
            uni = strat_long.universe(bar_close, md)
            ranked = strat_long.rank(bar_close, md, uni)[:top_n]
            _dbg('ranked', ranked)
            for sym in ranked:
                row = md.get(sym)
                if row is None:
                    continue
                key_long = pos_key(sym, 'LONG')
                key_short = pos_key(sym, 'SHORT')

                if key_long not in positions:
                    _dbg('entry-check', sym, 'LONG', 'close=', row.get('close'))
                    sig = strat_long.entry_signal(True, sym, row, ctx={})
                    _dbg('entry-sig', sym, 'LONG', sig)
                    if sig is None:
                        _dbg('entry-none', sym, 'LONG', _safe_get_state_snapshot(strat_long, sym))
                    if sig is not None:
                        entry_px = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                        first_usdt = float(getattr(strat_long, 'first_usdt', 0.0) or 0.0)
                        open_qty = (first_usdt / max(entry_px, 1e-12)) if first_usdt > 0 else (notional_long / max(entry_px, 1e-12))
                        res = place_open_qty(fetcher, sym, 'LONG', open_qty, position_mode)
                        if res.get('ok'):
                            qty = float(res['qty'])
                            rec = {'symbol': sym, 'side': 'LONG', 'qty': qty, 'entry': float(entry_px), 'ts_open': bar_close.isoformat(), 'run_id': run_id, 'order_id': str(uuid.uuid4())}
                            rec = _update_rec_from_pos(rec, _to_pos_like({'qty': qty, 'entry': entry_px, 'side': 'LONG'}), strat_long)
                            positions[key_long] = rec
                            save_positions(args.results_dir, positions)
                            try:
                                db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN', 'exchange': args.exchange, 'timeframe': tf})
                            except Exception:
                                pass
                            cprint('[open OK]', sym, 'LONG', f'qty={qty:.6g} px={entry_px}', fg='green', bold=True)
                        else:
                            cprint('[open FAIL]', sym, 'LONG', res, fg='red', bold=True)

                if key_short not in positions:
                    _dbg('entry-check', sym, 'SHORT', 'close=', row.get('close'))
                    sig = strat_short.entry_signal(True, sym, row, ctx={})
                    _dbg('entry-sig', sym, 'SHORT', sig)
                    if sig is None:
                        _dbg('entry-none', sym, 'SHORT', _safe_get_state_snapshot(strat_short, sym))
                    if sig is not None:
                        entry_px = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                        first_usdt = float(getattr(strat_short, 'first_usdt', 0.0) or 0.0)
                        open_qty = (first_usdt / max(entry_px, 1e-12)) if first_usdt > 0 else (notional_short / max(entry_px, 1e-12))
                        res = place_open_qty(fetcher, sym, 'SHORT', open_qty, position_mode)
                        if res.get('ok'):
                            qty = float(res['qty'])
                            rec = {'symbol': sym, 'side': 'SHORT', 'qty': qty, 'entry': float(entry_px), 'ts_open': bar_close.isoformat(), 'run_id': run_id, 'order_id': str(uuid.uuid4())}
                            rec = _update_rec_from_pos(rec, _to_pos_like({'qty': qty, 'entry': entry_px, 'side': 'SHORT'}), strat_short)
                            positions[key_short] = rec
                            save_positions(args.results_dir, positions)
                            try:
                                db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN', 'exchange': args.exchange, 'timeframe': tf})
                            except Exception:
                                pass
                            cprint('[open OK]', sym, 'SHORT', f'qty={qty:.6g} px={entry_px}', fg='green', bold=True)
                        else:
                            cprint('[open FAIL]', sym, 'SHORT', res, fg='red', bold=True)

            cprint('[live dual]', f'bar={bar_close.isoformat()} open_legs={len(positions)}', fg='cyan', bold=(len(positions) > 0))
        else:
            dot()
        time.sleep(args.poll_sec)
