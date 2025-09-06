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
 
# ANSI colors for order-close reporting
RESET = "\033[0m"
GRAY = "\033[90m"
RED = "\033[91m"
GREEN = "\033[92m"

def _fmt_float(val: Any) -> str:
    """Format float to at most two decimal places without trailing zeros."""
    try:
        return ("{:.2f}".format(float(val))).rstrip("0").rstrip(".")
    except Exception:
        return str(val)

import importlib
import os, sys, math, uuid, datetime as _dt, os
from typing import Any, Dict

CLOSE_CHECK_LOGGED = set()
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


def cleanup_stale_orders(fetcher: CCXTFetcher, positions: Dict[str, Any]):
    try:
        orders = fetcher.ex.fetch_open_orders()
    except Exception as e:
        cprint('[cleanup]', e, fg='red')
        return
    pos_syms = set(positions.keys())
    for od in orders or []:
        try:
            sym0 = od.get('symbol')
            sym = fetcher.resolve_symbol(sym0) or sym0
            if sym not in pos_syms:
                oid = od.get('id') or od.get('orderId')
                try:
                    fetcher.ex.cancel_order(oid, sym0)
                    cprint('[cleanup]', sym, fg='yellow', dim=True)
                except Exception as ce:
                    cprint('[cleanup]', sym, ce, fg='red', dim=True)
        except Exception:
            continue


def qty_for_notional(mkt: dict, notional: float, price: float, max_notional: float = None):
    if max_notional is not None and notional > max_notional:
        notional = max_notional
    min_amt = (m.get('limits', {}).get('amount', {}) or {}).get('min') if (m := mkt) else 0.0
    min_amt = float(min_amt or 0.0)
    step = (mkt.get('precision', {}) or {}).get('amount', None)
    min_notional_req = (mkt.get('limits', {}).get('cost', {}) or {}).get('min', 0.0)
    min_notional_req = float(min_notional_req or 0.0)
    qty = notional / max(price, 1e-9)
    if qty < min_amt:
        qty = float(min_amt)
    if step is not None:
        qstep = 10 ** (-step) if isinstance(step, int) else float(step)
        qty = (int(qty / qstep)) * qstep
    if price * qty < min_notional_req:
        qty = max(qty, (min_notional_req / max(price, 1e-9)))
        if step is not None:
            qstep = 10 ** (-step) if isinstance(step, int) else float(step)
            qty = (int(qty / qstep)) * qstep
    return qty, min_notional_req, step, min_amt

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

def _top_of_book(fetcher, ccxt_sym: str):
    ob = fetcher.ex.fetch_order_book(ccxt_sym, 5)
    best_bid = ob['bids'][0][0] if ob.get('bids') else None
    best_ask = ob['asks'][0][0] if ob.get('asks') else None
    return best_bid, best_ask

def _price_tick(mkt: dict):
    tick = float(mkt.get('limits', {}).get('price', {}).get('min') or 0) or 0.0
    if tick <= 0:
        prec = mkt.get('precision', {}).get('price')
        if isinstance(prec, int) and prec >= 0:
            tick = 10 ** (-prec)
    if tick <= 0:
        step = float(mkt.get('info', {}).get('priceStep') or 0.0)
        if step > 0:
            tick = step
    return tick if tick > 0 else 1e-6

