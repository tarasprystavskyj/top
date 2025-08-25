# live_runner.py — LIVE mode
from .common import *
from .common import _tf_to_seconds, _align_bar_close, load_positions, save_positions, print_and_save_heat_from_strategy, make_bot_id, db_load_open_positions, db_upsert_open_position, db_mark_closed

# color print fallback
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
import os, sys, math, uuid, datetime as _dt, os

def _cfg_get_nested(cfg: dict, dotted: str, _missing=object()):
    """Return cfg value by dotted path like "runner.top_n" or _missing."""
    cur = cfg
    for part in dotted.split('.'):
        if not isinstance(cur, dict) or part not in cur:
            return _missing
        cur = cur[part]
    return cur

def _cfg_pick(cfg: dict, candidates, default=None):
    """Try multiple dotted keys; return (value, origin_key or 'default')."""
    _missing = object()
    for key in candidates:
        v = _cfg_get_nested(cfg, key, _missing)
        if v is not _missing:
            return v, f"yaml:{key}"
    return default, "default"

def _debug_dump_effective(cfg: dict, strat, args, resolved: dict, env_over: dict):
    try:
        cprint("[cfg.dump] --- runner-args ---", fg="magenta", bold=True)
        cprint("  poll_sec:", args.poll_sec, "bar_delay_sec:", args.bar_delay_sec, "limit_klines:", args.limit_klines, fg="magenta", dim=True)
        cprint("  results_dir:", args.results_dir, "session_db:", args.session_db, "cache_out:", args.cache_out, fg="magenta", dim=True)
        cprint("  exchange:", args.exchange, "symbol_format:", args.symbol_format, "hour_cache:", args.hour_cache, fg="magenta", dim=True)
        cprint("[cfg.dump] --- runner-from-yaml ---", fg="cyan", bold=True)
        for k, (val, origin) in resolved.items():
            cprint(f"  {k} = {val!r}   ({origin})", fg="cyan", dim=True)
        if env_over:
            cprint("[cfg.dump] --- env-overrides ---", fg="yellow", bold=True)
            for k, v in env_over.items():
                cprint(f"  {k} = {v!r}", fg="yellow", dim=True)
        scfg = getattr(strat, 'cfg', {})
        if isinstance(scfg, dict) and scfg:
            cprint("[cfg.dump] --- strategy.cfg ---", fg="green", bold=True)
            for k in sorted(scfg.keys()):
                cprint(f"  {k}: {scfg[k]!r}", fg="green", dim=True)
    except Exception as e:
        cprint("[cfg.dump] error:", e, fg="red")


def log_skip_reason(sym, reason):
    try:
        cprint('[skip]', sym, '-', reason, fg='yellow', dim=True)
    except Exception:
        print('[skip]', sym, '-', reason)

def _dbg(*parts):
    try:
        if DEBUG_OPEN:
            cprint('[open?]', *parts, fg='yellow', dim=True)
    except NameError:
        pass

def get_exchange_open_positions(fetcher: CCXTFetcher):
    pos_map = {}
    try:
        pos_list = fetcher.ex.fetch_positions()
    except Exception as e:
        cprint("[positions fetch]", e, fg="red")
        pos_list = []
    for p in (pos_list or []):
        try:
            sym0 = p.get('symbol') if isinstance(p, dict) else None
            sym = fetcher.resolve_symbol(sym0) or sym0
            if not sym:
                continue
            qty = None
            if isinstance(p, dict):
                if p.get('contracts') is not None:
                    qty = float(p.get('contracts') or 0.0)
                elif isinstance(p.get('info', {}), dict):
                    info = p['info']
                    for k in ('positionAmt','positionAmount','position','size','contracts','available','holding'):
                        if k in info and info.get(k) not in (None, ''):
                            try:
                                qty = float(info.get(k))
                                break
                            except Exception:
                                pass
            if qty is None or abs(qty) <= 0.0:
                continue
            side = (p.get('side') or ('long' if qty > 0 else 'short')).upper()
            entry = None
            for k in ('entryPrice','entry'):
                try:
                    v = p.get(k)
                    if v:
                        entry = float(v)
                        break
                except Exception:
                    pass
            pos_map[sym] = {'qty': abs(qty), 'side': 'LONG' if side.startswith('LONG') else 'SHORT', 'entry': entry}
        except Exception:
            continue
    return pos_map

