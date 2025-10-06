# live_runner.py — LIVE mode
from .common import *
from .common import (
    _tf_to_seconds,
    _align_bar_close,
    load_positions,
    save_positions,
    print_and_save_heat_from_strategy,
    make_bot_id,
    db_load_open_positions,
    db_upsert_open_position,
    db_mark_closed,
    write_equity,
    insert_order_row,
)

# color print fallback
try:
    from .common import cprint as _cprint
except Exception:
    _cprint = None


def cprint(*parts, fg: str = "", bold: bool = False, dim: bool = False, file=None, end="\n", flush=False):
    if _cprint:
        return _cprint(*parts, fg=fg, bold=bold, dim=dim, file=file, end=end, flush=flush)
    print(" ".join(str(p) for p in parts), file=file, end=end, flush=flush)


def _normalize_close_reason(value, fallback: str = "") -> str:
    if value is None:
        return fallback
    try:
        text = str(value)
    except Exception:
        text = f"{value}"
    text = text.strip()
    return fallback if not text or text.lower() in {"", "nan", "none", "null", "nat"} else text


import importlib
import inspect
import os, sys, math, uuid, datetime as _dt
import json
import time as _time
import logging
import asyncio
from datetime import datetime, timezone
from typing import Optional
from types import SimpleNamespace


log = logging.getLogger(__name__)


# SL/BE migration tuning
BE_CANCEL_WAIT_SEC = 3.0
BE_POST_TP_DELAY_SEC = 2.0
BE_MAX_RETRIES = 5
BE_RETRY_BACKOFF = (0.6, 1.0, 1.5, 2.0, 2.5)
DUST_CLOSE_THRESHOLD_QTY = 1.05
EPS_QTY = 1e-12

FORCE_CLOSE_TIF = os.getenv("FORCE_CLOSE_TIF", "IOC")
FORCE_CLOSE_RETRIES = int(os.getenv("FORCE_CLOSE_RETRIES", "2"))
FORCE_CLOSE_SLIPPAGE_BPS = float(os.getenv("FORCE_CLOSE_SLIPPAGE_BPS", "5"))


def _pos_adapter(rec: dict):
    return SimpleNamespace(
        side=str(rec['side']).upper(),
        entry=float(rec['entry']),
        tp=float(rec.get('tp_price') or rec.get('tp') or 0.0),
        sl=float(rec.get('sl_price') or rec.get('sl') or 0.0),
        qty=float(rec['qty'])
    )


def _calc_tp50_and_be(rec):
    try:
        entry = float(rec.get('entry_fill') or rec.get('entry') or 0.0)
    except Exception:
        entry = 0.0
    try:
        tp = float(rec.get('tp_price') or rec.get('tp') or 0.0)
    except Exception:
        tp = 0.0
    side_long = str(rec['side']).upper() == 'LONG'
    if not tp or entry <= 0:
        return None, entry
    if side_long:
        prog = abs(tp - entry) * 0.5
        tp50 = entry + prog
        be = entry
    else:
        prog = abs(entry - tp) * 0.5
        tp50 = entry - prog
        be = entry
    return float(tp50), float(be)


async def _call_maybe_async(func, *args, **kwargs):
    """Call a possibly-async CCXT method and await the result when needed."""
    result = func(*args, **kwargs)
    if inspect.isawaitable(result):
        return await result
    return result


def _floor_step(x, step):
    if step <= 0:
        return float(x)
    return math.floor((float(x) + EPS_QTY) / step) * step


def _ceil_step(x, step):
    if step <= 0:
        return float(x)
    return math.ceil((float(x) - EPS_QTY) / step) * step


def allocate_tp_ladder(total_qty, fractions, lot_step):
    if not fractions:
        return []
    lot_step = float(lot_step or 0.0)
    qtys = []
    acc = 0.0
    for frac in fractions[:-1]:
        part = _floor_step(total_qty * float(frac), lot_step)
        qtys.append(part)
        acc += part
    last = _floor_step(max(0.0, total_qty - acc), lot_step)
    qtys.append(last)
    return qtys


def _market_steps(fetcher, symbol):
    markets = getattr(fetcher, "markets", {}) or {}
    mkt = markets.get(symbol, {}) or {}
    info = mkt.get("info") if isinstance(mkt.get("info"), dict) else {}
    precision = mkt.get("precision", {}) or {}
    limits = mkt.get("limits", {}) or {}
    price_limits = limits.get("price", {}) or {}
    amount_limits = limits.get("amount", {}) or {}

    tick_size = 0.0
    price_prec = precision.get("price")
    if price_prec is not None:
        try:
            tick_size = 10 ** (-int(price_prec))
        except Exception:
            try:
                tick_size = float(price_prec)
            except Exception:
                tick_size = 0.0
    if not tick_size:
        tick_size = float(price_limits.get("min") or 0.0)
    if not tick_size:
        tick_size = float(info.get("tickSize") or info.get("minPrice") or 0.0)

    lot_step = 0.0
    amount_prec = precision.get("amount")
    if amount_prec is not None:
        try:
            lot_step = 10 ** (-int(amount_prec))
        except Exception:
            try:
                lot_step = float(amount_prec)
            except Exception:
                lot_step = 0.0
    if not lot_step:
        lot_step = float(amount_limits.get("step") or 0.0)
    if not lot_step:
        lot_step = float(info.get("stepSize") or 0.0)

    min_qty = float(amount_limits.get("min") or 0.0)
    if not min_qty:
        min_qty = float(info.get("minQty") or 0.0)

    return tick_size, lot_step, min_qty


def _ensure_be_fields(rec):
    if 'entry_qty' not in rec:
        try:
            rec['entry_qty'] = float(rec.get('qty') or 0.0)
        except Exception:
            rec['entry_qty'] = 0.0
    rec.setdefault('be_plan_id', None)
    rec.setdefault('be_plan_active', False)
    rec.setdefault('sl_be_done', False)
    rec.setdefault('be_plan_activated_ts', None)
    rec.setdefault('be_plan_last_fallback_ts', None)
    rec.setdefault('be_plan', None)
    rec.setdefault('be_moved_once', False)
    rec.setdefault('be_last_error', None)
    rec.setdefault('tp_hits', 0)
    tp50, _ = _calc_tp50_and_be(rec)
    rec['tp50_trigger'] = tp50
    try:
        entry_val = float(rec.get('entry') or rec.get('entry_fill') or 0.0)
    except Exception:
        entry_val = 0.0
    if rec.get('be_high_price') is None and entry_val:
        rec['be_high_price'] = entry_val
    if rec.get('be_low_price') is None and entry_val:
        rec['be_low_price'] = entry_val
    rec.setdefault('be_high_price', None)
    rec.setdefault('be_low_price', None)
    rec.setdefault('be_extra_sl_done', False)
    rec.setdefault('be_extra_sl_order_id', None)
    rec.setdefault('be_extra_sl_last_attempt_ts', None)