def _place_limit_aggressive(fetcher, sym, side, qty, tif_ioc=True,
                            offset_ticks=1, chase_steps=0, chase_delay_ms=200,
                            chase_extra_ticks=1, params=None):
    """Ліміт 'в бік руху' з IOC і парою підгонів. Повертає (ok, order)."""
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.ex.market(ccxt_sym)
    tick = _price_tick(mkt)
    best_bid, best_ask = _top_of_book(fetcher, ccxt_sym)
    assert best_bid and best_ask, "order book empty"
    if side.lower() == 'buy':
        price = (best_ask or best_bid) + offset_ticks * tick
    else:
        price = (best_bid or best_ask) - offset_ticks * tick
    base = dict(params or {})
    if tif_ioc:
        base['timeInForce'] = 'IOC'
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'limit', side, qty, float(price), base)
        sleep_ms(120)
    except Exception as e:
        return False, {'error': str(e)}
    try:
        info = fetcher.ex.fetch_order(od.get('id'), ccxt_sym)
        filled = float(info.get('filled') or info.get('amount') or 0.0)
        amount = float(info.get('amount') or od.get('amount') or qty)
    except Exception:
        filled, amount = 0.0, float(od.get('amount') or qty)
    remain = max(amount - filled, 0.0)
    step = 0
    while remain > 0 and step < int(chase_steps):
        step += 1
        sleep_ms(int(chase_delay_ms))
        best_bid, best_ask = _top_of_book(fetcher, ccxt_sym)
        if side.lower() == 'buy':
            price = (best_ask or best_bid) + (offset_ticks + step * chase_extra_ticks) * tick
        else:
            price = (best_bid or best_ask) - (offset_ticks + step * chase_extra_ticks) * tick
        try:
            try:
                fetcher.ex.cancel_order(od.get('id'), ccxt_sym)
            except Exception:
                pass
            od = fetcher.ex.create_order(ccxt_sym, 'limit', side, remain, float(price), base)
            sleep_ms(100)
            info = fetcher.ex.fetch_order(od.get('id'), ccxt_sym)
            filled_now = float(info.get('filled') or info.get('amount') or 0.0)
            remain = max(remain - filled_now, 0.0)
        except Exception:
            break
    return True, od

def _place_tp_sl_after_open(fetcher, sym, entry_side, qty, tp_price, sl_price, position_mode):
    """Надійний fallback: окремі reduceOnly тригери (видно у Open Orders → Trigger)."""
    ccxt_sym = fetcher.resolve_symbol(sym)
    pos_oneway = str(position_mode or '').lower().startswith('one')
    base = {
        'reduceOnly': True,
        'positionSide': (
            'BOTH' if pos_oneway else ('LONG' if entry_side == 'LONG' else 'SHORT')
        ),
    }

    def try_o(typ, side, amount, price, p):
        try:
            return True, fetcher.ex.create_order(ccxt_sym, typ, side, amount, price, p)
        except Exception as e:
            return False, str(e)

    if tp_price:
        tp_side = 'sell' if entry_side == 'LONG' else 'buy'
        for cand in [
            ('take_profit', tp_side, qty, float(tp_price), dict(base)),
            (
                'take_profit_market',
                tp_side,
                None,
                {**base, 'triggerPrice': float(tp_price)},
            ),
            (
                'limit',
                tp_side,
                qty,
                float(tp_price),
                {**base, 'takeProfit': True},
            ),
        ]:
            ok, _ = try_o(*cand)
            if ok:
                break

    if sl_price:
        sl_side = 'sell' if entry_side == 'LONG' else 'buy'
        for cand in [
            (
                'stop_market',
                sl_side,
                None,
                {**base, 'triggerPrice': float(sl_price)},
            ),
            (
                'market',
                sl_side,
                None,
                {**base, 'stopLossPrice': float(sl_price)},
            ),
            ('stop', sl_side, qty, float(sl_price), dict(base)),
        ]:
            ok, _ = try_o(*cand)
            if ok:
                break

    try:
        if getattr(fetcher.ex, 'id', '') == 'bingx' and False:
            pass  # TODO: optional BingX position TP/SL endpoint
    except Exception:
        pass