def qty_for_notional(mkt: dict, notional: float, price: float):
    min_qty = float(mkt.get('limits', {}).get('amount', {}).get('min') or 0.0)
    step = float(mkt.get('precision', {}).get('amount') or 0.0)
    min_notional_req = float(mkt.get('limits', {}).get('cost', {}).get('min') or 0.0)
    if step and step > 0:
        qty = max(min_qty, math.floor(notional / max(price, 1e-9) / step) * step)
    else:
        qty = max(min_qty, notional / max(price, 1e-9))
    return qty, min_notional_req, step, min_qty

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

def place_open_long(fetcher: CCXTFetcher, sym: str, notional: float, price: float, position_mode: str, tp_price=None, sl_price=None):
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    qty, min_notional_req, step, min_qty = qty_for_notional(mkt, notional, price)
    if min_notional_req > notional + 1e-9:
        return {'ok': False, 'skip_reason': f'min_notional {min_notional_req:.6g} > {notional:.6g}', 'qty': qty}

    def _try(params):
        try:
            od = fetcher.ex.create_order(ccxt_sym, 'market', 'buy', qty, None, params)
            sleep_ms(RATE_MS)
            return {'ok': True, 'order': od, 'qty': qty, 'params': params}
        except Exception as e:
            return {'ok': False, 'error': str(e), 'params': params}

    base_params = {'reduceOnly': False}
    if position_mode == 'hedge':
        base_params['positionSide'] = 'LONG'

    param_candidates = []
    if tp_price is not None or sl_price is not None:
        p = dict(base_params)
        if tp_price is not None:
            p['takeProfit'] = float(tp_price)
            p['takeProfitPrice'] = float(tp_price)
        if sl_price is not None:
            p['stopLoss'] = float(sl_price)
            p['stopLossPrice'] = float(sl_price)
            p['stopPrice'] = float(sl_price)
        param_candidates.append(p)
    param_candidates.append(dict(base_params))

    last_res = None
    for params in param_candidates:
        res = _try(params)
        last_res = res
        if res['ok']:
            res['tp_price'] = tp_price
            res['sl_price'] = sl_price
            return res

        msg = (res.get('error') or '').lower()
        if ('one-way mode' in msg) or ('positionside' in msg):
            p2 = {k: v for k, v in params.items() if k != 'positionSide'}
            res2 = _try(p2)
            if res2['ok']:
                res2['tp_price'] = tp_price
                res2['sl_price'] = sl_price
                res2['retry'] = True
                res2['note'] = 'auto: one-way detected (no positionSide)'
                return res2
            p3 = dict(p2, positionSide='BOTH')
            res3 = _try(p3)
            if res3['ok']:
                res3['tp_price'] = tp_price
                res3['sl_price'] = sl_price
                res3['retry'] = True
                res3['note'] = 'auto: one-way detected (BOTH)'
                return res3
            last_res = res3
        elif ('min amount' in msg) and step > 0:
            try:
                qty2 = max(min_qty, qty + step)
                od = fetcher.ex.create_order(ccxt_sym, 'market', 'buy', qty2, None, params)
                sleep_ms(RATE_MS)
                r = {'ok': True, 'order': od, 'qty': qty2, 'params': params, 'retry': True}
                r['tp_price'] = tp_price
                r['sl_price'] = sl_price
                return r
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty2, 'params': params}
    return last_res or {'ok': False, 'error': 'unknown error'}