_last_dot_bar = None
def _bar_key(now: datetime, bar_sec: int) -> int:
    # у нас однакова для всіх символів сітка барів
    return int(now.timestamp() // bar_sec)

def print_dot_once_per_bar(now: datetime, bar_sec: int):
    global _last_dot_bar
    k = _bar_key(now, bar_sec)
    if _last_dot_bar != k:
        sys.stdout.write("."); sys.stdout.flush()
        _last_dot_bar = k

_last_cc_print = {}
def cc_log_once_per_bar(sym, bar_key, msg):
    k = (sym, bar_key)
    if _last_cc_print.get(k):
        return
    _last_cc_print[k] = True
    cprint(msg, fg="magenta", dim=True)

def _format_float_short(val):
    try:
        f = float(val)
    except Exception:
        return str(val)
    try:
        if not math.isfinite(f):
            return str(f)
    except Exception:
        return str(f)
    if abs(f) >= 1000 or (0 < abs(f) < 1e-4):
        return f"{f:.3e}"
    return f"{f:.4f}"

def _format_dict_short(data):
    if not isinstance(data, dict) or not data:
        return "{}" if not data else str(data)
    parts = []
    for key in sorted(data.keys()):
        parts.append(f"{key}:{_format_float_short(data[key])}")
    return "{" + ", ".join(parts) + "}"


def _safe_float(val):
    try:
        if val is None:
            return None
        f = float(val)
        if math.isfinite(f):
            return f
    except Exception:
        return None
    return f

def _call_entry_distance_safe(strat, t, sym, row):
    fn = getattr(strat, 'entry_distance', None)
    if not callable(fn):
        return None
    try:
        return fn(t, sym, row, breadth=getattr(strat, '_last_breadth', 1.0))
    except TypeError:
        try:
            return fn(t, sym, row)
        except Exception:
            return None
    except Exception:
        return None

def _call_best_entry_distance_safe(strat, t, md_slice, symbols=None):
    fn = getattr(strat, 'best_entry_distance', None)
    if not callable(fn):
        return None
    try:
        if symbols is None:
            return fn(t, md_slice)
        return fn(t, md_slice, symbols=symbols)
    except TypeError:
        try:
            if symbols is None:
                return fn(t, md_slice)
            return fn(t, md_slice, symbols)
        except Exception:
            return None
    except Exception:
        return None

def _log_heat_best(label, dist):
    if not isinstance(dist, dict) or not dist:
        return
    sym = dist.get('symbol') or dist.get('sym') or '??'
    gap = dist.get('combined_gap')
    heat = None
    try:
        if gap is not None:
            heat = max(0.0, min(1.0, 1.0 - float(gap)))
    except Exception:
        heat = None
    reason = dist.get('reason') or '-'
    parts = [
        f"[heat.best {label}]",
        str(sym),
    ]
    if gap is not None:
        parts.append(f"gap={_format_float_short(gap)}")
    if heat is not None:
        parts.append(f"heat={_format_float_short(heat)}")
    if reason:
        parts.append(f"reason={reason}")
    parts.append(f"gaps={_format_dict_short(dist.get('gaps') or {})}")
    parts.append(f"actuals={_format_dict_short(dist.get('actuals') or {})}")
    parts.append(f"thresholds={_format_dict_short(dist.get('thresholds') or {})}")
    cprint(*parts, fg='cyan', dim=True)

def _log_heat_distances(label, strat, t, md, symbols, limit, uni_set=None):
    if not symbols:
        cprint(f"[heat.dist {label}]", "no symbols", fg='yellow', dim=True)
        return
    try:
        limit = int(limit)
    except Exception:
        limit = 0
    if limit <= 0:
        limit = len(symbols)
    count = 0
    for sym in symbols:
        if count >= limit:
            break
        row = md.get(sym)
        if row is None:
            continue
        dist = _call_entry_distance_safe(strat, t, sym, row)
        if not isinstance(dist, dict):
            continue
        parts = [f"[heat.dist {label}]", str(sym)]
        if uni_set is not None and label == 'pre':
            parts.append(f"in_uni={'Y' if sym in uni_set else 'N'}")
        gap = dist.get('combined_gap')
        if gap is not None:
            parts.append(f"gap={_format_float_short(gap)}")
        reason = dist.get('reason')
        if reason:
            parts.append(f"reason={reason}")
        parts.append(f"gaps={_format_dict_short(dist.get('gaps') or {})}")
        parts.append(f"actuals={_format_dict_short(dist.get('actuals') or {})}")
        parts.append(f"thresholds={_format_dict_short(dist.get('thresholds') or {})}")
        cprint(*parts, fg=('blue' if label == 'pre' else 'green'), dim=True)
        count += 1
    if count == 0:
        cprint(f"[heat.dist {label}]", "no distances", fg='yellow', dim=True)


def _log_heat_debug_snapshot(
    strat,
    bar_close,
    md,
    md_symbols,
    pre_rank_syms,
    universe_syms,
    ranked_syms,
    top_n,
    *,
    log_pre: bool = False,):
    try:
        debug_limit = int(top_n)
    except Exception:
        try:
            debug_limit = int(float(top_n))
        except Exception:
            debug_limit = 0
    if debug_limit <= 0:
        debug_limit = 10
    pre_syms = list(pre_rank_syms or [])
    if not pre_syms:
        pre_syms = list(md_symbols or [])
    uni_set = set(universe_syms or []) if log_pre else None
    cprint(
        '[heat.debug]',
        f'limit={debug_limit}',
        f'pre_candidates={len(pre_syms)}',
        f'post_candidates={len(ranked_syms or [])}',
        fg='cyan',
        dim=True,
    )
    if log_pre:
        _log_heat_distances('pre', strat, bar_close, md, pre_syms, debug_limit, uni_set=uni_set)
    _log_heat_distances('post', strat, bar_close, md, ranked_syms or [], debug_limit)
    best_all = _call_best_entry_distance_safe(strat, bar_close, md, symbols=md_symbols)
    if best_all:
        _log_heat_best('pre', best_all)
    if universe_syms:
        best_uni = _call_best_entry_distance_safe(strat, bar_close, md, symbols=universe_syms)
        if best_uni:
            _log_heat_best('post', best_uni)

def mark_closed_now(fetcher, session_db_path, bot_id, sym, order_id, px_hint=None, mark_hint=None, reason=None):
    ts = datetime.now(timezone.utc).isoformat()
    px = px_hint or fetcher.fetch_ticker_price(sym)
    mark_px = mark_hint
    if mark_px is None:
        try:
            mark_px = fetcher.fetch_mark_price(sym)
        except Exception:
            mark_px = None
    reason_text = _normalize_close_reason(reason, fallback="market_exit")
    try:
        db_mark_closed(session_db_path, bot_id, order_id, ts,
                       exit_fill=px, exit_fill_ts=ts, exit_mark_price=mark_px,
                       close_reason=reason_text)
    except Exception as e:
        cprint('db_mark_closed_failed', str(e), fg='yellow')

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


def _tf_to_sec(tf: str) -> int:
    """Alias for _tf_to_seconds for clarity."""
    return _tf_to_seconds(tf)


def _infer_prewarm_bars(cfg: dict, timeframe: str) -> int:
    tf_sec = _tf_to_sec(timeframe)
    sp = {
        **(cfg.get("strategy_params") or {}),
        **((cfg.get("strategy") or {}).get("params") or {}),
    }
    atr_n = int(sp.get("atr_n", 50))
    adx_n = int(sp.get("adx_n", 14))
    heat_n = int(sp.get("heat_lookback", 50))
    bars_1h = max(1, 3600 // tf_sec)
    bars_24h = max(1, 24 * 3600 // tf_sec)
    k = 3
    need = max(k * atr_n, k * adx_n, k * heat_n, bars_24h)
    return int(need + 10)

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
            mark_price = _extract_mark_price_from_dict(p)
            for k in ('entryPrice','entry'):
                try:
                    v = p.get(k)
                    if v:
                        entry = float(v)
                        break
                except Exception:
                    pass
            if mark_price is None and isinstance(p.get('info'), dict):
                mark_price = _extract_mark_price_from_dict(p['info'])
            pos_map[sym] = {
                'qty': abs(qty),
                'side': 'LONG' if side.startswith('LONG') else 'SHORT',
                'entry': entry,
                'mark_price': mark_price,
            }
        except Exception:
            continue
    return pos_map

def get_account_equity(fetcher: CCXTFetcher) -> float:
    try:
        bal = fetcher.ex.fetch_balance()
    except Exception as e:
        cprint('[balance fetch]', e, fg='red')
        return 0.0
    def _as_float(v):
        try:
            if v is not None and v != "":
                return float(v)
        except Exception:
            pass
        return None

    def _pick(d: dict, paths: tuple):
        for path in paths:
            cur = d
            ok = True
            for key in path:
                if isinstance(cur, dict) and key in cur:
                    cur = cur[key]
                else:
                    ok = False
                    break
            if ok:
                fv = _as_float(cur)
                if fv is not None:
                    return fv
        return None

    try:
        if isinstance(bal, dict):
            # explicit stablecoins preferred
            currency_paths = [
                ('total', cur) for cur in ('USDT', 'USD', 'USDC', 'BUSD')
            ] + [
                (cur, fld) for cur in ('USDT', 'USD', 'USDC', 'BUSD')
                for fld in ('equity', 'total', 'free', 'balance', 'walletBalance', 'availableBalance', 'cashBal')
            ]
            v = _pick(bal, currency_paths)
            if v is not None:
                return v

            v = _pick(bal, [('equity',), ('total',), ('balance',), ('walletBalance',), ('availableBalance',), ('cashBal',)])
            if v is not None:
                return v

            info = bal.get('info')
            if isinstance(info, dict):
                v = _pick(
                    info,
                    [('equity',), ('total',), ('balance',), ('walletBalance',), ('availableBalance',), ('cashBal',)]
                    + [
                        (cur, fld)
                        for cur in ('USDT', 'USD', 'USDC', 'BUSD')
                        for fld in (
                            'equity', 'total', 'balance', 'walletBalance', 'availableBalance', 'cashBal'
                        )
                    ],
                )
                if v is not None:
                    return v

                if isinstance(info.get('balances'), list):
                    for entry in info['balances']:
                        if not isinstance(entry, dict):
                            continue
                        asset = str(
                            entry.get('asset')
                            or entry.get('currency')
                            or entry.get('coin')
                            or ''
                        ).upper()
                        if asset in ('USDT', 'USD', 'USDC', 'BUSD'):
                            for fld in (
                                'equity', 'total', 'balance', 'walletBalance', 'availableBalance', 'cashBal'
                            ):
                                fv = _as_float(entry.get(fld))
                                if fv is not None:
                                    return fv
    except Exception:
        pass
    return 0.0

def qty_for_notional(mkt: dict, notional: float, price: float):
    min_qty = float(mkt.get('limits', {}).get('amount', {}).get('min') or 0.0)
    step = float(mkt.get('precision', {}).get('amount') or 0.0)
    min_notional_req = float(mkt.get('limits', {}).get('cost', {}).get('min') or 0.0)
    if step and step > 0:
        qty = max(min_qty, math.floor(notional / max(price, 1e-9) / step) * step)
    else:
        qty = max(min_qty, notional / max(price, 1e-9))
    return qty, min_notional_req, step, min_qty


def round_to_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return float(value)
    return math.floor(float(value) / step) * step


def _round_to_step(value: float, step: float) -> float:
    if not step or step <= 0:
        return float(value)
    return math.floor((float(value) + EPS_QTY) / float(step)) * float(step)


def _position_qty(pos: Optional[dict], side: str) -> float:
    if not isinstance(pos, dict):
        return 0.0
    raw = pos.get('contracts')
    if raw in (None, ''):
        for key in (
            'positionAmt',
            'positionAmount',
            'position',
            'size',
            'available',
            'holding',
            'amount',
        ):
            val = pos.get(key)
            if val not in (None, ''):
                raw = val
                break
    if raw in (None, '') and isinstance(pos.get('info'), dict):
        info = pos['info']
        for key in (
            'positionAmt',
            'positionAmount',
            'position',
            'size',
            'contracts',
            'available',
            'holding',
            'amount',
        ):
            val = info.get(key)
            if val not in (None, ''):
                raw = val
                break
    try:
        qty = abs(float(raw)) if raw not in (None, '') else 0.0
    except Exception:
        qty = 0.0
    return qty


def _cancel_position_stops(fetcher, sym, rec) -> list:
    canceled = []
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    try:
        open_orders = fetcher.ex.fetch_open_orders(ccxt_sym)
    except Exception as exc:
        log.warning(f"[sl->BE] fetch_open_orders before cancel failed: {exc}")
        open_orders = []
    for od in open_orders or []:
        info = od.get('info') or {}
        reduce_only = bool(info.get('reduceOnly') or od.get('reduceOnly'))
        if not reduce_only:
            continue
        otype = str(od.get('type') or info.get('type') or '').lower()
        if 'stop' not in otype:
            continue
        oid = str(od.get('id') or od.get('orderId') or od.get('clientOrderId') or '')
        if not oid:
            continue
        try:
            fetcher.ex.cancel_order(oid, ccxt_sym, params={'positionSide': 'BOTH'})
            canceled.append(oid)
        except Exception as exc:
            log.warning(f"[sl->BE] cancel stop {oid} failed: {exc}")
    if canceled:
        log.info(f"[sl->BE wait] {sym} canceled={len(canceled)}")
    return canceled


def _place_be_plan_on_exchange(fetcher, sym, rec, position_mode, be_plan,
                               max_retries=6, retry_sleep=1.0, post_cancel_wait=0.8):
    """
    Place (or re-place) the BE stop for a position after TP1 hit.
    - Cancels previous SL(s)
    - Waits a bit so reserved qty frees up on exchange
    - Places a new stop market with trigger=be_plan['trigger']
    - Quantity logic:
        * use current position qty (side-aware) minus any still-reserved qty
        * round with exchange stepSize/lot restrictions
    - Robust fallbacks for:
        * 'The order size must be less than the available amount of 0 USDT'
        * 'Stop Loss price should be greater/less than the current price'
    Returns True on success, False on permanent failure.
    """
    if not be_plan or 'trigger' not in be_plan:
        rec['be_last_error'] = 'missing be_plan trigger'
        return False

    side = rec.get('side', 'LONG')
    ex = fetcher.ex
    market = ex.market(sym) if hasattr(ex, 'market') else (fetcher.markets or {}).get(sym, {})

    _cancel_position_stops(fetcher, sym, rec)

    if post_cancel_wait and post_cancel_wait > 0:
        _time.sleep(post_cancel_wait)

    trigger = float(be_plan['trigger'])
    params = {"reduceOnly": True, "positionSide": "BOTH", "workingType": "MARK_PRICE"}
    qty_step = None
    if isinstance(market, dict):
        qty_step = (
            market.get('amount', {}).get('step')
            if isinstance(market.get('amount'), dict)
            else None
        )
        if not qty_step:
            qty_step = market.get('precision', {}).get('amount') if isinstance(market.get('precision'), dict) else None
        if not qty_step:
            qty_step = market.get('info', {}).get('stepSize') if isinstance(market.get('info'), dict) else None
    qty_step = float(qty_step or 0.0)

    min_qty = 0.0
    if isinstance(market, dict):
        min_qty = market.get('limits', {}).get('amount', {}).get('min') if isinstance(market.get('limits'), dict) else None
    try:
        min_qty = float(min_qty or 0.0)
    except Exception:
        min_qty = 0.0

    def _reserved_qty(open_orders):
        total = 0.0
        for order in open_orders or []:
            info = order.get('info') or {}
            if not bool(info.get('reduceOnly') or order.get('reduceOnly')):
                continue
            otype = str(order.get('type') or info.get('type') or '').lower()
            if 'stop' not in otype and 'take_profit' not in otype:
                continue
            amount = None
            for key in ('amount', 'origQty', 'qty', 'quantity', 'volume', 'size'):
                val = order.get(key)
                if val not in (None, ''):
                    amount = val
                    break
                if isinstance(info, dict) and info.get(key) not in (None, ''):
                    amount = info.get(key)
                    break
            try:
                total += max(0.0, float(amount)) if amount not in (None, '') else 0.0
            except Exception:
                continue
        return total

    last_error = None
    for attempt in range(int(max_retries)):
        try:
            pos = fetcher.fetch_position(sym, position_mode=position_mode)
        except Exception:
            pos = None
        pos_qty = _position_qty(pos, side)
        if pos_qty <= 0:
            rec['be_last_error'] = 'no position qty'
            return False

        try:
            open_orders = ex.fetch_open_orders(sym)
        except Exception:
            open_orders = []
        available_qty = max(0.0, pos_qty - _reserved_qty(open_orders))
        qty = _round_to_step(available_qty, qty_step) if qty_step else float(available_qty)
        if qty_step and qty < qty_step and available_qty >= qty_step - EPS_QTY:
            qty = qty_step
        if qty < max(min_qty, EPS_QTY):
            last_error = f"qty {qty} < min {min_qty}"
            _time.sleep(retry_sleep)
            continue

        log.info(
            f"[sl->BE place] {sym} attempt={attempt+1}/{max_retries} qty={qty:.6g} trig={trigger} reserved={available_qty - qty:.6g}"
        )
        side_close = 'sell' if str(side).upper() == 'LONG' else 'buy'
        try:
            ex.create_order(
                sym,
                type='stop_market',
                side=side_close,
                amount=qty,
                price=None,
                params={**params, 'triggerPrice': trigger},
            )
            rec['be_moved_once'] = True
            rec['be_last_error'] = None
            return True
        except Exception as exc:
            msg = str(exc)
            rec['be_last_error'] = msg
            last_error = msg
            if 'available amount of 0' in msg or 'must be less than the available amount of 0' in msg:
                _time.sleep(retry_sleep)
                continue
            if 'Stop Loss price should be greater' in msg or 'Stop Loss price should be less' in msg:
                precision = 5
                if isinstance(market, dict):
                    prec_val = market.get('precision', {}).get('price') if isinstance(market.get('precision'), dict) else None
                    if prec_val is not None:
                        precision = int(prec_val)
                eps = 10 ** (-precision)
                trigger = trigger + eps if str(side).upper() == 'LONG' else trigger - eps
                continue
            _time.sleep(retry_sleep)

    log.error(f"[sl->BE fail] {sym} unable to place BE stop: {last_error}")
    return False


def opp_side(side: str) -> str:
    return 'SHORT' if str(side).upper().startswith('LONG') else 'LONG'

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


def _extract_mark_price_from_dict(data):
    if not isinstance(data, dict):
        return None
    for key in (
        'markPrice',
        'mark_price',
        'indexPrice',
        'index_price',
        'lastMarkPrice',
        'last_mark_price',
        'marketPrice',
        'market_price',
    ):
        val = data.get(key)
        if val in (None, ''):
            continue
        v = _safe_float(val)
        if v is not None:
            return v
    return None


def _extract_mark_price(order):
    mark = _extract_mark_price_from_dict(order)
    if mark is None and isinstance(order, dict):
        info = order.get('info')
        if isinstance(info, dict):
            mark = _extract_mark_price_from_dict(info)
    return mark


def _fetch_order_fill(fetcher: CCXTFetcher, sym: str, order_id: str, max_wait_ms: int = 5000):
    """Return (avg_price, datetime) for an order id, polling until filled or timeout."""
    if not order_id:
        return None, None, None
    ccxt_sym = fetcher.resolve_symbol(sym)
    deadline = _time.time() + max_wait_ms / 1000.0
    last_od = None
    while True:
        try:
            od = fetcher.ex.fetch_order(order_id, ccxt_sym)
            last_od = od
            sleep_ms(RATE_MS)
        except Exception as e:
            _dbg('fetch_order', str(e))
            od = last_od or {}
        price = None
        ts = None
        mark_price = _extract_mark_price(od)
        try:
            for k in ('average', 'price', 'avgPrice', 'avg_price'):
                v = od.get(k)
                if v is not None:
                    price = float(v)
                    break
        except Exception:
            pass
        try:
            ts = od.get('timestamp')
            if ts is None:
                dt_str = od.get('datetime')
                if dt_str:
                    ts = int(_dt.datetime.fromisoformat(dt_str.replace('Z', '+00:00')).timestamp() * 1000)
            if ts is None and isinstance(od.get('info'), dict):
                info = od['info']
                for k in ('updateTime', 'transactTime', 'ts'):
                    if info.get(k) is not None:
                        ts = int(info.get(k))
                        break
        except Exception:
            ts = None
        status = str(od.get('status') or '').lower()
        if price is not None or status in ('closed', 'canceled') or _time.time() >= deadline:
            fill_dt = None
            if ts is not None:
                try:
                    fill_dt = _dt.datetime.fromtimestamp(ts / 1000.0, tz=_dt.timezone.utc)
                except Exception:
                    pass
            if mark_price is None:
                try:
                    fetch_fn = getattr(fetcher, 'fetch_mark_price', None)
                    if callable(fetch_fn):
                        mark_price = fetch_fn(sym)
                except Exception:
                    mark_price = None
            return price, fill_dt, mark_price
        sleep_ms(250)

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
    # On BingX (one-way), inline SL in create_order('market') often fails.
    # We'll avoid inline SL here and place it as a separate reduce-only order after open.
    pos_oneway = True if str(position_mode or '').lower().startswith('one') else False
    if tp_price is not None:
        p = dict(base_params)
        p['takeProfit'] = float(tp_price)
        p['takeProfitPrice'] = float(tp_price)
        if pos_oneway:
            p = dict(p)  # copy
            param_candidates.append(dict(p, positionSide='BOTH'))
            param_candidates.append({k:v for k,v in p.items() if k!='positionSide'})
        else:
            param_candidates.append(p)
    # Always add a clean base candidate (no TP/SL inline)
    if pos_oneway:
        param_candidates.append({'reduceOnly': False, 'positionSide': 'BOTH'})
        param_candidates.append({'reduceOnly': False})
    else:
        param_candidates.append(dict(base_params))

    last_res = None
    try:
        _dbg(
            'place_open_long', sym,
            f'qty={qty:.6g}', f'price={price:.6g}',
            f'tp={tp_price if tp_price is not None else "-"}',
            f'sl={sl_price if sl_price is not None else "-"}',
            f'candidates={len(param_candidates)}'
        )
    except Exception:
        pass

    seen = set()
    for params in param_candidates:
        key = json.dumps(params, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        _dbg('try_params', params)
        res = _try(params)
        try:
            _dbg('result', ('ok' if res.get('ok') else 'ERR'), (('order_id=' + str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId'))) if res.get('ok') else ''), res.get('error',''))
        except Exception:
            pass
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
    # On BingX (one-way), inline SL often fails; mirror LONG logic and place SL separately.
    pos_oneway = True if str(position_mode or '').lower().startswith('one') else False
    if tp_price is not None:
        p = dict(base_params)
        p['takeProfit'] = float(tp_price)
        p['takeProfitPrice'] = float(tp_price)
        if pos_oneway:
            p = dict(p)
            param_candidates.append(dict(p, positionSide='BOTH'))
            param_candidates.append({k: v for k, v in p.items() if k != 'positionSide'})
        else:
            param_candidates.append(p)
    # Always add a clean base candidate (no TP/SL inline)
    if pos_oneway:
        param_candidates.append({'reduceOnly': False, 'positionSide': 'BOTH'})
        param_candidates.append({'reduceOnly': False})
    else:
        param_candidates.append(dict(base_params))

    last_res = None
    try:
        _dbg(
            'place_open_short', sym,
            f'qty={qty:.6g}', f'price={price:.6g}',
            f'tp={tp_price if tp_price is not None else "-"}',
            f'sl={sl_price if sl_price is not None else "-"}',
            f'candidates={len(param_candidates)}'
        )
    except Exception:
        pass

    seen = set()
    for params in param_candidates:
        key = json.dumps(params, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        _dbg('try_params', params)
        res = _try(params)
        try:
            _dbg('result', ('ok' if res.get('ok') else 'ERR'), (('order_id=' + str((res.get('order') or {}).get('id') or (res.get('order') or {}).get('orderId'))) if res.get('ok') else ''), res.get('error',''))
        except Exception:
            pass
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
    params = {'reduceOnly': True, 'positionSide': 'BOTH'}
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
                params2 = {'reduceOnly': True, 'positionSide': 'BOTH'}
                od = fetcher.ex.create_order(ccxt_sym, 'market', side_close, qty, None, params2)
                sleep_ms(RATE_MS)
                return od
            except Exception:
                pass

        # If the exchange reports that reduce-only cannot open a position,
        # it likely means there is no open position to close. Treat this as
        # a no-op so the caller can clear the local position and move on.
        err_msg = msg
        if 'reduce only order can only decrease' in err_msg or 'code":80001' in err_msg or 'code":101290' in err_msg:
            cprint('[live reduceOnly missing]', sym, ':', e, fg='yellow')
            return {'error': 'no_position'}

        cprint('[live reduceOnly]', sym, ':', e, fg='red', file=sys.stderr)
        return None


def _report_close_cooldown(sym: str, pos_rec: dict, px: float, bar_key: int):
    # print close-check only in debug mode
    if not globals().get('DEBUG_OPEN'):
        return
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
        msg = (f"[close-check] {sym} side={side} "
               f"tp_gap={fmt(tp_gap)} sl_gap={fmt(sl_gap)} "
               f"nearest={fmt(nearest_val)} ({nearest_label})")
        cc_log_once_per_bar(sym, bar_key, msg)
    except Exception:
        pass


def load_strategy(path_cls: str, cfg: dict):
    mod_path, cls_name = path_cls.rsplit('.', 1)
    mod = importlib.import_module(mod_path)
    cls = getattr(mod, cls_name)
    return cls(cfg)

def _close_if_hit(fetcher: CCXTFetcher, sym: str, entry_side: str, px: float, pos_rec: dict, position_mode: str, now_dt=None, session_db_path=None, bot_id=None):
    side = str(entry_side or pos_rec.get('side', 'LONG')).upper()
    tp = pos_rec.get('tp_price')
    sl = pos_rec.get('sl_price')
    try:
        tp = float(tp) if tp is not None else None
        sl = float(sl) if sl is not None else None
        px = float(px)
    except Exception:
        return None
    sign = 1.0 if side == 'LONG' else -1.0
    if side == 'LONG':
        if tp is not None and px >= tp:
            od = place_reduce_only(fetcher, sym, 'sell', float(pos_rec.get('qty', 0.0)), position_mode)
            if od and isinstance(od, dict) and od.get('error') == 'no_position':
                now_iso = (now_dt or _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)).isoformat()
                mark_price = None
                try:
                    mark_price = fetcher.fetch_mark_price(sym)
                except Exception:
                    mark_price = None
                cprint('[tp close]', sym, f'@~{px:.6g} tp={tp:.6g}', fg='green', bold=True)
                return {'fill_price': px, 'fill_ts': now_iso, 'slip_bp': None, 'lag_sec': None, 'mark_price': mark_price, 'reason': 'TP'}
            if od:
                fill, fdt, mark_price = _fetch_order_fill(fetcher, sym, str(od.get('id') or od.get('orderId') or ''), 8000)
                slip = (fill / px - 1.0) * 10000.0 * sign if fill else None
                lag = (fdt - now_dt).total_seconds() if (fdt and now_dt) else None
                cprint('[tp close]', sym, f'@~{(fill or px):.6g} tp={tp:.6g}', fg='green', bold=True)
                return {
                    'fill_price': fill,
                    'fill_ts': fdt.isoformat() if fdt else None,
                    'slip_bp': slip,
                    'lag_sec': lag,
                    'mark_price': mark_price,
                    'reason': 'TP',
                }
        if sl is not None and px <= sl:
            od = place_reduce_only(fetcher, sym, 'sell', float(pos_rec.get('qty', 0.0)), position_mode)
            if od and isinstance(od, dict) and od.get('error') == 'no_position':
                now_iso = (now_dt or _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)).isoformat()
                mark_price = None
                try:
                    mark_price = fetcher.fetch_mark_price(sym)
                except Exception:
                    mark_price = None
                cprint('[sl close]', sym, f'@~{px:.6g} sl={sl:.6g}', fg='red', bold=True)
                return {'fill_price': px, 'fill_ts': now_iso, 'slip_bp': None, 'lag_sec': None, 'mark_price': mark_price, 'reason': 'SL'}
            if od:
                fill, fdt, mark_price = _fetch_order_fill(fetcher, sym, str(od.get('id') or od.get('orderId') or ''), 8000)
                slip = (fill / px - 1.0) * 10000.0 * sign if fill else None
                lag = (fdt - now_dt).total_seconds() if (fdt and now_dt) else None
                cprint('[sl close]', sym, f'@~{(fill or px):.6g} sl={sl:.6g}', fg='red', bold=True)
                return {
                    'fill_price': fill,
                    'fill_ts': fdt.isoformat() if fdt else None,
                    'slip_bp': slip,
                    'lag_sec': lag,
                    'mark_price': mark_price,
                    'reason': 'SL',
                }
    elif side == 'SHORT':
        if tp is not None and px <= tp:
            od = place_reduce_only(fetcher, sym, 'buy', float(pos_rec.get('qty', 0.0)), position_mode)
            if od and isinstance(od, dict) and od.get('error') == 'no_position':
                now_iso = (now_dt or _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)).isoformat()
                mark_price = None
                try:
                    mark_price = fetcher.fetch_mark_price(sym)
                except Exception:
                    mark_price = None
                cprint('[tp close]', sym, f'@~{px:.6g} tp={tp:.6g}', fg='green', bold=True)
                return {'fill_price': px, 'fill_ts': now_iso, 'slip_bp': None, 'lag_sec': None, 'mark_price': mark_price, 'reason': 'TP'}
            if od:
                fill, fdt, mark_price = _fetch_order_fill(fetcher, sym, str(od.get('id') or od.get('orderId') or ''), 8000)
                slip = (fill / px - 1.0) * 10000.0 * sign if fill else None
                lag = (fdt - now_dt).total_seconds() if (fdt and now_dt) else None
                cprint('[tp close]', sym, f'@~{(fill or px):.6g} tp={tp:.6g}', fg='green', bold=True)
                return {
                    'fill_price': fill,
                    'fill_ts': fdt.isoformat() if fdt else None,
                    'slip_bp': slip,
                    'lag_sec': lag,
                    'mark_price': mark_price,
                    'reason': 'TP',
                }
        if sl is not None and px >= sl:
            od = place_reduce_only(fetcher, sym, 'buy', float(pos_rec.get('qty', 0.0)), position_mode)
            if od and isinstance(od, dict) and od.get('error') == 'no_position':
                now_iso = (now_dt or _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)).isoformat()
                mark_price = None
                try:
                    mark_price = fetcher.fetch_mark_price(sym)
                except Exception:
                    mark_price = None
                cprint('[sl close]', sym, f'@~{px:.6g} sl={sl:.6g}', fg='red', bold=True)
                return {'fill_price': px, 'fill_ts': now_iso, 'slip_bp': None, 'lag_sec': None, 'mark_price': mark_price, 'reason': 'SL'}
            if od:
                fill, fdt, mark_price = _fetch_order_fill(fetcher, sym, str(od.get('id') or od.get('orderId') or ''), 8000)
                slip = (fill / px - 1.0) * 10000.0 * sign if fill else None
                lag = (fdt - now_dt).total_seconds() if (fdt and now_dt) else None
                cprint('[sl close]', sym, f'@~{(fill or px):.6g} sl={sl:.6g}', fg='red', bold=True)
                return {
                    'fill_price': fill,
                    'fill_ts': fdt.isoformat() if fdt else None,
                    'slip_bp': slip,
                    'lag_sec': lag,
                    'mark_price': mark_price,
                    'reason': 'SL',
                }
    return None


def _place_tp_sl_after_open(
    fetcher: CCXTFetcher,
    sym: str,
    side: str,
    qty: float,
    tp_price,
    sl_price,
    position_mode: str,
    part_tp_price: Optional[float] = None,
    part_tp_qty: Optional[float] = None,
    pos_rec: Optional[dict] = None,
):
    """Place TP/SL (and optional partial TP) as reduce-only orders after a market open."""
    try:
        ccxt_sym = fetcher.resolve_symbol(sym)
        pos_oneway = True if str(position_mode or '').lower().startswith('one') else False
        base = {'reduceOnly': True}
        base['positionSide'] = 'BOTH' if pos_oneway else ('LONG' if side=='LONG' else 'SHORT')

        def _try(order_type, order_side, amount, price, params):
            try:
                od = fetcher.ex.create_order(ccxt_sym, order_type, order_side, amount, price, params)
                sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'params': params}
            except Exception as e:
                return {'ok': False, 'error': str(e), 'params': params}

        # ---- Partial TP (50% or as configured) ----
        ptp_ok = False
        if (
            part_tp_price is not None
            and part_tp_price > 0
            and part_tp_qty is not None
            and part_tp_qty > 0
        ):
            ptp_side = "sell" if side == "LONG" else "buy"
            ptp_candidates = [
                ("take_profit", ptp_side, float(part_tp_price), dict(base)),
                ("take_profit_market", ptp_side, None, {**base, "triggerPrice": float(part_tp_price)}),
                ("limit", ptp_side, float(part_tp_price), {**base, "takeProfit": True}),
                ("market", ptp_side, None, {**base, "takeProfitPrice": float(part_tp_price)}),
            ]
            _dbg(
                "ptp_fallback",
                sym,
                f"side={side}",
                f"qty={part_tp_qty:.6g}",
                f"price={part_tp_price}",
                f"pos_mode={position_mode}",
                f"candidates={len(ptp_candidates)}",
            )
            for otype, oside, oprice, pms in ptp_candidates:
                r = _try(otype, oside, part_tp_qty, oprice, pms)
                _dbg("ptp_try", {"type": otype, "side": oside, "price": oprice, "params": pms})
                _dbg(
                    "ptp_res",
                    ("ok" if r.get("ok") else "ERR"),
                    r.get("error", ""),
                    (
                        "order_id="
                        + str((r.get("order") or {}).get("id") or (r.get("order") or {}).get("orderId"))
                    )
                    if r.get("ok")
                    else "",
                )
                if r.get("ok"):
                    ptp_ok = True
                    break

        # adjust remaining qty for full TP if partial TP succeeded
        tp_qty = qty - (part_tp_qty if ptp_ok else 0.0)

        # ---- TP ----
        if tp_price is not None and tp_price > 0 and tp_qty > 0:
            tp_side = 'sell' if side=='LONG' else 'buy'
            tp_candidates = [
                ('take_profit', tp_side, float(tp_price), dict(base)),
                ('take_profit_market', tp_side, None, {**base, 'triggerPrice': float(tp_price)}),
                ('limit', tp_side, float(tp_price), {**base, 'takeProfit': True}),
                ('market', tp_side, None, {**base, 'takeProfitPrice': float(tp_price)}),
            ]
            _dbg('tp_fallback', sym, f'side={side}', f'qty={tp_qty:.6g}', f'price={tp_price}', f'pos_mode={position_mode}', f'candidates={len(tp_candidates)}')
            for otype, oside, oprice, pms in tp_candidates:
                r = _try(otype, oside, tp_qty, oprice, pms)
                _dbg('tp_try', {'type': otype, 'side': oside, 'price': oprice, 'params': pms})
                _dbg('tp_res', ('ok' if r.get('ok') else 'ERR'), r.get('error',''),
                     ('order_id=' + str((r.get('order') or {}).get('id') or (r.get('order') or {}).get('orderId'))) if r.get('ok') else '')
                if r.get('ok'):
                    break

        # ---- SL ----
        if pos_rec and pos_rec.get('sl_be_done'):
            sl_price = None

        if sl_price is not None and sl_price > 0:
            sl_side = 'sell' if side=='LONG' else 'buy'
            sl_candidates = [  # prefer stop_market with triggerPrice only to avoid "SL Price must be lower than Trigger Price"
                ('stop_market', sl_side, None, {**base, 'triggerPrice': float(sl_price)}),
                ('stop_market', sl_side, None, {**base, 'stopPrice': float(sl_price)}),
                ('market', sl_side, None, {**base, 'stopLossPrice': float(sl_price)}),
                ('stop', sl_side, float(sl_price), dict(base)),
            ]
            _dbg('sl_fallback', sym, f'side={side}', f'qty={qty:.6g}', f'price={sl_price}', f'pos_mode={position_mode}', f'candidates={len(sl_candidates)}')
            for otype, oside, oprice, pms in sl_candidates:
                r = _try(otype, oside, qty, oprice, pms)
                _dbg('sl_try', {'type': otype, 'side': oside, 'price': oprice, 'params': pms})
                _dbg('sl_res', ('ok' if r.get('ok') else 'ERR'), r.get('error',''),
                     ('order_id=' + str((r.get('order') or {}).get('id') or (r.get('order') or {}).get('orderId'))) if r.get('ok') else '')
                if r.get('ok'):
                    break
    except Exception as e:
        _dbg('post_open_error', str(e))




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
    notional_v, notional_origin = _cfg_pick(cfg, ['notional','position_notional','runner.notional','live.notional','portfolio.position_notional'], 2.2)
    max_nf_v, max_nf_origin = _cfg_pick(cfg, ['max_notional_frac','runner.max_notional_frac','live.max_notional_frac','portfolio.max_notional_frac'], 0.5)
    init_eq_v, init_eq_origin = _cfg_pick(cfg, ['initial_equity','runner.initial_equity','live.initial_equity','portfolio.initial_equity'], 100.0)
    position_mode_v, position_mode_origin = _cfg_pick(cfg, ['position_mode','runner.position_mode','live.position_mode','session.position_mode'], 'hedge')
    tf_v, tf_origin = _cfg_pick(cfg, ['timeframe','runner.timeframe','live.timeframe'], '1h')
    top_n = int(top_n_v)
    notional = float(notional_v)
    max_notional_frac = float(max_nf_v)
    initial_equity = float(init_eq_v)
    position_mode = str(position_mode_v)
    tf = str(tf_v)
    tf_sec = _tf_to_seconds(tf)
    BAR_SECONDS = tf_sec

    # Build a stable results directory to accumulate session data
    cfg_name = os.path.splitext(os.path.basename(getattr(args, 'cfg', 'cfg')))[0]
    base_live_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '_reports', '_live'))
    args.results_dir = os.path.join(base_live_dir, f"livecfg_{cfg_name}_{tf}")

    # Optional: open entries based on heat (even if entry_signal is None)
    open_on_heat_v, open_on_heat_origin = _cfg_pick(cfg, ['open_on_heat','runner.open_on_heat','live.open_on_heat'], False)
    open_heat_min_v, open_heat_min_origin = _cfg_pick(cfg, ['open_heat_min','runner.open_heat_min','live.open_heat_min'], 0.80)
    open_on_heat = bool(open_on_heat_v)
    open_heat_min = float(open_heat_min_v)
    cprint('[cfg]', f'top_n={top_n}, notional={notional}, timeframe={tf}, position_mode={position_mode}, max_notional_frac={max_notional_frac}, initial_equity={initial_equity}, open_on_heat={open_on_heat}, heat_min={open_heat_min}', fg='magenta')
    if getattr(args, 'debug', False):
        _debug_dump_effective(cfg, strat, args,
            resolved={
                'top_n': (top_n, top_n_origin),
                'notional': (notional, notional_origin),
                'max_notional_frac': (max_notional_frac, max_nf_origin),
                'initial_equity': (initial_equity, init_eq_origin),
                'position_mode': (position_mode, position_mode_origin),
                'timeframe': (tf, tf_origin),
                'open_on_heat': (bool(open_on_heat_v), open_on_heat_origin),
                'open_heat_min': (float(open_heat_min_v), open_heat_min_origin),
            }, env_over={}
        )

    os.makedirs(args.results_dir, exist_ok=True)
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, args.session_db, args.cache_out)

    # ---- resolve prewarm config ----
    pw_cfg = ((cfg.get("runner") or {}).get("prewarm") or {})
    prewarm_bars = pw_cfg.get("bars")
    prewarm_hours = pw_cfg.get("hours")
    if getattr(args, "prewarm_bars", None) is not None:
        prewarm_bars = args.prewarm_bars
    if getattr(args, "prewarm_hours", None) is not None:
        prewarm_hours = args.prewarm_hours
    if prewarm_hours and not prewarm_bars:
        prewarm_bars = int(prewarm_hours * 3600 // _tf_to_sec(tf))
    if not prewarm_bars:
        prewarm_bars = _infer_prewarm_bars(cfg, tf)

    md_warm_cache = {}
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
    universe0 = [s for s in all_syms if (not allow or s in allow)]

    def _prewarm_sym(sym: str):
        lim = max(int(getattr(args, 'limit_klines', 200) or 200), prewarm_bars + 50)
        df = fetcher.fetch_ohlcv_df(sym, timeframe=tf, limit=lim)
        if df is None or len(df) == 0:
            return
        feats_df = compute_feats(df, tf_seconds=tf_sec)
        if args.hour_cache in ('save', 'load'):
            try:
                cache_out_upsert(cache_out_path, sym, feats_df)
            except Exception:
                pass
        md_warm_cache[sym] = feats_df.iloc[-1].to_dict()

    for sym in universe0:
        _prewarm_sym(sym)

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
        ensured = False
        for rec in positions.values():
            missing = any(
                key not in rec
                for key in (
                    'entry_qty',
                    'be_plan_id',
                    'be_plan_active',
                    'sl_be_done',
                    'tp50_trigger',
                    'be_plan_activated_ts',
                    'be_plan_last_fallback_ts',
                )
            )
            prev_tp50 = rec.get('tp50_trigger')
            _ensure_be_fields(rec)
            if missing or rec.get('tp50_trigger') != prev_tp50:
                ensured = True
        if ensured:
            save_positions(args.results_dir, positions)
            for rec2 in positions.values():
                try:
                    db_upsert_open_position(
                        session_db_path,
                        bot_id,
                        {**rec2, 'status': 'OPEN', 'exchange': args.exchange, 'timeframe': tf},
                    )
                except Exception:
                    pass
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
            mark_price = _extract_mark_price_from_dict(p)
            for k in ('entryPrice','entry'):
                try:
                    v = p.get(k)
                    if v:
                        entry = float(v); break
                except Exception:
                    pass
            if mark_price is None and isinstance(p.get('info'), dict):
                mark_price = _extract_mark_price_from_dict(p['info'])
            ex_list.append({'symbol': sym, 'qty': abs(qty), 'entry': entry, 'mark_price': mark_price})
        except Exception:
            continue
    cprint('[exchange]', f'open positions: {len(ex_list)}', fg='cyan')
    for es in ex_list:
        mark_str = f" mark={es['mark_price']}" if es.get('mark_price') is not None else ''
        cprint('   -', es['symbol'], f"qty={es['qty']} entry={es['entry']}{mark_str}", fg='cyan', dim=True)

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
                now_iso = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc).isoformat()
                px_now = fetcher.fetch_ticker_price(sym) or rec.get('entry') or 0.0
                mark_px_now = None
                try:
                    mark_px_now = fetcher.fetch_mark_price(sym)
                except Exception:
                    mark_px_now = None
                reason_text = 'sync_cleanup'
                db_mark_closed(
                    session_db_path,
                    bot_id,
                    rec.get('order_id'),
                    now_iso,
                    exit_fill=px_now,
                    exit_fill_ts=now_iso,
                    exit_mark_price=mark_px_now,
                    close_reason=reason_text,
                )
            except Exception:
                pass
            positions.pop(sym, None)
    save_positions(args.results_dir, positions)

    _last_close_check_bar_by_sym = {}
    last_bar_ts = None
    cprint('[live]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s', fg='cyan')
    while True:
        now = _dt.datetime.utcnow().replace(tzinfo=_dt.timezone.utc)
        bar_close = _align_bar_close(now, tf_sec)

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

        # sync local positions with exchange; drop stale entries and update qty
        sync_map = get_exchange_open_positions(fetcher) if positions else {}
        sync_changed = False
        for sym, rec in list(positions.items()):
            _ensure_be_fields(rec)
            ex_rec = sync_map.get(sym)
            if not ex_rec or float(ex_rec.get('qty', 0.0)) <= 0.0:
                cprint('[desync-miss]', sym, 'NOT found on exchange -> closing locally', fg='yellow')
                mark_closed_now(fetcher, session_db_path, bot_id, sym, rec.get('order_id'), reason='sync_cleanup')
                positions.pop(sym, None)
                sync_changed = True
                continue
            ex_qty = float(ex_rec.get('qty', 0.0))
            try:
                cur_qty = float(rec.get('qty', 0.0))
            except Exception:
                cur_qty = 0.0
            if abs(ex_qty - cur_qty) > max(1e-8, 0.01 * max(1.0, ex_qty)):
                rec['qty'] = ex_qty
                sync_changed = True
            ex_entry = _safe_float(ex_rec.get('entry'))
            if ex_entry is not None:
                if _safe_float(rec.get('entry_fill')) != ex_entry:
                    rec['entry_fill'] = ex_entry
                    sync_changed = True
                if rec.get('entry') is None:
                    rec['entry'] = ex_entry
            ex_mark = _safe_float(ex_rec.get('mark_price'))
            if ex_mark is not None and _safe_float(rec.get('entry_mark_price')) != ex_mark:
                rec['entry_mark_price'] = ex_mark
                sync_changed = True
        if sync_changed:
            save_positions(args.results_dir, positions)
            for rec2 in positions.values():
                try:
                    db_upsert_open_position(
                        session_db_path,
                        bot_id,
                        {**rec2, 'status': 'OPEN', 'exchange': args.exchange, 'timeframe': tf},
                    )
                except Exception:
                    pass

        for sym, rec in list(positions.items()):
            _ensure_be_fields(rec)
            now = datetime.now(timezone.utc)
            bar_key = _bar_key(now, BAR_SECONDS)
            if _last_close_check_bar_by_sym.get(sym) == bar_key:
                continue
            _last_close_check_bar_by_sym[sym] = bar_key
            px = fetcher.fetch_ticker_price(sym)
            if px is not None:
                _report_close_cooldown(sym, rec, px, bar_key)
                rec_changed = False
                side_long = str(rec.get('side', 'LONG')).upper() == 'LONG'
                tp50_trigger = rec.get('tp50_trigger')
                _, be_price = _calc_tp50_and_be(rec)
                try:
                    px_f = float(px)
                except Exception:
                    px_f = None
                if px_f is not None:
                    if side_long:
                        prev_high = _safe_float(rec.get('be_high_price'))
                        if prev_high is None or px_f > prev_high + 1e-12:
                            rec['be_high_price'] = px_f
                            rec_changed = True
                    else:
                        prev_low = _safe_float(rec.get('be_low_price'))
                        if prev_low is None or px_f < prev_low - 1e-12:
                            rec['be_low_price'] = px_f
                            rec_changed = True
                try:
                    sl_cur = float(rec.get('sl_price') or rec.get('sl') or 0.0)
                except Exception:
                    sl_cur = 0.0
                if be_price:
                    tolerance = max(1e-12, abs(be_price) * 1e-6)
                    if side_long and sl_cur >= be_price - tolerance:
                        if not rec.get('sl_be_done'):
                            rec['sl_be_done'] = True
                            rec_changed = True
                    elif (not side_long) and sl_cur <= be_price + tolerance:
                        if not rec.get('sl_be_done'):
                            rec['sl_be_done'] = True
                            rec_changed = True

                if rec_changed:
                    save_positions(args.results_dir, positions)
                    try:
                        db_upsert_open_position(
                            session_db_path,
                            bot_id,
                            {**rec, 'status': 'OPEN', 'exchange': args.exchange, 'timeframe': tf},
                        )
                    except Exception:
                        pass


                tf_sec_local = int(_tf_to_seconds(tf))
                try:
                    bar_close_local = bar_close
                except NameError:
                    bar_close_local = None
                if not bar_close_local:
                    now_utc = datetime.now(timezone.utc)
                    bar_close_local = now_utc.replace(second=0, microsecond=0)
                    bar_close_local = bar_close_local - _dt.timedelta(seconds=(bar_close_local.timestamp() % tf_sec_local))
                ccxt_sym = fetcher.resolve_symbol(sym) or sym
                feats_row = {}
                if args.hour_cache == 'load':
                    feats_row = read_hour_cache_row(cache_out_path, ccxt_sym, bar_close_local)
                if not feats_row:
                    lim = max(prewarm_bars + 2, getattr(args, 'limit_klines', 0) or 0, 60)
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=tf, limit=lim)
                    if df is not None and len(df):
                        feats_df = compute_feats(df, tf_seconds=tf_sec_local)
                        if feats_df is not None and len(feats_df):
                            if args.hour_cache in ('save', 'load'):
                                try:
                                    cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                                except Exception:
                                    pass
                            feats_row = feats_df.iloc[-1].to_dict()
                feats_row = feats_row or {}

                def _flt(val, default=0.0):
                    fv = _safe_float(val)
                    return fv if fv is not None else float(default)

                close_val = _safe_float(feats_row.get('close'))
                if close_val is None:
                    try:
                        close_val = float(px)
                    except Exception:
                        close_val = 0.0
                row = {
                    'close': float(close_val),
                    'atr_ratio': float(_flt(feats_row.get('atr_ratio'))),
                    'dp6h': float(_flt(feats_row.get('dp6h'))),
                    'dp12h': float(_flt(feats_row.get('dp12h'))),
                    'quote_volume': float(_flt(feats_row.get('quote_volume'))),
                    'qv_24h': float(_flt(feats_row.get('qv_24h'))),
                }

                ex = None
                try:
                    if hasattr(strat, 'manage_position_v2'):
                        ex = strat.manage_position_v2(sym, row, _pos_adapter(rec), ctx=None)
                    elif hasattr(strat, 'manage_position'):
                        ex = strat.manage_position(sym, row, _pos_adapter(rec), ctx=None)
                except Exception:
                    ex = None

                if ex:
                    side_close = 'sell' if str(rec.get('side', 'LONG')).upper() == 'LONG' else 'buy'
                    qty_total = float(rec.get('qty', 0.0))

                    if getattr(ex, 'action', None) == 'TP_PARTIAL':
                        frac = max(0.0, min(1.0, float(getattr(ex, 'qty_frac', 0.5))))
                        qty_close = qty_total * frac

                        ccxt_sym = fetcher.resolve_symbol(sym) or sym
                        mkt = fetcher.markets.get(ccxt_sym, {})
                        step = float(mkt.get('precision', {}).get('amount') or 0.0)
                        min_qty = float(mkt.get('limits', {}).get('amount', {}).get('min') or 0.0)
                        min_notional = float(mkt.get('limits', {}).get('cost', {}).get('min') or getattr(strat, 'exchange_min_notional', 0.0))
                        qty_close = round_to_step(qty_close, step)

                        price = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                        if qty_close < max(min_qty, float(getattr(strat, 'min_qty', 0.0))) or price * qty_close < min_notional:
                            cprint('[tp_partial skip] too small', sym, fg='yellow')
                        else:
                            od = place_reduce_only(fetcher, sym, side_close, qty_close, position_mode)
                            executed = False
                            now_utc = datetime.now(timezone.utc)
                            if isinstance(od, dict) and od.get('error') == 'no_position':
                                fill = price
                                fdt = now_utc
                                try:
                                    mark_price = fetcher.fetch_mark_price(sym)
                                except Exception:
                                    mark_price = None
                                executed = True
                            elif od:
                                order_id = str(od.get('id') or od.get('orderId') or '')
                                fill, fdt, mark_price = _fetch_order_fill(fetcher, sym, order_id, 8000)
                                if fill is None:
                                    fill = float(price if 'price' in locals() and price is not None else row.get('close') or 0.0)
                                executed = True

                            if executed:
                                now_utc = datetime.now(timezone.utc)
                                entry = float(rec.get('entry', 0.0))
                                is_long = (str(rec.get('side', 'LONG')).upper() == 'LONG')
                                gross = (fill - entry) / entry if entry and is_long else ((entry - fill) / entry if entry else 0.0)
                                if not is_long and entry == 0:
                                    gross = 0.0
                                fee_rate = float(getattr(strat, 'fee_rate', 0.0))
                                fees = (entry * qty_close + fill * qty_close) * fee_rate
                                net = gross - (fees / (entry * qty_close)) if entry and qty_close else gross
                                realized = net * entry * qty_close

                                try:
                                    insert_order_row(session_db_path, {
                                        'order_id': str(uuid.uuid4()),
                                        'ts_utc': now_utc.isoformat(),
                                        'bar_time_utc': now_utc.isoformat(),
                                        'mode': 'TP_PARTIAL',
                                        'symbol': sym,
                                        'side': side_close,
                                        'type': 'market',
                                        'price': float(fill),
                                        'qty': float(qty_close),
                                        'status': 'filled',
                                        'reason': getattr(ex, 'reason', 'TP_PARTIAL'),
                                        'run_id': run_id,
                                        'extra': json.dumps({
                                            'gross_return': gross,
                                            'net_return': net,
                                            'fees_paid': fees,
                                            'realized_pnl': realized,
                                            'mark_price': mark_price,
                                        })
                                    })
                                except Exception:
                                    pass

                                rec['qty'] = max(0.0, qty_total - qty_close)
                                _ensure_be_fields(rec)
                                rec['tp_hits'] = int(rec.get('tp_hits', 0)) + 1
                                try:
                                    db_upsert_open_position(session_db_path, bot_id, rec)
                                except Exception:
                                    pass

                                remaining_qty = float(rec.get('qty', 0.0))
                                if remaining_qty > 0:
                                    side_dir = str(rec.get('side', 'LONG')).upper()
                                    ccxt_sym = fetcher.resolve_symbol(sym) or sym
                                    be_plan = rec.get('be_plan') if isinstance(rec.get('be_plan'), dict) else None
                                    should_move_be = (
                                        be_plan
                                        and not rec.get('be_moved_once')
                                        and int(rec.get('tp_hits', 0)) == 1
                                    )
                                    if should_move_be:
                                        retry_sleep = BE_RETRY_BACKOFF[0] if BE_RETRY_BACKOFF else 1.0
                                        if BE_POST_TP_DELAY_SEC > 0:
                                            _time.sleep(BE_POST_TP_DELAY_SEC)
                                        ok_be = _place_be_plan_on_exchange(
                                            fetcher,
                                            ccxt_sym,
                                            rec,
                                            position_mode,
                                            be_plan,
                                            max_retries=BE_MAX_RETRIES,
                                            retry_sleep=retry_sleep,
                                            post_cancel_wait=BE_CANCEL_WAIT_SEC,
                                        )
                                        if ok_be:
                                            new_sl = float(be_plan.get('trigger'))
                                            rec['sl_price'] = new_sl
                                            rec['sl'] = new_sl
                                            rec['sl_be_done'] = True
                                            rec['sl_be_done_at'] = _time.time()
                                            rec['sl_be_retry_at'] = 0
                                            log.info(f"[sl->BE ok] {sym} moved to {new_sl}")
                                            save_positions(args.results_dir, positions)
                                            try:
                                                db_upsert_open_position(session_db_path, bot_id, rec)
                                            except Exception:
                                                pass
                                        else:
                                            log.error(f"[sl->BE extra-fail] {sym} {rec.get('be_last_error')}")
                                    try:
                                        _place_tp_sl_after_open(
                                            fetcher,
                                            sym,
                                            rec.get('side', 'LONG'),
                                            remaining_qty,
                                            rec.get('tp_price'),
                                            rec.get('sl_price'),
                                            position_mode,
                                            pos_rec=rec,
                                        )
                                    except Exception:
                                        pass

                                save_positions(args.results_dir, positions)

                                continue

                    if getattr(ex, 'action', None) in ('EXIT', 'TP', 'SL'):
                        qty_close = qty_total
                        px_for_reason = fetcher.fetch_ticker_price(sym) or float(row.get('close') or 0.0)
                        od = place_reduce_only(fetcher, sym, side_close, qty_close, position_mode)
                        executed = False
                        now_utc = datetime.now(timezone.utc)
                        action = str(getattr(ex, 'action', '')).upper()
                        fallback_reason = {
                            'TP': 'TP',
                            'SL': 'SL',
                            'EXIT': 'market_exit',
                        }.get(action, action or 'market_exit')
                        reason_text = _normalize_close_reason(getattr(ex, 'reason', None), fallback=fallback_reason)
                        if isinstance(od, dict) and od.get('error') == 'no_position':
                            fill = px_for_reason
                            fdt = now_utc
                            try:
                                mark_price = fetcher.fetch_mark_price(sym)
                            except Exception:
                                mark_price = None
                            executed = True
                        elif od:
                            order_id = str(od.get('id') or od.get('orderId') or '')
                            fill, fdt, mark_price = _fetch_order_fill(fetcher, sym, order_id, 8000)
                            if fill is None:
                                fill = float(px_for_reason if 'px_for_reason' in locals() and px_for_reason is not None else row.get('close') or 0.0)
                            executed = True

                        if executed:
                            now_utc = datetime.now(timezone.utc)
                            entry = float(rec.get('entry', 0.0))
                            is_long = (str(rec.get('side', 'LONG')).upper() == 'LONG')
                            gross = (fill - entry) / entry if entry and is_long else ((entry - fill) / entry if entry else 0.0)
                            if not is_long and entry == 0:
                                gross = 0.0
                            fee_rate = float(getattr(strat, 'fee_rate', 0.0))
                            fees = (entry * qty_close + fill * qty_close) * fee_rate
                            net = gross - (fees / (entry * qty_close)) if entry and qty_close else gross
                            realized = net * entry * qty_close

                            try:
                                insert_order_row(session_db_path, {
                                    'order_id': str(uuid.uuid4()),
                                    'ts_utc': now_utc.isoformat(),
                                    'bar_time_utc': now_utc.isoformat(),
                                    'mode': 'EXIT',
                                    'symbol': sym,
                                    'side': side_close,
                                    'type': 'market',
                                    'price': float(fill),
                                    'qty': float(qty_close),
                                    'status': 'filled',
                                    'reason': reason_text,
                                    'run_id': run_id,
                                    'extra': json.dumps({
                                        'gross_return': gross,
                                        'net_return': net,
                                        'fees_paid': fees,
                                        'realized_pnl': realized,
                                        'mark_price': mark_price,
                                    })
                                })
                            except Exception:
                                pass

                            rec['close_reason'] = reason_text
                            try:
                                db_mark_closed(
                                    session_db_path,
                                    bot_id,
                                    rec.get('order_id'),
                                    now_utc.isoformat(),
                                    exit_fill=fill,
                                    exit_fill_ts=fdt.isoformat() if fdt else None,
                                    exit_mark_price=mark_price,
                                    close_reason=reason_text,
                                )
                            except Exception:
                                pass

                            try:
                                ccxt_sym = fetcher.resolve_symbol(sym) or sym
                                fetcher.ex.cancel_all_orders(ccxt_sym)
                                sleep_ms(RATE_MS)
                            except Exception:
                                pass

                            positions.pop(sym, None)
                            save_positions(args.results_dir, positions)
                            continue
                closed = _close_if_hit(fetcher, sym, rec.get('side', 'LONG'), px, rec, position_mode, now, session_db_path, bot_id)
                if closed:
                    reason_text = _normalize_close_reason(closed.get('reason'), fallback='market_exit')
                    if not closed.get('already_marked'):
                        try:
                            db_mark_closed(
                                session_db_path,
                                bot_id,
                                rec.get('order_id'),
                                now.isoformat(),
                                exit_fill=closed.get('fill_price'),
                                exit_fill_ts=closed.get('fill_ts'),
                                exit_slip_bp=closed.get('slip_bp'),
                                exit_lag_sec=closed.get('lag_sec'),
                                exit_mark_price=closed.get('mark_price'),
                                close_reason=reason_text,
                            )
                        except Exception:
                            pass
                    rec['close_reason'] = reason_text
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
                    lim = max(prewarm_bars + 2, getattr(args, 'limit_klines', 0) or 0, 60)
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=tf, limit=lim)
                    if df is None or len(df) < max(30, prewarm_bars):
                        continue
                    feats_df = compute_feats(df, tf_seconds=tf_sec)
                    if args.hour_cache in ('save', 'load'):
                        try:
                            cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                        except Exception:
                            pass
                    feats = feats_df.iloc[-1].to_dict()
                md[ccxt_sym] = feats
                print_dot_once_per_bar(datetime.now(timezone.utc), BAR_SECONDS)

            md_symbols = list(md.keys())
            heat_report_enabled = bool(getattr(args, 'heat_report', False))
            heat_debug_verbose = bool(heat_report_enabled and getattr(args, 'debug', False))
            pre_rank_syms = []
            if heat_report_enabled and hasattr(strat, 'rank'):
                try:
                    pre_rank_syms = strat.rank(bar_close, md, md_symbols)
                    if pre_rank_syms is None:
                        pre_rank_syms = []
                    else:
                        pre_rank_syms = list(pre_rank_syms)
                except Exception as e:
                    cprint('[heat.debug]', f'rank(all) failed: {e}', fg='yellow', dim=True)
                    pre_rank_syms = []

            uni_raw = strat.universe(bar_close, md)
            uni = list(uni_raw) if uni_raw is not None else []
            # Strategy already enforces its own top_n; avoid double-slicing
            ranked_raw = strat.rank(bar_close, md, uni)
            ranked = list(ranked_raw) if ranked_raw is not None else []
            _dbg('ranked', ranked[:5], 'top_n=', top_n)
            if heat_report_enabled:
                try:
                    _log_heat_debug_snapshot(
                        strat,
                        bar_close,
                        md,
                        md_symbols,
                        pre_rank_syms,
                        uni,
                        ranked,
                        top_n,
                        log_pre=heat_debug_verbose,
                    )
                except Exception as exc:
                    cprint('[heat.debug]', f'log failed: {exc}', fg='yellow', dim=True)
                debug_limit = top_n if top_n > 0 else 10
                if debug_limit <= 0:
                    debug_limit = 10
                uni_set = set(uni or [])
                pre_syms = pre_rank_syms if pre_rank_syms else md_symbols
                cprint(
                    '[heat.debug]',
                    f'limit={debug_limit}',
                    f'pre_candidates={len(pre_syms)}',
                    f'post_candidates={len(ranked)}',
                    fg='cyan',
                    dim=True,
                )
                _log_heat_distances('pre', strat, bar_close, md, pre_syms, debug_limit, uni_set=uni_set)
                _log_heat_distances('post', strat, bar_close, md, ranked, debug_limit)
                best_all = _call_best_entry_distance_safe(strat, bar_close, md, symbols=md_symbols)
                if best_all:
                    _log_heat_best('pre', best_all)
                if uni:
                    best_uni = _call_best_entry_distance_safe(strat, bar_close, md, symbols=uni)
                    if best_uni:
                        _log_heat_best('post', best_uni)
            opened = 0
            equity = get_account_equity(fetcher)
            position_notional = sum(
                (p.get('qty', 0.0)) * ((p.get('entry_fill') or p.get('entry') or 0.0))
                for p in positions.values()
            )
            for sym in ranked:
                if sym in positions:
                    _dbg(sym, 'skip: already tracked')
                    log_skip_reason(sym, 'already open by THIS bot')
                    continue
                # Use CURRENT equity (fallback to initial if exchange returns 0)
                #curr_equity = float(equity) if equity else float(initial_equity)
                curr_equity = float(initial_equity)
                if position_notional + notional > max_notional_frac * curr_equity:
                    log_skip_reason(
                        sym,
                        f"budget cap reached (equity={float(equity):.2f}, pos={notional:.2f})",
                    )
                    break
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
                            fill, fdt, mark = _fetch_order_fill(fetcher, sym, ex_order_id)
                            entry_fill = float(fill) if fill is not None else float(entry_px)
                            lag_sec = (fdt - bar_close).total_seconds() if fdt else None
                            slip_bp = (entry_fill / float(entry_px) - 1.0) * 10000.0 * (1 if side_str == 'LONG' else -1)
                            entry_mark_price = _safe_float(mark)
                            cprint('[open OK]', f'{sym} {side_str} qty={qty:.6g} px={entry_px}'+(f' id={ex_order_id}' if ex_order_id else ''), fg='green', bold=True)
                            rec = {'symbol': sym,'side': side_str,'qty': qty,'entry': float(entry_px),'tp_price': float(tp_price) if tp_price is not None else None,'sl_price': float(sl_price) if sl_price is not None else None,'ts_open': bar_close.isoformat(),'run_id': run_id,'order_id': str(uuid.uuid4()),'exchange_order_id': ex_order_id,'entry_fill': entry_fill,'entry_fill_ts': fdt.isoformat() if fdt else None,'entry_slip_bp': slip_bp,'entry_lag_sec': lag_sec,'entry_mark_price': entry_mark_price}
                            _ensure_be_fields(rec)
                            position_notional += qty * entry_fill
                            # Fallback TP/SL placement as separate orders (reduce-only)
                            part_tp_price = part_tp_qty = None
                            if getattr(strat, 'partial_tp_enable', False) and tp_price is not None:
                                trig = float(getattr(strat, 'partial_trigger_frac_of_tp', 0.5))
                                frac = float(getattr(strat, 'partial_tp_frac', 0.5))
                                frac = max(0.0, min(1.0, frac))
                                ccxt_sym = fetcher.resolve_symbol(sym) or sym
                                _, lot_step, _ = _market_steps(fetcher, ccxt_sym)
                                if side_str == 'SHORT':
                                    path = entry_px - tp_price
                                    part_tp_price = entry_px - trig * path
                                else:
                                    path = tp_price - entry_px
                                    part_tp_price = entry_px + trig * path
                                qty_parts = allocate_tp_ladder(qty, [frac, max(0.0, 1.0 - frac)], lot_step)
                                part_tp_qty = qty_parts[0] if qty_parts else 0.0
                            try:
                                _place_tp_sl_after_open(
                                    fetcher,
                                    sym,
                                    side_str,
                                    qty,
                                    tp_price,
                                    sl_price,
                                    position_mode,
                                    part_tp_price,
                                    part_tp_qty,
                                    pos_rec=rec,
                                )
                            except Exception as e: _dbg('post_open_error', str(e))
                            tp50, be_price = _calc_tp50_and_be(rec)
                            rec['tp50_trigger'] = tp50
                            rec['be_plan'] = {'trigger': float(be_price)} if be_price else None
                            rec['be_moved_once'] = False
                            rec['be_last_error'] = None
                            rec['tp_hits'] = 0
                            rec['be_plan_id'] = None
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
                    #_dbg(sym, 'skip: SHORT disabled (oneway-long-only)'); log_skip_reason(sym, 'short disabled'); continue
                # if side_up in ('SHORT','SELL'):  # fallback disabled above
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
                    fill, fdt, mark = _fetch_order_fill(fetcher, sym, ex_order_id)
                    entry_fill = float(fill) if fill is not None else float(entry_px)
                    lag_sec = (fdt - bar_close).total_seconds() if fdt else None
                    slip_bp = (entry_fill / float(entry_px) - 1.0) * 10000.0 * -1  # SHORT
                    entry_mark_price = _safe_float(mark)
                    cprint('[open OK]', f'{sym} SHORT qty={qty:.6g} px={entry_px}'+(f' id={ex_order_id}' if ex_order_id else ''), fg='green', bold=True)
                    rec = {'symbol': sym,'side': 'SHORT','qty': qty,'entry': float(entry_px),'tp_price': float(tp_price) if tp_price is not None else None,'sl_price': float(sl_price) if sl_price is not None else None,'ts_open': bar_close.isoformat(),'run_id': run_id,'order_id': str(uuid.uuid4()),'exchange_order_id': ex_order_id,'entry_fill': entry_fill,'entry_fill_ts': fdt.isoformat() if fdt else None,'entry_slip_bp': slip_bp,'entry_lag_sec': lag_sec,'entry_mark_price': entry_mark_price}
                    _ensure_be_fields(rec)
                    position_notional += qty * entry_fill
                    # Fallback TP/SL placement as separate orders (reduce-only)
                    part_tp_price = part_tp_qty = None
                    if getattr(strat, 'partial_tp_enable', False) and tp_price is not None:
                        trig = float(getattr(strat, 'partial_trigger_frac_of_tp', 0.5))
                        frac = float(getattr(strat, 'partial_tp_frac', 0.5))
                        frac = max(0.0, min(1.0, frac))
                        path = entry_px - tp_price
                        part_tp_price = entry_px - trig * path
                        ccxt_sym = fetcher.resolve_symbol(sym) or sym
                        _, lot_step, _ = _market_steps(fetcher, ccxt_sym)
                        qty_parts = allocate_tp_ladder(qty, [frac, max(0.0, 1.0 - frac)], lot_step)
                        part_tp_qty = qty_parts[0] if qty_parts else 0.0
                    try:
                        _place_tp_sl_after_open(
                            fetcher,
                            sym,
                            'SHORT',
                            qty,
                            tp_price,
                            sl_price,
                            position_mode,
                            part_tp_price,
                            part_tp_qty,
                            pos_rec=rec,
                        )
                    except Exception as e:
                        _dbg('post_open_error', str(e))
                    tp50, be_price = _calc_tp50_and_be(rec)
                    rec['tp50_trigger'] = tp50
                    rec['be_plan'] = {'trigger': float(be_price)} if be_price else None
                    rec['be_moved_once'] = False
                    rec['be_last_error'] = None
                    rec['tp_hits'] = 0
                    rec['be_plan_id'] = None
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
                fill, fdt, mark = _fetch_order_fill(fetcher, sym, ex_order_id)
                entry_fill = float(fill) if fill is not None else float(entry_px)
                lag_sec = (fdt - bar_close).total_seconds() if fdt else None
                slip_bp = (entry_fill / float(entry_px) - 1.0) * 10000.0
                entry_mark_price = _safe_float(mark)
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
                    'exchange_order_id': ex_order_id,
                    'entry_fill': entry_fill,
                    'entry_fill_ts': fdt.isoformat() if fdt else None,
                    'entry_slip_bp': slip_bp,
                    'entry_lag_sec': lag_sec,
                    'entry_mark_price': entry_mark_price,
                }
                _ensure_be_fields(rec)
                position_notional += qty * entry_fill
                # Fallback TP/SL placement as separate orders (reduce-only)
                part_tp_price = part_tp_qty = None
                if getattr(strat, 'partial_tp_enable', False) and tp_price is not None:
                    trig = float(getattr(strat, 'partial_trigger_frac_of_tp', 0.5))
                    frac = float(getattr(strat, 'partial_tp_frac', 0.5))
                    path = tp_price - entry_px
                    part_tp_price = entry_px + trig * path
                    part_tp_qty = qty * frac
                try:
                    _place_tp_sl_after_open(
                        fetcher,
                        sym,
                        side_str,
                        qty,
                        tp_price,
                        sl_price,
                        position_mode,
                        part_tp_price,
                        part_tp_qty,
                        pos_rec=rec,
                    )
                except Exception as e:
                    _dbg('post_open_error', str(e))
                tp50, be_price = _calc_tp50_and_be(rec)
                rec['tp50_trigger'] = tp50
                rec['be_plan'] = {'trigger': float(be_price)} if be_price else None
                rec['be_moved_once'] = False
                rec['be_last_error'] = None
                rec['tp_hits'] = 0
                rec['be_plan_id'] = None
                positions[sym] = rec
                save_positions(args.results_dir, positions)
                try:
                    db_upsert_open_position(
                        session_db_path,
                        bot_id,
                        {**rec, 'status': 'OPEN', 'exchange': args.exchange, 'timeframe': tf},
                    )
                except Exception as e:
                    cprint('[db upsert OPEN]', e, fg='red')
                opened += 1

            if args.heat_report and opened == 0:
                print_and_save_heat_from_strategy(strat, 'live', bar_close, md, uni, cache_out_path)
            cprint('[live]', f'opened={opened} at {bar_close.isoformat()}', fg='cyan', bold=(opened>0))
        else:
            now = datetime.now(timezone.utc)
            print_dot_once_per_bar(now, BAR_SECONDS)
        equity = get_account_equity(fetcher)
        try:
            write_equity(session_db_path, bot_id, now, {'equity': float(equity)})
        except Exception as e:
            _dbg('write_equity_failed', str(e))
        _time.sleep(args.poll_sec)