def place_open_long(fetcher: CCXTFetcher, sym: str, notional: float, price: float,
                    position_mode: str, tp_price=None, sl_price=None,
                    notional_max: float = None, cfg: dict = None):
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    mkt_price = None
    try:
        ticker = fetcher.ex.fetch_ticker(ccxt_sym)
        mkt_price = ticker.get('ask') or ticker.get('last')
    except Exception:
        mkt_price = None
    mkt_price = float(mkt_price or price or 0.0)
    if mkt_price <= 0:
        return {'ok': False, 'skip_reason': 'no_price', 'qty': 0}
    qty, min_notional_req, step, min_qty = qty_for_notional(
        mkt, notional, mkt_price, max_notional=notional_max
    )
    if qty < float(min_qty) - 1e-9 or mkt_price * qty < float(min_notional_req) - 1e-9:
        return {
            'ok': False,
            'skip_reason': 'min_qty/min_notional',
            'qty': qty,
        }

    base_params = {'reduceOnly': False}
    if position_mode == 'hedge':
        base_params['positionSide'] = 'LONG'

    entry_cfg = (cfg or {}).get('entry') or {}
    entry_type = str(entry_cfg.get('type', 'market')).lower()

    if entry_type == 'limit_aggressive':
        ok, od = _place_limit_aggressive(
            fetcher,
            sym,
            'buy',
            qty,
            tif_ioc=True,
            offset_ticks=int(entry_cfg.get('offset_ticks', 1) or 1),
            chase_steps=int(entry_cfg.get('chase_steps', 0) or 0),
            chase_delay_ms=int(entry_cfg.get('chase_delay_ms', 250) or 250),
            chase_extra_ticks=int(entry_cfg.get('chase_extra_ticks', 1) or 1),
            params=base_params,
        )
        if not ok:
            return {'ok': False, 'error': f"limit_aggressive failed: {od.get('error','')}", 'qty': qty}
        res = {'ok': True, 'order': od, 'qty': qty}
    else:
        def _try(params):
            try:
                od = fetcher.ex.create_order(ccxt_sym, 'market', 'buy', qty, None, params)
                sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': params}
            except Exception as e:
                return {'ok': False, 'error': str(e), 'params': params}

        res = _try(base_params)
        if not res.get('ok'):
            msg = (res.get('error') or '').lower()
            if ('one-way mode' in msg) or ('positionside' in msg):
                p2 = {k: v for k, v in base_params.items() if k != 'positionSide'}
                res = _try(p2)
                if not res.get('ok'):
                    p3 = dict(p2, positionSide='BOTH')
                    res = _try(p3)
            elif ('min amount' in msg) and step > 0:
                try:
                    qty2 = max(min_qty, qty + step)
                    od = fetcher.ex.create_order(ccxt_sym, 'market', 'buy', qty2, None, base_params)
                    sleep_ms(RATE_MS)
                    res = {'ok': True, 'order': od, 'qty': qty2, 'params': base_params, 'retry': True}
                except Exception as e2:
                    res = {'ok': False, 'error': str(e2), 'qty': qty2, 'params': base_params}
        if not res.get('ok'):
            return res

    _place_tp_sl_after_open(fetcher, sym, 'LONG', qty, tp_price, sl_price, position_mode)
    return res