def place_open_short(fetcher: CCXTFetcher, sym: str, notional: float, price: float, position_mode: str, tp_price=None, sl_price=None):
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    qty, min_notional_req, step, min_qty = qty_for_notional(mkt, notional, price)
    if min_notional_req > notional + 1e-9:
        return {'ok': False, 'skip_reason': f'min_notional {min_notional_req:.6g} > {notional:.6g}', 'qty': qty}

    def _try(params):
        try:
            od = fetcher.ex.create_order(ccxt_sym, 'market', 'sell', qty, None, params)
            sleep_ms(RATE_MS)
            return {'ok': True, 'order': od, 'qty': qty, 'params': params}
        except Exception as e:
            return {'ok': False, 'error': str(e), 'params': params}

    base_params = {'reduceOnly': False}
    if position_mode == 'hedge':
        base_params['positionSide'] = 'SHORT'

    param_candidates = []
    if tp_price is not None or sl_price is not None:
        p = dict(base_params)
        if tp_price is not None:
            p['takeProfit'] = float(tp_price)
            p['takeProfitPrice'] = float(tp_price)
        if sl_price is not None:
            p['stopLoss'] = float(sl_price)
            p['stopLossPrice'] = float(sl_price)
            p['stopPrice'] = float(sl_price)
        param_candidates.append(p)
    param_candidates.append(dict(base_params))

    last_res = None
    for params in param_candidates:
        res = _try(params)
        last_res = res
        if res['ok']:
            res['tp_price'] = tp_price
            res['sl_price'] = sl_price
            return res

        msg = (res.get('error') or '').lower()
        if ('one-way mode' in msg) or ('positionside' in msg):
            p2 = {k: v for k, v in params.items() if k != 'positionSide'}
            res2 = _try(p2)
            if res2['ok']:
                res2['tp_price'] = tp_price
                res2['sl_price'] = sl_price
                res2['retry'] = True
                res2['note'] = 'auto: one-way detected (no positionSide)'
                return res2
            p3 = dict(p2, positionSide='BOTH')
            res3 = _try(p3)
            if res3['ok']:
                res3['tp_price'] = tp_price
                res3['sl_price'] = sl_price
                res3['retry'] = True
                res3['note'] = 'auto: one-way detected (BOTH)'
                return res3
            last_res = res3
        elif ('min amount' in msg) and step > 0:
            try:
                qty2 = max(min_qty, qty + step)
                od = fetcher.ex.create_order(ccxt_sym, 'market', 'sell', qty2, None, params)
                sleep_ms(RATE_MS)
                r = {'ok': True, 'order': od, 'qty': qty2, 'params': params, 'retry': True}
                r['tp_price'] = tp_price
                r['sl_price'] = sl_price
                return r
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty2, 'params': params}
    return last_res or {'ok': False, 'error': 'unknown error'}

def place_reduce_only(fetcher: CCXTFetcher, sym: str, side_close: str, qty: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym)
    params = {'reduceOnly': True}
    if position_mode == 'hedge':
        params['positionSide'] = 'LONG' if side_close.lower() == 'sell' else 'SHORT'
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'market', side_close, qty, None, params)
        sleep_ms(RATE_MS)
        return od
    except Exception as e:
        msg = str(e).lower()
        if ('one-way mode' in msg) or ('positionside' in msg):
            try:
                params2 = {'reduceOnly': True}
                od = fetcher.ex.create_order(ccxt_sym, 'market', side_close, qty, None, params2)
                sleep_ms(RATE_MS)
                return od
            except Exception:
                pass
        cprint('[live reduceOnly]', sym, ':', e, fg='red', file=sys.stderr)
        return None


def _report_close_cooldown(sym: str, pos_rec: dict, px: float):
    try:
        side = str(pos_rec.get('side', 'LONG')).upper()
        tp = pos_rec.get('tp_price'); sl = pos_rec.get('sl_price')

        def _to_f(v):
            try:
                return float(v) if v is not None else None
            except Exception:
                return None
        tp = _to_f(tp); sl = _to_f(sl)
        try:
            px = float(px)
        except Exception:
            return

        tp_gap = sl_gap = None
        if side == 'LONG':
            if tp is not None:
                tp_gap = max(0.0, (tp - px) / max(px, 1e-12)) * 100.0
            if sl is not None:
                sl_gap = max(0.0, (px - sl) / max(px, 1e-12)) * 100.0
        else:  # SHORT
            if tp is not None:
                tp_gap = max(0.0, (px - tp) / max(px, 1e-12)) * 100.0
            if sl is not None:
                sl_gap = max(0.0, (sl - px) / max(px, 1e-12)) * 100.0

        nearest_label, nearest_val = 'n/a', None
        candidates = []
        if tp_gap is not None: candidates.append(('TP', tp_gap))
        if sl_gap is not None: candidates.append(('SL', sl_gap))
        if candidates:
            nearest_label, nearest_val = min(candidates, key=lambda kv: kv[1])

        def fmt(v): return f"{v:.2f}%" if v is not None else "n/a"
        cprint("[close-check]", sym, f"side={side}",
               f"tp_gap={fmt(tp_gap)}", f"sl_gap={fmt(sl_gap)}",
               f"nearest={fmt(nearest_val)} ({nearest_label})",
               fg="magenta", dim=True)
    except Exception:
        pass