def place_open_short(fetcher: CCXTFetcher, sym: str, notional: float, price: float,
                     position_mode: str, tp_price=None, sl_price=None,
                     notional_max: float = None, cfg: dict = None):
    ccxt_sym = fetcher.resolve_symbol(sym)
    mkt = fetcher.markets.get(ccxt_sym, {})
    mkt_price = None
    try:
        ticker = fetcher.ex.fetch_ticker(ccxt_sym)
        mkt_price = ticker.get('bid') or ticker.get('last')
    except Exception:
        mkt_price = None
    mkt_price = float(mkt_price or price or 0.0)
    if mkt_price <= 0:
        return {'ok': False, 'skip_reason': 'no_price', 'qty': 0}
    qty, min_notional_req, step, min_qty = qty_for_notional(
        mkt, notional, mkt_price, max_notional=notional_max
    )
    if qty < float(min_qty) - 1e-9 or mkt_price * qty < float(min_notional_req) - 1e-9:
        return {
            'ok': False,
            'skip_reason': 'min_qty/min_notional',
            'qty': qty,
        }

    base_params = {'reduceOnly': False}
    if position_mode == 'hedge':
        base_params['positionSide'] = 'SHORT'

    entry_cfg = (cfg or {}).get('entry') or {}
    entry_type = str(entry_cfg.get('type', 'market')).lower()

    if entry_type == 'limit_aggressive':
        ok, od = _place_limit_aggressive(
            fetcher,
            sym,
            'sell',
            qty,
            tif_ioc=True,
            offset_ticks=int(entry_cfg.get('offset_ticks', 1) or 1),
            chase_steps=int(entry_cfg.get('chase_steps', 0) or 0),
            chase_delay_ms=int(entry_cfg.get('chase_delay_ms', 250) or 250),
            chase_extra_ticks=int(entry_cfg.get('chase_extra_ticks', 1) or 1),
            params=base_params,
        )
        if not ok:
            return {'ok': False, 'error': f"limit_aggressive failed: {od.get('error','')}", 'qty': qty}
        res = {'ok': True, 'order': od, 'qty': qty}
    else:
        def _try(params):
            try:
                od = fetcher.ex.create_order(ccxt_sym, 'market', 'sell', qty, None, params)
                sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': params}
            except Exception as e:
                return {'ok': False, 'error': str(e), 'params': params}

        res = _try(base_params)
        if not res.get('ok'):
            msg = (res.get('error') or '').lower()
            if ('one-way mode' in msg) or ('positionside' in msg):
                p2 = {k: v for k, v in base_params.items() if k != 'positionSide'}
                res = _try(p2)
                if not res.get('ok'):
                    p3 = dict(p2, positionSide='BOTH')
                    res = _try(p3)
            elif ('min amount' in msg) and step > 0:
                try:
                    qty2 = max(min_qty, qty + step)
                    od = fetcher.ex.create_order(ccxt_sym, 'market', 'sell', qty2, None, base_params)
                    sleep_ms(RATE_MS)
                    res = {'ok': True, 'order': od, 'qty': qty2, 'params': base_params, 'retry': True}
                except Exception as e2:
                    res = {'ok': False, 'error': str(e2), 'qty': qty2, 'params': base_params}
        if not res.get('ok'):
            return res

    _place_tp_sl_after_open(fetcher, sym, 'SHORT', qty, tp_price, sl_price, position_mode)
    return res

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
    if sym in CLOSE_CHECK_LOGGED:
        return
    CLOSE_CHECK_LOGGED.add(sym)

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
        pos_rec['_last_close_report_ts'] = getattr(bar_close, 'isoformat', lambda: bar_close)()
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
    qty = float(pos_rec.get('qty', 0.0))
    if side == 'LONG':
        if tp is not None and px >= tp:
            od = place_reduce_only(fetcher, sym, 'sell', qty, position_mode)
            if od:
                pnl = (px - float(pos_rec.get('entry', 0.0))) * qty
                color = GREEN if pnl >= 0 else RED
                print(f"{GRAY}[close] {sym} {side} qty={_fmt_float(qty)} exit={_fmt_float(px)} reason=TP "
                      f"pnl={color}{pnl:+.2f}{GRAY}{RESET}")
                return True
        if sl is not None and px <= sl:
            od = place_reduce_only(fetcher, sym, 'sell', qty, position_mode)
            if od:
                pnl = (px - float(pos_rec.get('entry', 0.0))) * qty
                color = GREEN if pnl >= 0 else RED
                print(f"{GRAY}[close] {sym} {side} qty={_fmt_float(qty)} exit={_fmt_float(px)} reason=SL "
                      f"pnl={color}{pnl:+.2f}{GRAY}{RESET}")
                return True
    elif side == 'SHORT':
        if tp is not None and px <= tp:
            od = place_reduce_only(fetcher, sym, 'buy', qty, position_mode)
            if od:
                pnl = (float(pos_rec.get('entry', 0.0)) - px) * qty
                color = GREEN if pnl >= 0 else RED
                print(f"{GRAY}[close] {sym} {side} qty={_fmt_float(qty)} exit={_fmt_float(px)} reason=TP "
                      f"pnl={color}{pnl:+.2f}{GRAY}{RESET}")
                return True
        if sl is not None and px >= sl:
            od = place_reduce_only(fetcher, sym, 'buy', qty, position_mode)
            if od:
                pnl = (float(pos_rec.get('entry', 0.0)) - px) * qty
                color = GREEN if pnl >= 0 else RED
                print(f"{GRAY}[close] {sym} {side} qty={_fmt_float(qty)} exit={_fmt_float(px)} reason=SL "
                      f"pnl={color}{pnl:+.2f}{GRAY}{RESET}")
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

    # Load allow-list from --universe-file if provided
    allowed_universe = None
    try:
        uf = getattr(args, 'universe_file', '') or ''
        if uf:
            with open(uf, 'r', encoding='utf-8') as fh:
                allowed_universe = set([ln.strip() for ln in fh.read().splitlines() if ln.strip() and not ln.strip().startswith('#')])
            cprint('[universe]', f'allow list size = {len(allowed_universe)}', fg='cyan')
    except Exception as _e:
        cprint('[universe]', f'failed to read universe file: {uf} -> {_e}', fg='red')
        allowed_universe = None

    top_n_v, top_n_origin = _cfg_pick(cfg, ['top_n','runner.top_n','live.top_n','strategy_params.top_n','strategy.top_n'], 4)
    notional_v, notional_origin = _cfg_pick(cfg, ['notional','position_notional','runner.notional','live.notional','portfolio.position_notional'], 2.2)
    notional_max_v, notional_max_origin = _cfg_pick(cfg, ['position_notional_max','runner.position_notional_max','live.position_notional_max','portfolio.position_notional_max'], None)
    position_mode_v, position_mode_origin = _cfg_pick(cfg, ['position_mode','runner.position_mode','live.position_mode','session.position_mode'], 'hedge')
    tf_v, tf_origin = _cfg_pick(cfg, ['timeframe','runner.timeframe','live.timeframe'], '1h')
    top_n = int(top_n_v)
    notional = float(notional_v)
    position_notional_max = float(notional_max_v) if notional_max_v is not None else None
    position_mode = str(position_mode_v)
    tf = str(tf_v)
    tf_sec = _tf_to_seconds(tf)

    # Optional: open entries based on heat (even if entry_signal is None)
    open_on_heat_v, open_on_heat_origin = _cfg_pick(cfg, ['open_on_heat','runner.open_on_heat','live.open_on_heat'], False)
    open_heat_min_v, open_heat_min_origin = _cfg_pick(cfg, ['open_heat_min','runner.open_heat_min','live.open_heat_min'], 0.80)
    open_on_heat = bool(open_on_heat_v)
    open_heat_min = float(open_heat_min_v)
    cprint('[cfg]', f'top_n={top_n}, notional={notional}, notional_max={position_notional_max}, timeframe={tf}, position_mode={position_mode}, open_on_heat={open_on_heat}, heat_min={open_heat_min}', fg='magenta')
    if getattr(args, 'debug', False):
        _debug_dump_effective(cfg, strat, args,
            resolved={
                'top_n': (top_n, top_n_origin),
                'notional': (notional, notional_origin),
                'position_notional_max': (position_notional_max, notional_max_origin),
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
        cprint(
            '   -',
            es['symbol'],
            f"qty={_fmt_float(es['qty'])} entry={_fmt_float(es['entry'])}",
            fg='cyan',
            dim=True,
        )

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
            CLOSE_CHECK_LOGGED.discard(sym)
    cleanup_stale_orders(fetcher, positions)
    save_positions(args.results_dir, positions)

    last_bar_ts = None
    cprint('[live]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s', fg='cyan')
    while True:
        now = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)
        bar_close = _align_bar_close(now, tf_sec)

        all_syms = sorted(set(fetcher.by_base.values()))
        universe = [sym for sym in all_syms if (allowed_universe is None or sym in allowed_universe)]
        for sym, rec in list(positions.items()):
            px = fetcher.fetch_ticker_price(sym)
            if px is not None:
                last_rep = rec.get('_last_close_report_ts')
                bc_iso = bar_close.isoformat()
                if last_rep != bc_iso:
                    _report_close_cooldown(sym, rec, px, bar_close)
                if _close_if_hit(fetcher, sym, rec.get('side', 'LONG'), px, rec, position_mode):
                    try:
                        db_mark_closed(session_db_path, bot_id, rec.get('order_id'), now.isoformat())
                    except Exception:
                        pass
                    positions.pop(sym, None)
                    CLOSE_CHECK_LOGGED.discard(sym)
                    cleanup_stale_orders(fetcher, positions)
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
                        side = str(rec.get('side', 'LONG')).upper()
                        close_side = 'sell' if side == 'LONG' else 'buy'
                        od = place_reduce_only(fetcher, sym, close_side, float(rec.get('qty', 0.0)), position_mode)
                        if od:
                            qty = float(rec.get('qty', 0.0))
                            entry = float(rec.get('entry', 0.0))
                            pnl = (px - entry) * qty if side == 'LONG' else (entry - px) * qty
                            color = GREEN if pnl >= 0 else RED
                            print(
                                f"{GRAY}[close] {sym} {side} qty={_fmt_float(qty)} exit={_fmt_float(px)} "
                                f"reason={getattr(adj, 'reason', 'EXIT')} pnl={color}{pnl:+.2f}{GRAY}{RESET}"
                            )
                            try:
                                db_mark_closed(session_db_path, bot_id, rec.get('order_id'), now.isoformat())
                            except Exception:
                                pass
                            positions.pop(sym, None)
                            CLOSE_CHECK_LOGGED.discard(sym)
                            cleanup_stale_orders(fetcher, positions)
                            save_positions(args.results_dir, positions)

            uni = strat.universe(bar_close, md)


            if 'allowed_universe' in locals() and allowed_universe is not None:


                uni = [sym for sym in uni if sym in allowed_universe]
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
                                res = place_open_short(fetcher, sym, notional, entry_px, position_mode,
                                                       tp_price=tp_price, sl_price=sl_price,
                                                       notional_max=position_notional_max, cfg=cfg)
                            else:
                                res = place_open_long(fetcher, sym, notional, entry_px, position_mode,
                                                      tp_price=tp_price, sl_price=sl_price,
                                                      notional_max=position_notional_max, cfg=cfg)
                            if not res.get('ok'):
                                reason = res.get('skip_reason') or res.get('error') or 'unknown'
                                if res.get('skip_reason'):
                                    log_skip_reason(sym, reason)
                                else:
                                    cprint('[open FAIL]', sym, ':', res, fg='red', file=sys.stderr)
                                continue
                            qty = float(res['qty'])
                            ex_order_id = str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId') or '') if (res.get('order')) else None
                            side_str = 'SHORT' if side_cfg=='SHORT' else 'LONG'
                            side_str = 'LONG'
                            cprint(
                                '[open OK]',
                                f'{sym} {side_str} qty={_fmt_float(qty)} px={_fmt_float(entry_px)}'
                                + (f' id={ex_order_id}' if ex_order_id else ''),
                                fg='green',
                                bold=True,
                            )
                            rec = {'symbol': sym,'side': side_str,'qty': qty,'entry': float(entry_px),'tp_price': float(tp_price) if tp_price is not None else None,'sl_price': float(sl_price) if sl_price is not None else None,'ts_open': bar_close.isoformat(),'run_id': run_id,'order_id': str(uuid.uuid4()),'exchange_order_id': ex_order_id}
                            positions[sym] = rec; save_positions(args.results_dir, positions)
                            try: db_upsert_open_position(session_db_path, bot_id, {**rec, 'status':'OPEN', 'exchange': args.exchange, 'timeframe': tf})
                            except Exception as e: cprint('[db upsert OPEN]', e, fg='red')
                            cleanup_stale_orders(fetcher, positions)
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
                    res = place_open_short(fetcher, sym, notional, entry_px, position_mode,
                                           tp_price=tp_price, sl_price=sl_price,
                                           notional_max=position_notional_max, cfg=cfg)
                    if not res.get('ok'):
                        reason = res.get('skip_reason') or res.get('error') or 'unknown'
                        if res.get('skip_reason'):
                            log_skip_reason(sym, reason)
                        else:
                            cprint('[open FAIL]', sym, ':', res, fg='red', file=sys.stderr)
                        continue
                    qty = float(res['qty'])
                    ex_order_id = str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId') or '') if (res.get('order')) else None
                    cprint(
                        '[open OK]',
                        f'{sym} SHORT qty={_fmt_float(qty)} px={_fmt_float(entry_px)}'
                        + (f' id={ex_order_id}' if ex_order_id else ''),
                        fg='green',
                        bold=True,
                    )
                    rec = {'symbol': sym,'side': 'SHORT','qty': qty,'entry': float(entry_px),'tp_price': float(tp_price) if tp_price is not None else None,'sl_price': float(sl_price) if sl_price is not None else None,'ts_open': bar_close.isoformat(),'run_id': run_id,'order_id': str(uuid.uuid4()),'exchange_order_id': ex_order_id}
                    positions[sym] = rec; save_positions(args.results_dir, positions)
                    try: db_upsert_open_position(session_db_path, bot_id, {**rec, 'status':'OPEN', 'exchange': args.exchange, 'timeframe': tf})
                    except Exception as e: cprint('[db upsert OPEN]', e, fg='red')
                    cleanup_stale_orders(fetcher, positions)
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

                res = place_open_long(fetcher, sym, notional, entry_px, position_mode,
                                      tp_price=tp_price, sl_price=sl_price,
                                      notional_max=position_notional_max, cfg=cfg)
                if not res.get('ok'):
                    reason = res.get('skip_reason') or res.get('error') or 'unknown'
                    _dbg(sym, 'open FAIL:', reason)
                    if res.get('skip_reason'):
                        log_skip_reason(sym, reason)
                    else:
                        cprint('[open FAIL]', sym, ':', res, fg='red', file=sys.stderr)
                    continue
                qty = float(res['qty'])
                ex_order_id = None
                try:
                    ex_order_id = str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId') or '')
                except Exception:
                    ex_order_id = None
                side_str = 'LONG'
                cprint(
                    '[open OK]',
                    f'{sym} {side_str} qty={_fmt_float(qty)} px={_fmt_float(entry_px)}'
                    + (f' tp={_fmt_float(tp_price)}' if tp_price is not None else ' tp=-')
                    + (f' sl={_fmt_float(sl_price)}' if sl_price is not None else ' sl=-')
                    + (f' id={ex_order_id}' if ex_order_id else ''),
                    fg='green',
                    bold=True,
                )

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
                cleanup_stale_orders(fetcher, positions)
                opened += 1
            # Pretty-print currently open positions in YELLOW (diagnostics)
            try:
                if positions:
                    for _sym, _rec in positions.items():
                        _side = str(_rec.get('side', '?')).upper()
                        _qty = _rec.get('qty', None)
                        _entry = _rec.get('entry', None)
                        _tp = _rec.get('tp_price', None)
                        _sl = _rec.get('sl_price', None)
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

            if args.heat_report and len(positions) == 0:
                try:
                    print_and_save_heat_from_strategy(strat, 'live', bar_close, md, uni, cache_out_path)
                except Exception:
                    pass

            cprint('[live]', f'opened={opened} at {bar_close.isoformat()}', fg='cyan', bold=(opened>0))
        else:
            dot()
        time.sleep(args.poll_sec)