def load_strategy(path_cls: str, cfg: dict):
    mod_path, cls_name = path_cls.rsplit('.', 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(cfg)

def _close_if_hit(fetcher: CCXTFetcher, sym: str, entry_side: str, px: float, pos_rec: dict, position_mode: str):
    side = str(entry_side or pos_rec.get('side','LONG')).upper()
    tp = pos_rec.get('tp_price'); sl = pos_rec.get('sl_price')
    try:
        tp = float(tp) if tp is not None else None
        sl = float(sl) if sl is not None else None
    except Exception:
        tp, sl = None, None
    if side == 'LONG':
        if tp is not None and px >= tp:
            od = place_reduce_only(fetcher, sym, 'sell', float(pos_rec.get('qty', 0.0)), position_mode)
            if od:
                cprint('[tp close]', sym, f'@~{px:.6g} tp={tp:.6g}', fg='green', bold=True)
                return True
        if sl is not None and px <= sl:
            od = place_reduce_only(fetcher, sym, 'sell', float(pos_rec.get('qty', 0.0)), position_mode)
            if od:
                cprint('[sl close]', sym, f'@~{px:.6g} sl={sl:.6g}', fg='red', bold=True)
                return True
    elif side == 'SHORT':
        if tp is not None and px <= tp:
            od = place_reduce_only(fetcher, sym, 'buy', float(pos_rec.get('qty', 0.0)), position_mode)
            if od:
                cprint('[tp close]', sym, f'@~{px:.6g} tp={tp:.6g}', fg='green', bold=True)
                return True
        if sl is not None and px >= sl:
            od = place_reduce_only(fetcher, sym, 'buy', float(pos_rec.get('qty', 0.0)), position_mode)
            if od:
                cprint('[sl close]', sym, f'@~{px:.6g} sl={sl:.6g}', fg='red', bold=True)
                return True
    return False
    tp = float(pos_rec.get('tp_price') or 0.0) or None
    sl = float(pos_rec.get('sl_price') or 0.0) or None
    if tp is not None and px >= tp:
        od = place_reduce_only(fetcher, sym, 'sell', float(pos_rec.get('qty', 0.0)), position_mode)
        if od:
            cprint('[tp close]', sym, f'@~{px:.6g} tp={tp:.6g}', fg='green', bold=True)
            return True
    if sl is not None and px <= sl:
        od = place_reduce_only(fetcher, sym, 'sell', float(pos_rec.get('qty', 0.0)), position_mode)
        if od:
            cprint('[sl close]', sym, f'@~{px:.6g} sl={sl:.6g}', fg='red', bold=True)
            return True
    return False

def run_live(cfg: dict, args):
    assert ccxt is not None, 'ccxt required for LIVE mode'
    strat_path = cfg.get('strategy_class', 'strategies.cross_sectional_rs.CrossSectionalRS')
    strat = load_strategy(strat_path, cfg)

    if args.env_file and os.path.exists(args.env_file):
        for line in open(args.env_file, 'r', encoding='utf-8').read().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1)
                os.environ[k.strip()] = v.strip()

    api_k = os.environ.get('BINGX_KEY', '')
    api_s = os.environ.get('BINGX_SECRET', '')
    cprint('[LIVE API]', f'key="{mask(api_k)}", secret="{mask(api_s)}"', fg='cyan')

    fetcher = CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=args.debug)

    top_n_v, top_n_origin = _cfg_pick(cfg, ['top_n','runner.top_n','live.top_n','strategy_params.top_n','strategy.top_n'], 4)
    notional_v, notional_origin = _cfg_pick(cfg, ['notional','runner.notional','live.notional','portfolio.position_notional'], 2.2)
    position_mode_v, position_mode_origin = _cfg_pick(cfg, ['position_mode','runner.position_mode','live.position_mode','session.position_mode'], 'hedge')
    tf_v, tf_origin = _cfg_pick(cfg, ['timeframe','runner.timeframe','live.timeframe'], '1h')
    top_n = int(top_n_v)
    notional = float(notional_v)
    position_mode = str(position_mode_v)
    tf = str(tf_v)
    tf_sec = _tf_to_seconds(tf)

    # Optional: open entries based on heat (even if entry_signal is None)
    open_on_heat_v, open_on_heat_origin = _cfg_pick(cfg, ['open_on_heat','runner.open_on_heat','live.open_on_heat'], False)
    open_heat_min_v, open_heat_min_origin = _cfg_pick(cfg, ['open_heat_min','runner.open_heat_min','live.open_heat_min'], 0.80)
    open_on_heat = bool(open_on_heat_v)
    open_heat_min = float(open_heat_min_v)
    cprint('[cfg]', f'top_n={top_n}, notional={notional}, timeframe={tf}, position_mode={position_mode}, open_on_heat={open_on_heat}, heat_min={open_heat_min}', fg='magenta')
    if getattr(args, 'debug', False):
        _debug_dump_effective(cfg, strat, args,
            resolved={
                'top_n': (top_n, top_n_origin),
                'notional': (notional, notional_origin),
                'position_mode': (position_mode, position_mode_origin),
                'timeframe': (tf, tf_origin),
                'open_on_heat': (bool(open_on_heat_v), open_on_heat_origin),
                'open_heat_min': (float(open_heat_min_v), open_heat_min_origin),
            }, env_over={}
        )

    os.makedirs(args.results_dir, exist_ok=True)
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, args.session_db, args.cache_out)

    run_id = _dt.datetime.utcnow().strftime('LIVE_%Y%m%d_%H%M%S')
    write_config_snapshot(session_db_path, run_id, cfg)

    global DEBUG_OPEN
    DEBUG_OPEN = bool(getattr(args, 'debug', False) or cfg.get('debug_open', False) or bool(getattr(args, 'heat_report', False)))

    positions = load_positions(args.results_dir)
    bot_id = make_bot_id(args.results_dir, args.exchange, tf)
    db_positions = db_load_open_positions(session_db_path, bot_id)
    if db_positions:
        positions = db_positions
    if positions:
        cprint('[resume]', f'bot has {len(positions)} locally recorded open position(s)', fg='yellow')

    try:
        ex_positions = fetcher.ex.fetch_positions()
    except Exception as e:
        cprint('[positions fetch]', e, fg='red')
        ex_positions = []
    ex_list = []
    for p in (ex_positions or []):
        try:
            sym0 = p.get('symbol') if isinstance(p, dict) else None
            sym = fetcher.resolve_symbol(sym0) or sym0
            if not sym:
                continue
            qty = None
            if isinstance(p, dict):
                if p.get('contracts') is not None:
                    qty = float(p.get('contracts') or 0.0)
                elif isinstance(p.get('info', {}), dict):
                    info = p['info']
                    for k in ('positionAmt','positionAmount','position','size','contracts','available','holding'):
                        if k in info and info.get(k) not in (None, ''):
                            try:
                                qty = float(info.get(k))
                                break
                            except Exception:
                                pass
            if qty is None or abs(qty) <= 0.0:
                continue
            entry = None
            for k in ('entryPrice','entry'):
                try:
                    v = p.get(k)
                    if v:
                        entry = float(v); break
                except Exception:
                    pass
            ex_list.append({'symbol': sym, 'qty': abs(qty), 'entry': entry})
        except Exception:
            continue
    cprint('[exchange]', f'open positions: {len(ex_list)}', fg='cyan')
    for es in ex_list:
        cprint('   -', es['symbol'], f"qty={es['qty']} entry={es['entry']}", fg='cyan', dim=True)

    def _match_ex(sym, qty, entry):
        for e in ex_list:
            if e['symbol'] != sym:
                continue
            qok = (e['qty'] == qty) or (abs(e['qty'] - qty) <= max(1e-8, 0.01 * max(1.0, qty)))
            if entry is None or e['entry'] is None:
                eok = True
            else:
                eok = (e['entry'] == entry) or (abs(e['entry'] - entry) <= max(1e-8, 0.005 * max(1.0, entry)))
            if qok and eok:
                return True
        return False

    for sym, rec in list(positions.items()):
        if _match_ex(sym, float(rec.get('qty',0.0)), rec.get('entry')):
            cprint('[resume-ok]', sym, 'confirmed on exchange', fg='green', dim=True)
        else:
            cprint('[resume-miss]', sym, 'NOT found on exchange -> closing locally', fg='yellow')
            try:
                db_mark_closed(session_db_path, bot_id, rec.get('order_id'), _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat())
            except Exception:
                pass
            positions.pop(sym, None)
    save_positions(args.results_dir, positions)

    last_bar_ts = None
    cprint('[live]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s', fg='cyan')
    while True:
        now = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)
        bar_close = _align_bar_close(now, tf_sec)

        universe = sorted(set(fetcher.by_base.values()))
        for sym, rec in list(positions.items()):
            px = fetcher.fetch_ticker_price(sym)
            if px is not None:
                _report_close_cooldown(sym, rec, px)
                if _close_if_hit(fetcher, sym, rec.get('side', 'LONG'), px, rec, position_mode):
                    try:
                        db_mark_closed(session_db_path, bot_id, rec.get('order_id'), now.isoformat())
                    except Exception:
                        pass
                    positions.pop(sym, None)
                    save_positions(args.results_dir, positions)
                    continue

        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close
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
                        try:
                            cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                        except Exception:
                            pass
                    feats = feats_df.iloc[-1].to_dict()
                md[ccxt_sym] = feats
                dot()

            for sym, rec in list(positions.items()):
                row = md.get(sym)
                if row is None:
                    continue
                adj = None
                try:
                    Pos = type('Pos', (), {})
                    pos_like = Pos()
                    for k,v in rec.items():
                        setattr(pos_like, k, v)
                    adj = strat.manage_position(bar_close, sym, pos_like, row, ctx={})
                except Exception:
                    adj = None
                if getattr(adj, 'action', None) == 'EXIT':
                    px = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                    if px:
                        od = place_reduce_only(fetcher, sym, 'sell', float(rec.get('qty', 0.0)), position_mode)
                        if od:
                            cprint('[exit close]', sym, f'@~{px:.6g}', fg='yellow')
                            try:
                                db_mark_closed(session_db_path, bot_id, rec.get('order_id'), now.isoformat())
                            except Exception:
                                pass
                            positions.pop(sym, None)
                            save_positions(args.results_dir, positions)

            uni = strat.universe(bar_close, md)
            ranked = strat.rank(bar_close, md, uni)[:top_n]
            _dbg('ranked', ranked[:5], 'top_n=', top_n)
            opened = 0
            for sym in ranked:
                if sym in positions:
                    _dbg(sym, 'skip: already tracked')
                    log_skip_reason(sym, 'already open by THIS bot')
                    continue
                row = md.get(sym)
                if row is None:
                    _dbg(sym, 'skip: no md row (key mismatch)')
                    log_skip_reason(sym, 'no md row')
                    continue
                sig = strat.entry_signal(bar_close, sym, row, ctx={})
                if sig is None:
                    # Heat-based opportunistic entry (optional)
                    if open_on_heat:
                        try:
                            dist = strat.entry_distance(bar_close, sym, row, breadth=getattr(strat, '_last_breadth', 1.0))
                            heat = max(0.0, 1.0 - float(dist.get('combined_gap', 1.0)))
                        except Exception:
                            heat = 0.0
                        if heat >= open_heat_min:
                            cprint('[open on heat]', sym, f'heat={heat*100:.1f}% >= {open_heat_min*100:.1f}%', fg='yellow')
                            side_cfg = str(getattr(strat, 'cfg', {}).get('strategy_params', {}).get('side','LONG')).upper()
                            entry_px = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                            if not entry_px:
                                log_skip_reason(sym, 'no price available'); continue
                            atr_ratio = float(row.get('atr_ratio') or 0.0)
                            tp_mult = getattr(strat, 'cfg', {}).get('strategy_params', {}).get('tp_atr_mult', None)
                            sl_mult = getattr(strat, 'cfg', {}).get('strategy_params', {}).get('sl_atr_mult', None)
                            tp_price = sl_price = None
                            if tp_mult is not None and sl_mult is not None and atr_ratio>0:
                                atr_abs = max(1e-12, entry_px * atr_ratio)
                                if side_cfg == 'SHORT':
                                    tp_price = entry_px - float(tp_mult)*atr_abs
                                    sl_price = entry_px + float(sl_mult)*atr_abs
                                else:
                                    tp_price = entry_px + float(tp_mult)*atr_abs
                                    sl_price = entry_px - float(sl_mult)*atr_abs
                            if side_cfg == 'SHORT':
                                res = place_open_short(fetcher, sym, notional, entry_px, position_mode, tp_price=tp_price, sl_price=sl_price)
                            else:
                                res = place_open_long(fetcher, sym, notional, entry_px, position_mode, tp_price=tp_price, sl_price=sl_price)
                            if not res.get('ok'):
                                cprint('[open FAIL]', sym, ':', res, fg='red', file=sys.stderr); continue
                            qty = float(res['qty'])
                            ex_order_id = str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId') or '') if (res.get('order')) else None
                            side_str = 'SHORT' if side_cfg=='SHORT' else 'LONG'
                            side_str = 'LONG'
                            cprint('[open OK]', f'{sym} {side_str} qty={qty:.6g} px={entry_px}'+(f' id={ex_order_id}' if ex_order_id else ''), fg='green', bold=True)
                            rec = {'symbol': sym,'side': side_str,'qty': qty,'entry': float(entry_px),'tp_price': float(tp_price) if tp_price is not None else None,'sl_price': float(sl_price) if sl_price is not None else None,'ts_open': bar_close.isoformat(),'run_id': run_id,'order_id': str(uuid.uuid4()),'exchange_order_id': ex_order_id}
                            positions[sym] = rec; save_positions(args.results_dir, positions)
                            try: db_upsert_open_position(session_db_path, bot_id, {**rec, 'status':'OPEN', 'exchange': args.exchange, 'timeframe': tf})
                            except Exception as e: cprint('[db upsert OPEN]', e, fg='red')
                            opened += 1
                            continue
                        else:
                            _dbg(sym, 'skip: entry_signal is None')
                            log_skip_reason(sym, f'no entry_signal; heat={heat*100:.1f}% < {open_heat_min*100:.1f}%')
                            continue
                    else:
                        _dbg(sym, 'skip: entry_signal is None')
                        log_skip_reason(sym, 'no entry_signal')
                        continue
                side_attr = getattr(sig, 'side', 'LONG')
                try:
                    if isinstance(sig, bool) and sig:
                        side_attr = 'LONG'
                except Exception:
                    pass
                side_up = str(side_attr).upper()
                if side_up in ('SHORT','SELL'):
                    entry_px = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                    if not entry_px:
                        log_skip_reason(sym, 'no price available'); continue
                    tp_price = _sig_get(sig, 'tp_price', None) or _sig_get(sig, 'tp', None)
                    sl_price = _sig_get(sig, 'sl_price', None) or _sig_get(sig, 'sl', None)
                    res = place_open_short(fetcher, sym, notional, entry_px, position_mode, tp_price=tp_price, sl_price=sl_price)
                    if not res.get('ok'):
                        cprint('[open FAIL]', sym, ':', res, fg='red', file=sys.stderr); continue
                    qty = float(res['qty'])
                    ex_order_id = str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId') or '') if (res.get('order')) else None
                    cprint('[open OK]', f'{sym} SHORT qty={qty:.6g} px={entry_px}'+(f' id={ex_order_id}' if ex_order_id else ''), fg='green', bold=True)
                    rec = {'symbol': sym,'side': 'SHORT','qty': qty,'entry': float(entry_px),'tp_price': float(tp_price) if tp_price is not None else None,'sl_price': float(sl_price) if sl_price is not None else None,'ts_open': bar_close.isoformat(),'run_id': run_id,'order_id': str(uuid.uuid4()),'exchange_order_id': ex_order_id}
                    positions[sym] = rec; save_positions(args.results_dir, positions)
                    try: db_upsert_open_position(session_db_path, bot_id, {**rec, 'status':'OPEN', 'exchange': args.exchange, 'timeframe': tf})
                    except Exception as e: cprint('[db upsert OPEN]', e, fg='red')
                    opened += 1
                    continue
                elif side_up not in ('LONG','BUY','TRUE','1'):
                    _dbg(sym, 'skip: side neither LONG nor SHORT'); log_skip_reason(sym, f'entry side not LONG/SHORT (got {side_attr})'); continue

                entry_px = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                if not entry_px:
                    _dbg(sym, 'skip: no price available')
                    log_skip_reason(sym, 'no price available')
                    continue

                tp_price = _sig_get(sig, 'tp_price', None) or _sig_get(sig, 'tp', None)
                sl_price = _sig_get(sig, 'sl_price', None) or _sig_get(sig, 'sl', None)
                tp_pct = _sig_get(sig, 'tp_pct', None)
                sl_pct = _sig_get(sig, 'sl_pct', None)
                try:
                    if tp_price is None and tp_pct is not None:
                        tp_price = float(entry_px) * (1.0 + float(tp_pct))
                    if sl_price is None and sl_pct is not None:
                        sl_price = float(entry_px) * (1.0 - float(sl_pct))
                except Exception:
                    pass

                res = place_open_long(fetcher, sym, notional, entry_px, position_mode, tp_price=tp_price, sl_price=sl_price)
                if not res.get('ok'):
                    reason = res.get('skip_reason') or res.get('error') or 'unknown'
                    _dbg(sym, 'open FAIL:', reason)
                    cprint('[open FAIL]', sym, ':', res, fg='red', file=sys.stderr)
                    continue
                qty = float(res['qty'])
                ex_order_id = None
                try:
                    ex_order_id = str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId') or '')
                except Exception:
                    ex_order_id = None
                side_str = 'LONG'
                cprint('[open OK]', f'{sym} {side_str} qty={qty:.6g} px={entry_px}'
           + (f' tp={tp_price:.6g}' if tp_price is not None else ' tp=-')
           + (f' sl={sl_price:.6g}' if sl_price is not None else ' sl=-')
           + (f' id={ex_order_id}' if ex_order_id else ''), fg='green', bold=True)

                rec = {
                    'symbol': sym,
                    'side': 'LONG',
                    'qty': qty,
                    'entry': float(entry_px),
                    'tp_price': float(tp_price) if tp_price is not None else None,
                    'sl_price': float(sl_price) if sl_price is not None else None,
                    'ts_open': bar_close.isoformat(),
                    'run_id': run_id,
                    'order_id': str(uuid.uuid4()),
                    'exchange_order_id': ex_order_id
                }
                positions[sym] = rec
                save_positions(args.results_dir, positions)
                try:
                    db_upsert_open_position(session_db_path, bot_id, {**rec, 'status':'OPEN', 'exchange': args.exchange, 'timeframe': tf})
                except Exception as e:
                    cprint('[db upsert OPEN]', e, fg='red')
                opened += 1

            if args.heat_report and opened == 0:
                print_and_save_heat_from_strategy(strat, 'live', bar_close, md, uni, cache_out_path)
            cprint('[live]', f'opened={opened} at {bar_close.isoformat()}', fg='cyan', bold=(opened>0))
        else:
            dot()
        time.sleep(args.poll_sec)