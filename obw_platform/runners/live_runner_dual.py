
from __future__ import annotations

import copy, datetime as _dt, hashlib, importlib, json, logging, math, os, time, uuid, sqlite3
from pathlib import Path
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP, ROUND_UP
from typing import Optional, Tuple

from .common import *  # noqa: F401,F403
from .common import (
    _align_bar_close, _tf_to_seconds, cache_out_upsert, compute_feats,
    db_load_open_positions, db_mark_closed, db_upsert_open_position,
    ensure_orders_db, ensure_session_dbs, insert_order_row, load_positions,
    make_bot_id, read_hour_cache_row, save_positions, write_config_snapshot,
    write_equity,
)
try:
    from .exchange_trace_layer import ExchangeTraceProxy, ensure_exchange_trace_db
except Exception:
    ExchangeTraceProxy = None
    def ensure_exchange_trace_db(db_path: str) -> None:
        return None

try:
    from .live_debug_bundle import debug_event
except Exception:
    def debug_event(db_path: str, run_id: str, event_type: str, payload=None, level: str = 'INFO') -> None:
        return None

try:
    from .orderbook_slippage_probe_v1 import (
        ensure_microstructure_tables,
        safe_fetch_order_book,
        record_pretrade_snapshot,
        record_fill_observation,
    )
except Exception:
    def ensure_microstructure_tables(session_db_path: str) -> None:
        return None
    def safe_fetch_order_book(fetcher, symbol: str, limit: int = 10):
        return None
    def record_pretrade_snapshot(session_db_path: str, **kwargs):
        return {}
    def record_fill_observation(session_db_path: str, **kwargs):
        return {}

try:
    from .slippage_directional_model_v2 import (
        load_or_fit_model as _load_or_fit_slippage_model,
        save_model as _save_slippage_model,
        update_directional_slippage_model as _update_slippage_model,
        build_training_frame as _build_slippage_frame,
        make_feature_row as _make_slippage_feature_row,
    )
except Exception:
    def _load_or_fit_slippage_model(*args, **kwargs):
        return {}
    def _save_slippage_model(*args, **kwargs):
        return None
    def _update_slippage_model(model, observation):
        return model
    def _build_slippage_frame(*args, **kwargs):
        return None
    def _make_slippage_feature_row(*args, **kwargs):
        return {}

try:
    from .common import cprint as _cprint, dot as _dot
except Exception:
    _cprint, _dot = None, None

log = logging.getLogger(__name__)
DEBUG_OPEN = False
DEBUG_RUNTIME = False
DEBUG_EVENT_PAYLOAD = False
CONSOLE_ERROR_SUMMARY = True
CONSOLE_ERROR_DETAILS = False
ENTRY_LIMIT_USE_BOOK_PASSIVE = True
ENTRY_LIMIT_PASSIVE_TICKS = 0
ENTRY_LIMIT_TTL_SEC = float(os.getenv("ENTRY_LIMIT_TTL_SEC", os.getenv("ORDER_SYNC_WAIT_SEC", "2.0")))
ENTRY_LIMIT_FALLBACK_TO_MARKET = True
ENTRY_LIMIT_FALLBACK_ON_REJECT = True
ENTRY_LIMIT_FALLBACK_ON_TIMEOUT = True
ENTRY_LIMIT_CANCEL_CONFIRM_SEC = float(os.getenv("ENTRY_LIMIT_CANCEL_CONFIRM_SEC", "2.0"))
ORDER_SYNC_WAIT_SEC = float(os.getenv("ORDER_SYNC_WAIT_SEC", "2.0"))
ORDER_SYNC_POLL_SEC = float(os.getenv("ORDER_SYNC_POLL_SEC", "0.25"))
MAX_ENTRY_SLIP_BP = float(os.getenv("MAX_ENTRY_SLIP_BP", "0"))
MAX_EXIT_SLIP_BP = float(os.getenv("MAX_EXIT_SLIP_BP", "0"))
ENABLE_FALLBACK_TPSL = os.getenv("ENABLE_FALLBACK_TPSL", "0") == "1"
ENABLE_STARTUP_RECONCILE = os.getenv("ENABLE_STARTUP_RECONCILE", "0") == "1"
ENABLE_LOOP_RECONCILE = os.getenv("ENABLE_LOOP_RECONCILE", "0") == "1"


def cprint(*parts, fg: str = "", bold: bool = False, dim: bool = False, file=None, end="\n", flush=False):
    if _cprint:
        return _cprint(*parts, fg=fg, bold=bold, dim=dim, file=file, end=end, flush=flush)
    print(" ".join(str(p) for p in parts), file=file, end=end, flush=flush)


def dot():
    if _dot:
        return _dot()
    print('.', end='', flush=True)


def pos_key(sym: str, side: str) -> str:
    return f"{sym}|{side.upper()}"


def split_pos_key(key: str) -> Tuple[str, str]:
    if '|' in key:
        a, b = key.rsplit('|', 1)
        return a, b.upper()
    return key, 'LONG'


def _dbg(*parts):
    if DEBUG_OPEN:
        cprint('[dual dbg]', *parts, fg='yellow', dim=True)
def _debug_summary(event_type: str, payload):
    if isinstance(payload, str):
        return payload[:500]
    if not isinstance(payload, dict):
        return str(payload)[:500]
    keep = {}
    for k in ('symbol', 'side', 'order_type', 'reason', 'qty', 'requested_px', 'spread_bp', 'est_sweep_bp', 'imbalance', 'order_id', 'ready', 'bars_loaded'):
        if k in payload:
            keep[k] = payload.get(k)
    # Special-case large exchange payloads. Never print full raw order/orderbook unless DEBUG_EVENT_PAYLOAD is on.
    if 'exchange_response' in payload:
        ex = payload.get('exchange_response') or {}
        if isinstance(ex, dict):
            keep['exchange_error'] = ex.get('error') or ex.get('skip_reason') or ex.get('status')
    if 'order' in payload:
        od = payload.get('order') or {}
        if isinstance(od, dict):
            keep['order_status'] = od.get('status')
            keep['order_id'] = keep.get('order_id') or od.get('id')
    try:
        return json.dumps(keep or {'event': event_type}, ensure_ascii=False, sort_keys=True)
    except Exception:
        return str(keep)[:500]


def _emit_runtime_debug(session_db_path: str, run_id: str, event_type: str, payload=None, *, level: str = 'INFO', fg: str = 'yellow'):
    # Always persist full payload to session.sqlite. Console output is gated separately.
    try:
        debug_event(session_db_path, run_id, event_type, payload or {}, level=level)
    except Exception:
        pass

    should_print = bool(DEBUG_RUNTIME)
    if not should_print and str(level).upper() in {'ERROR', 'CRITICAL'}:
        should_print = bool(CONSOLE_ERROR_SUMMARY or CONSOLE_ERROR_DETAILS)
    if not should_print:
        return

    try:
        if DEBUG_EVENT_PAYLOAD or CONSOLE_ERROR_DETAILS:
            txt = payload if isinstance(payload, str) else json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
        else:
            txt = _debug_summary(event_type, payload)
    except Exception:
        txt = str(payload)[:500]

    try:
        cprint(f'[{event_type}]', txt, fg=fg)
    except Exception:
        pass



def _slippage_model_path(results_dir: str) -> str:
    return str(Path(results_dir) / 'slippage_live_model.json')

def _fit_bootstrap_slippage_model(session_db_path: str, results_dir: str, symbol: str):
    model_path = _slippage_model_path(results_dir)
    try:
        model = _load_or_fit_slippage_model(model_path, session_db_path, None, symbol=symbol)
        _save_slippage_model(model_path, model)
        return model
    except Exception:
        return {}

def _online_update_slippage_model(session_db_path: str, results_dir: str, symbol: str, strategy_side: str, order_action: str, qty: float, requested_px: float, fill_px: float, pre_snapshot: dict):
    try:
        model_path = _slippage_model_path(results_dir)
        model = _load_or_fit_slippage_model(model_path, session_db_path, None, symbol=symbol)
        obs = {
            'symbol': symbol,
            'strategy_side': strategy_side,
            'order_action': order_action,
            'qty': float(qty),
            'requested_price': float(requested_px),
            'fill_price': float(fill_px),
            'actual_adverse_bp': float(record_fill_observation.__globals__.get('adverse_slip_bp', lambda a,b,c,d:0.0)(requested_px, fill_px, strategy_side, order_action)) if False else 0.0,
        }
        obs['actual_adverse_bp'] = 0.0
        try:
            from .slippage_directional_model_v2 import adverse_slip_bp_from_fill
            obs['actual_adverse_bp'] = adverse_slip_bp_from_fill(requested_px, fill_px, strategy_side, order_action)
        except Exception:
            pass
        feat_row = _make_slippage_feature_row({
            'open': requested_px,
            'high': requested_px,
            'low': requested_px,
            'close': requested_px,
            'volume': 0.0,
            'quote_volume': requested_px * qty,
            'spread_bp': (pre_snapshot or {}).get('spread_bp') or 0.0,
            'est_sweep_slip_bp': (pre_snapshot or {}).get('est_sweep_slip_bp') or 0.0,
            'bid_depth_qty': (pre_snapshot or {}).get('bid_depth_qty') or 0.0,
            'ask_depth_qty': (pre_snapshot or {}).get('ask_depth_qty') or 0.0,
            'book_imbalance': (pre_snapshot or {}).get('book_imbalance') or 0.0,
        }, strategy_side, order_action, qty)
        obs.update(feat_row)
        model = _update_slippage_model(model, obs)
        _save_slippage_model(model_path, model)
        return model
    except Exception:
        return {}
def _auth_probe(fetcher, session_db_path: str, run_id: str):
    payload = {}
    try:
        rep = {}
        if hasattr(fetcher, 'debug_credentials_report'):
            try:
                rep = fetcher.debug_credentials_report() or {}
            except Exception:
                rep = {}
        payload['credentials'] = rep
        payload['credentials_present'] = bool(getattr(fetcher, 'credentials_present', False))
        try:
            bal = fetcher.ex.fetch_balance()
            sleep_ms(RATE_MS)
            payload['fetch_balance_ok'] = True
            payload['balance_keys'] = sorted(list((bal or {}).keys()))[:20] if isinstance(bal, dict) else []
            _emit_runtime_debug(session_db_path, run_id, 'auth_probe_ok', payload, level='INFO', fg='green')
            return True
        except Exception as e:
            payload['fetch_balance_ok'] = False
            payload['error'] = str(e)
            _emit_runtime_debug(session_db_path, run_id, 'auth_probe_fail', payload, level='ERROR', fg='red')
            return False
    except Exception as e:
        _emit_runtime_debug(session_db_path, run_id, 'auth_probe_fail', {'error': str(e)}, level='ERROR', fg='red')
        return False




def _normalize_close_reason(value, fallback: str = "") -> str:
    if value is None:
        return fallback
    try:
        text = str(value).strip()
    except Exception:
        text = f"{value}"
    return fallback if not text or text.lower() in {"", "nan", "none", "null", "nat"} else text


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


def _safe_float(value, default=None):
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _decimal_safe(value):
    try:
        if value is None:
            return None
        return Decimal(str(value))
    except Exception:
        return None


def _round_step(value, step, rounding):
    dv = _decimal_safe(value)
    if dv is None:
        return 0.0
    ds = _decimal_safe(step)
    if ds is None or ds <= 0:
        return float(dv)
    try:
        quant = (dv / ds).to_integral_value(rounding=rounding)
        return float(quant * ds)
    except Exception:
        return float(dv)


def _round_to_step(value, step):
    return _round_step(value, step, ROUND_HALF_UP)


def _floor_step(value, step):
    return _round_step(value, step, ROUND_DOWN)


def _step_from_precision(precision):
    if precision in (None, ""):
        return None
    try:
        if isinstance(precision, (int, float)) and (isinstance(precision, int) or float(int(precision)) == float(precision)):
            return float(Decimal("1") / (Decimal("10") ** int(precision))) if precision else None
        return float(precision)
    except Exception:
        try:
            return float(str(precision))
        except Exception:
            return None


def _exchange_id(fetcher_or_ex) -> str:
    ex = getattr(fetcher_or_ex, 'ex', fetcher_or_ex)
    try:
        return str(getattr(ex, 'id', '') or getattr(ex, 'name', '') or '').lower()
    except Exception:
        return ''


def _market_steps(fetcher_or_ex, symbol):
    ex = getattr(fetcher_or_ex, "ex", fetcher_or_ex)
    resolver = getattr(fetcher_or_ex, "resolve_symbol", None)
    ccxt_sym = symbol
    if callable(resolver):
        try:
            resolved = resolver(symbol)
            if resolved:
                ccxt_sym = resolved
        except Exception:
            pass
    try:
        market = ex.market(ccxt_sym)
    except Exception:
        market = None
    if not isinstance(market, dict):
        market = None
    if market is None:
        market = (getattr(ex, "markets", {}) or {}).get(ccxt_sym) or {}

    def _pick_float(paths):
        for path in paths:
            cur = market
            ok = True
            for key in path:
                if isinstance(cur, dict):
                    cur = cur.get(key)
                else:
                    ok = False
                    break
            if not ok:
                continue
            try:
                if cur in (None, ""):
                    continue
                val = float(cur)
                if math.isfinite(val) and val > 0:
                    return val
            except Exception:
                pass
        return None

    lot_step = _pick_float((("limits", "amount", "step"), ("info", "lotSizeFilter", "stepSize"), ("info", "qtyStep"), ("info", "stepSize"), ("info", "step")))
    if lot_step is None:
        lot_step = _step_from_precision((market.get("precision") or {}).get("amount"))
    min_qty = _pick_float((("limits", "amount", "min"), ("info", "lotSizeFilter", "minQty"), ("info", "minQty"))) or 0.0
    tick_size = _pick_float((("limits", "price", "step"), ("info", "tickSize"), ("info", "priceStep")))
    if tick_size is None:
        tick_size = _step_from_precision((market.get("precision") or {}).get("price"))
    return tick_size, lot_step, min_qty


def _extract_order_id(order_obj: Optional[dict]) -> str:
    if not order_obj:
        return ''
    return str(order_obj.get('id') or order_obj.get('orderId') or order_obj.get('clientOrderId') or '')

def _normalize_fetch_timeframe(fetcher, tf: str) -> str:
    tf = str(tf or '1m')
    if tf in {'30s', '0.5m', '15s'}:
        return '1m'
    ex = getattr(fetcher, 'ex', fetcher)
    allowed = getattr(ex, 'timeframes', None) or {}
    if isinstance(allowed, dict) and allowed:
        if tf in allowed:
            return tf
        if tf not in allowed and '1m' in allowed:
            return '1m'
    return tf


def _normalize_order_qty(fetcher, sym: str, qty: float, *, is_close: bool = False, max_qty: float = None) -> float:
    _tick, lot_step, min_qty = _market_steps(fetcher, sym)
    q = float(qty or 0.0)
    if max_qty is not None:
        q = min(q, float(max_qty))
    if q <= 0:
        return 0.0
    if is_close:
        if lot_step:
            q = _floor_step(q, lot_step)
        if max_qty is not None and q <= 0 and float(max_qty) > 0:
            q = float(max_qty)
        if min_qty and q < min_qty:
            if max_qty is not None and float(max_qty) <= min_qty + 1e-12:
                q = float(max_qty)
            else:
                q = float(min_qty)
                if max_qty is not None:
                    q = min(q, float(max_qty))
        return max(0.0, q)
    # opens / adds: round UP to exchange minimums
    if min_qty and q < min_qty:
        q = float(min_qty)
    if lot_step:
        q = _round_step(q, lot_step, ROUND_UP)
    return max(0.0, q)


def _choose_requested_price(fetcher, sym: str, fallback: float) -> float:
    t = None
    try:
        t = fetcher.ex.fetch_ticker(fetcher.resolve_symbol(sym) or sym)
        sleep_ms(RATE_MS)
    except Exception:
        pass
    px = None
    if isinstance(t, dict):
        px = _safe_float(t.get('last')) or _safe_float(t.get('close')) or _safe_float(t.get('bid')) or _safe_float(t.get('ask'))
    if px is None:
        px = _safe_float(fetcher.fetch_ticker_price(sym))
    return float(px or fallback or 0.0)


def _snapshot_limit_price(snapshot, fallback=None):
    try:
        if isinstance(snapshot, dict) and snapshot.get('next_level_price') not in (None, ''):
            return float(snapshot.get('next_level_price'))
    except Exception:
        pass
    try:
        if snapshot is not None and getattr(snapshot, 'next_level_price', None) not in (None, ''):
            return float(getattr(snapshot, 'next_level_price'))
    except Exception:
        pass
    try:
        return float(fallback) if fallback is not None else None
    except Exception:
        return None


def _safe_order_average(od, fallback=None):
    try:
        if isinstance(od, dict) and od.get('average') not in (None, ''):
            return float(od.get('average'))
    except Exception:
        pass
    try:
        info = od.get('info') if isinstance(od, dict) else None
        if isinstance(info, dict):
            for key in ('fill_price', 'avgPrice', 'price'):
                if info.get(key) not in (None, ''):
                    return float(info.get(key))
    except Exception:
        pass
    try:
        return float(fallback) if fallback is not None else None
    except Exception:
        return None


def _pending_entry_order_type(cfg: dict) -> str:
    v, _ = _cfg_pick(cfg or {}, ['live.dca_open_order_type', 'runner.dca_open_order_type', 'dca_open_order_type'], 'market')
    return str(v or 'market').lower().strip()



def _price_to_precision_float(fetcher: CCXTFetcher, sym: str, price: float) -> float:
    try:
        ccxt_sym = fetcher.resolve_symbol(sym) or sym
        return float(fetcher.ex.price_to_precision(ccxt_sym, float(price)))
    except Exception:
        return float(price)


def _infer_price_tick_from_snapshot(pre_snapshot: dict) -> float:
    prices = []
    try:
        raw = pre_snapshot.get('raw_json')
        if raw:
            book = json.loads(raw) if isinstance(raw, str) else raw
            for side in ('bids', 'asks'):
                for lvl in (book.get(side) or []):
                    if lvl and lvl[0] not in (None, ''):
                        prices.append(float(lvl[0]))
    except Exception:
        pass
    try:
        for k in ('best_bid', 'best_ask'):
            if pre_snapshot.get(k) not in (None, ''):
                prices.append(float(pre_snapshot.get(k)))
    except Exception:
        pass
    prices = sorted(set([p for p in prices if p > 0]))
    diffs = [round(prices[i+1] - prices[i], 12) for i in range(len(prices)-1) if prices[i+1] > prices[i]]
    diffs = [d for d in diffs if d > 0]
    if diffs:
        return float(min(diffs))
    # Fallback for low-priced futures like ENA. This is intentionally conservative.
    try:
        best = float(pre_snapshot.get('best_bid') or pre_snapshot.get('best_ask') or 0.0)
        if best < 1:
            return 0.0001
    except Exception:
        pass
    return 1e-6


def _sanitize_post_only_limit_price(fetcher: CCXTFetcher, sym: str, side: str, limit_price: float, pre_snapshot: dict, passive_ticks: int = 0, enabled: bool = True) -> float:
    """Make a post-only entry limit non-crossing using current order book.

    Maker, but aggressive:
    - LONG open / BUY must be strictly below best_ask.
      It may sit at best_bid or inside the spread.
    - SHORT open / SELL must be strictly above best_bid.
      It may sit at best_ask or inside the spread.

    Previous version forced BUY <= best_bid and SELL >= best_ask. That was too passive
    and killed fill rate. This function only moves the price when it would cross.
    """
    px = float(limit_price)
    if not enabled or not pre_snapshot:
        return _price_to_precision_float(fetcher, sym, px)

    try:
        best_bid = float(pre_snapshot.get('best_bid') or 0.0)
        best_ask = float(pre_snapshot.get('best_ask') or 0.0)
    except Exception:
        best_bid, best_ask = 0.0, 0.0

    if best_bid <= 0 or best_ask <= 0:
        return _price_to_precision_float(fetcher, sym, px)

    tick = _infer_price_tick_from_snapshot(pre_snapshot)
    ticks = max(0, int(passive_ticks or 0))

    if str(side).upper() == 'LONG':
        # Crossing BUY is >= best_ask. Move just below ask if needed.
        if px >= best_ask:
            px = best_ask - tick * max(1, ticks + 1)
    else:
        # Crossing SELL is <= best_bid. Move just above bid if needed.
        if px <= best_bid:
            px = best_bid + tick * max(1, ticks + 1)

    px = _price_to_precision_float(fetcher, sym, px)

    # Re-check after exchange precision rounding.
    if str(side).upper() == 'LONG' and px >= best_ask:
        px = _price_to_precision_float(fetcher, sym, best_ask - tick)
    elif str(side).upper() == 'SHORT' and px <= best_bid:
        px = _price_to_precision_float(fetcher, sym, best_bid + tick)
    return float(px)


def place_open_qty_limit(fetcher: CCXTFetcher, sym: str, side: str, qty: float, limit_price: float, position_mode: str, post_only: bool = True):
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    qty = _normalize_order_qty(fetcher, sym, qty, is_close=False)
    if qty <= 0:
        return {'ok': False, 'error': 'open_qty_zero_after_normalize', 'qty': qty}
    order_side = _order_open_side(side)
    params = {'positionSide': str(side).upper()} if str(position_mode or '').lower() == 'hedge' else {}
    if post_only:
        params['postOnly'] = True
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'limit', order_side, qty, float(limit_price), params); sleep_ms(RATE_MS)
        return {'ok': True, 'order': od, 'qty': qty, 'params': params, 'limit_price': float(limit_price)}
    except Exception as e:
        return {'ok': False, 'error': str(e), 'qty': qty, 'limit_price': float(limit_price)}



def _place_market_fallback_open(fetcher, strat, *, sym, side, requested_px, qty, bar_close, position_mode, session_db_path, run_id, bot_id, results_dir, positions, tp_price=None, sl_price=None, snapshot=None, pre_snapshot=None, fallback_reason='limit_no_fill', strategy_event='open'):
    _emit_runtime_debug(
        session_db_path, run_id, 'entry_limit_fallback_market',
        {
            'symbol': sym,
            'side': side,
            'qty': qty,
            'requested_px': requested_px,
            'reason': fallback_reason,
        },
        level='WARNING', fg='yellow'
    )
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'submitted', 1.0)
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'qty_requested', qty)
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'notional_requested', float(qty or 0.0) * float(requested_px or 0.0))

    res = place_open_qty(fetcher, sym, side, qty, position_mode)
    if not res.get('ok'):
        if snapshot is not None:
            _strategy_restore(strat, sym, snapshot)
        _strategy_rejected(strat, sym, 'open_market_fallback', res)
        fail_reason = str(res.get('error') or res.get('skip_reason') or 'market_fallback_open_fail')
        _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'rejected', 1.0)
        _write_exec_metrics(results_dir, session_db_path, run_id, event_type='execution_metrics_market_fallback_fail')
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='REJECTED', reason=fail_reason, run_id=run_id, extra={'order_type': 'market_fallback', 'fallback_reason': fallback_reason})
        _emit_runtime_debug(session_db_path, run_id, 'open_market_fallback_fail', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': fail_reason}, level='ERROR', fg='red')
        return False, res

    ex_order_id = _extract_order_id(res.get('order'))
    fill_px, fill_dt, od = _fetch_order_fill(fetcher, sym, ex_order_id, wait_sec=ORDER_SYNC_WAIT_SEC)
    if fill_px is None:
        if snapshot is not None:
            _strategy_restore(strat, sym, snapshot)
        reason = str(((od or {}).get('info') or {}).get('reason') or (od or {}).get('status') or 'market_fallback_timeout_no_fill')
        _strategy_rejected(strat, sym, 'open_market_fallback', {'reason': reason, 'order_id': ex_order_id})
        _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'canceled_no_fill', 1.0)
        _write_exec_metrics(results_dir, session_db_path, run_id, event_type='execution_metrics_market_fallback_nofill')
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='CANCELED', reason=reason, run_id=run_id, exchange_order_id=ex_order_id, extra={'order_type': 'market_fallback', 'fallback_reason': fallback_reason})
        _emit_runtime_debug(session_db_path, run_id, 'open_market_fallback_nofill', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': reason, 'order_id': ex_order_id}, level='ERROR', fg='red')
        return False, {'error': reason, 'order': od}

    record_fill_observation(
        session_db_path,
        run_id=run_id,
        bar_time_utc=bar_close.isoformat(),
        symbol=sym,
        strategy_side=side,
        order_action='OPEN',
        qty=qty,
        requested_price=requested_px,
        fill_price=fill_px,
        pre_snapshot=pre_snapshot,
    )
    _online_update_slippage_model(session_db_path, results_dir, sym, side, 'OPEN', qty, requested_px, fill_px, pre_snapshot)
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'filled', 1.0)
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'qty_filled', qty)
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'notional_filled', float(qty or 0.0) * float(fill_px or requested_px or 0.0))
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'adverse_slip_bp_sum', _adverse_slip_bp(side, requested_px, fill_px, is_close=False))
    _exec_metric_inc(sym, side, 'OPEN', 'market_fallback', 'signed_slip_bp_sum', _signed_slip_bp(side, requested_px, fill_px, is_close=False))
    _write_exec_metrics(results_dir, session_db_path, run_id, event_type='execution_metrics_market_fallback_fill')
    rec = {'symbol': sym, 'side': side, 'qty': qty, 'entry': requested_px, 'tp_price': tp_price, 'sl_price': sl_price, 'ts_open': bar_close.isoformat(), 'run_id': run_id, 'order_id': str(uuid.uuid4()), 'entry_order_type': 'market_fallback', 'fallback_reason': fallback_reason}
    return _finalize_open_success(fetcher, strat, rec, sym=sym, side=side, requested_px=requested_px, qty_requested=qty, ex_order_id=ex_order_id, bar_close=bar_close, fill_px=fill_px, fill_dt=fill_dt, session_db_path=session_db_path, bot_id=bot_id, results_dir=results_dir, positions=positions, run_id=run_id, position_mode=position_mode, strategy_event=strategy_event)


def _sync_pending_entry_orders(fetcher, pending_entries: dict, positions: dict, results_dir: str, session_db_path: str, bot_id: str, strat_long, strat_short, current_bar_iso: str, run_id: str = ''):
    for key, pend in list((pending_entries or {}).items()):
        sym = pend.get('symbol')
        side = str(pend.get('side', 'LONG')).upper()
        strat = strat_long if side == 'LONG' else strat_short
        order_id = pend.get('exchange_order_id')
        created_iso = str(pend.get('created_bar_iso') or '')
        if created_iso != str(current_bar_iso):
            try:
                fetcher.ex.cancel_order(order_id, fetcher.resolve_symbol(sym) or sym); sleep_ms(RATE_MS)
            except Exception:
                pass
            if float(pend.get('applied_filled_qty') or 0.0) <= 1e-12:
                _strategy_restore(strat, sym, pend.get('strategy_snapshot'))
                _strategy_rejected(strat, sym, 'dca_limit_timeout', {'order_id': order_id, 'limit_price': pend.get('limit_price')})
            pending_entries.pop(key, None)
            continue
        try:
            od = fetcher.ex.fetch_order(order_id, fetcher.resolve_symbol(sym) or sym); sleep_ms(RATE_MS)
        except Exception:
            od = pend.get('last_order') or {}
        status = str((od or {}).get('status') or pend.get('status') or '').lower()
        filled = float((od or {}).get('filled') or 0.0)
        already_applied = float(pend.get('applied_filled_qty') or 0.0)
        delta_filled = max(0.0, filled - already_applied)
        rec = positions.get(key)
        if delta_filled > 1e-12 and rec:
            fill_px = _safe_order_average(od, fallback=pend.get('limit_price'))
            old_qty = float(rec.get('qty', 0.0) or 0.0)
            old_entry = float(rec.get('entry', 0.0) or 0.0)
            new_qty = old_qty + delta_filled
            if fill_px is not None and old_qty > 0:
                rec['entry'] = ((old_qty * old_entry) + (delta_filled * float(fill_px))) / max(new_qty, 1e-12)
            elif fill_px is not None:
                rec['entry'] = float(fill_px)
            rec['qty'] = new_qty
            positions[key] = rec
            save_positions(results_dir, positions)
            try:
                db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN', 'entry_fill': fill_px, 'entry_fill_ts': _dt.datetime.now(_dt.timezone.utc).isoformat()})
            except Exception:
                pass
            _record_order(session_db_path, bar_time=current_bar_iso, symbol=sym, side=side, type_='OPEN', price=float(pend.get('limit_price') or 0.0), qty=delta_filled, status='FILLED', reason='dca_limit_fill', run_id=run_id, exchange_order_id=order_id, extra={'fill': fill_px, 'strategy_event': 'dca_limit_fill', 'filled_total': filled, 'applied_before': already_applied})
            _strategy_sync_after_fill(strat, sym, qty=new_qty, entry=float(rec.get('entry') or 0.0), fill_price=float(fill_px or rec.get('entry') or 0.0), delta_qty=delta_filled, event='dca_limit_fill', bar_time=current_bar_iso, extra={'exchange_order_id': order_id, 'limit_price': pend.get('limit_price'), 'filled_total': filled, 'applied_before': already_applied})
            pend['applied_filled_qty'] = already_applied + delta_filled
        if status == 'closed':
            pending_entries.pop(key, None)
            continue
        pend['last_order'] = od
        pend['status'] = status


def _sync_close_local_only(strat, key: str, rec: dict, positions: dict, results_dir: str, session_db_path: str, bot_id: str, run_id: str, reason: str):
    sym, side = split_pos_key(key)
    positions.pop(key, None)
    save_positions(results_dir, positions)
    try:
        db_mark_closed(session_db_path, bot_id, rec.get('order_id'), _dt.datetime.now(_dt.timezone.utc).isoformat(), exit_fill=None, exit_fill_ts=None, exit_slip_bp=None, exit_lag_sec=None, exit_mark_price=None, close_reason=reason)
    except Exception:
        pass
    _strategy_sync_after_fill(strat, sym, qty=0.0, entry=0.0, fill_price=None, delta_qty=float(rec.get('qty') or 0.0), event='sync_absent', extra={'reason': reason})
    _emit_runtime_debug(session_db_path, run_id, 'position_sync_absent', {'symbol': sym, 'side': side, 'reason': reason, 'local_qty': float(rec.get('qty') or 0.0)}, level='WARNING', fg='yellow')


def _reconcile_position_with_exchange(fetcher, strat, key: str, rec: dict, positions: dict, results_dir: str, session_db_path: str, bot_id: str, run_id: str):
    sym, side = split_pos_key(key)
    ex_pos = _fetch_exchange_position(fetcher, sym, side)
    if not ex_pos or float(ex_pos.get('qty') or 0.0) <= 1e-12:
        _sync_close_local_only(strat, key, rec, positions, results_dir, session_db_path, bot_id, run_id, 'exchange_sync_absent')
        return None
    ex_qty = float(ex_pos.get('qty') or 0.0)
    ex_entry = float(ex_pos.get('entry') or 0.0)
    loc_qty = float(rec.get('qty') or 0.0)
    loc_entry = float(rec.get('entry') or 0.0)
    qty_mismatch = abs(ex_qty - loc_qty) > max(1e-9, 0.01 * max(ex_qty, loc_qty, 1e-9))
    entry_mismatch = abs(ex_entry - loc_entry) > max(1e-9, 5e-4 * max(ex_entry, loc_entry, 1e-9))
    if qty_mismatch or entry_mismatch:
        rec['qty'] = ex_qty
        rec['entry'] = ex_entry
        positions[key] = rec
        save_positions(results_dir, positions)
        try:
            db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN'})
        except Exception:
            pass
        _strategy_sync_after_fill(strat, sym, qty=ex_qty, entry=ex_entry, fill_price=ex_entry, delta_qty=0.0, event='sync_reconcile', extra={'reason': 'exchange_position_reconcile'})
        _emit_runtime_debug(session_db_path, run_id, 'position_sync_update', {'symbol': sym, 'side': side, 'local_qty': loc_qty, 'exchange_qty': ex_qty, 'local_entry': loc_entry, 'exchange_entry': ex_entry}, level='INFO', fg='cyan')
    return rec


def _order_close_side(entry_side: str) -> str:
    return 'sell' if str(entry_side).upper() == 'LONG' else 'buy'


def _order_open_side(entry_side: str) -> str:
    return 'buy' if str(entry_side).upper() == 'LONG' else 'sell'


def _adverse_slip_bp(entry_side: str, requested_px: float, fill_px: float, *, is_close: bool = False) -> float:
    req = max(float(requested_px or 0.0), 1e-12)
    fill = float(fill_px or 0.0)
    side = str(entry_side).upper()
    if not is_close:
        return max(0.0, (fill / req - 1.0) * 10000.0) if side == 'LONG' else max(0.0, (req / max(fill, 1e-12) - 1.0) * 10000.0)
    return max(0.0, (req / max(fill, 1e-12) - 1.0) * 10000.0) if side == 'LONG' else max(0.0, (fill / req - 1.0) * 10000.0)


def _signed_slip_bp(entry_side: str, requested_px: float, fill_px: float, *, is_close: bool = False) -> float:
    req = max(float(requested_px or 0.0), 1e-12)
    fill = float(fill_px or 0.0)
    side = str(entry_side).upper()
    signed = (fill / req - 1.0) * 10000.0
    if (side == 'SHORT' and not is_close) or (side == 'LONG' and is_close):
        signed *= -1.0
    return signed


def _strategy_snapshot(strat, sym: str):
    if hasattr(strat, 'export_state_snapshot'):
        try:
            return strat.export_state_snapshot(sym)
        except Exception:
            pass
    try:
        return copy.deepcopy(getattr(strat, '_states', {}).get(sym))
    except Exception:
        return None


def _strategy_restore(strat, sym: str, snapshot):
    if hasattr(strat, 'restore_state_snapshot'):
        try:
            strat.restore_state_snapshot(sym, snapshot)
            return
        except Exception:
            pass
    try:
        states = getattr(strat, '_states', None)
        if snapshot is None:
            states.pop(sym, None)
        else:
            states[sym] = copy.deepcopy(snapshot)
    except Exception:
        pass


def _strategy_rejected(strat, sym: str, event: str, details=None):
    fn = getattr(strat, 'on_order_rejected', None)
    if callable(fn):
        try:
            fn(sym, event=event, details=details)
        except Exception:
            pass



def _json_safe_value(value, max_items: int = 50):
    """Convert dataclass/state/exchange-ish values into bounded JSON-safe objects."""
    try:
        if value is None or isinstance(value, (str, int, float, bool)):
            return value
        if isinstance(value, Decimal):
            return float(value)
        if isinstance(value, (_dt.datetime, _dt.date)):
            return value.isoformat()
        if isinstance(value, dict):
            out = {}
            for i, (k, v) in enumerate(value.items()):
                if i >= max_items:
                    out['_truncated'] = True
                    break
                out[str(k)] = _json_safe_value(v, max_items=max_items)
            return out
        if isinstance(value, (list, tuple, set, deque)):
            arr = list(value)
            out = [_json_safe_value(v, max_items=max_items) for v in arr[:max_items]]
            if len(arr) > max_items:
                out.append({'_truncated_count': len(arr) - max_items})
            return out
        if hasattr(value, '__dict__'):
            return _json_safe_value(vars(value), max_items=max_items)
        return str(value)
    except Exception:
        return str(value)


def _num_or_none(x):
    try:
        if x is None:
            return None
        v = float(x)
        if not math.isfinite(v):
            return None
        return v
    except Exception:
        return None


def _state_get(snapshot, name, default=None):
    if snapshot is None:
        return default
    if isinstance(snapshot, dict):
        return snapshot.get(name, default)
    return getattr(snapshot, name, default)


def _strategy_state_brief(snapshot) -> dict:
    """Small deterministic state digest for parity/debug, not a full pickle."""
    if snapshot is None:
        return {}
    lots_raw = _state_get(snapshot, 'lots', []) or []
    lots = []
    total_qty = 0.0
    total_notional = 0.0
    try:
        for lot in list(lots_raw):
            if isinstance(lot, dict):
                q = _num_or_none(lot.get('qty') or lot.get('q'))
                px = _num_or_none(lot.get('price') or lot.get('entry') or lot.get('px'))
            else:
                q = _num_or_none(lot[0]) if len(lot) > 0 else None
                px = _num_or_none(lot[1]) if len(lot) > 1 else None
            if q is not None:
                total_qty += q
            if q is not None and px is not None:
                total_notional += q * px
            lots.append({'qty': q, 'price': px})
    except Exception:
        lots = []
        total_qty = _num_or_none(_state_get(snapshot, 'pos_size', 0.0)) or 0.0
        total_notional = _num_or_none(_state_get(snapshot, 'pos_value_usdt', 0.0)) or 0.0

    keys = [
        'pos_size', 'pos_value_usdt', 'avg_price', 'num_fills', 'last_fill_price',
        'next_level_price', 'trailing_active', 'trailing_ref', 'reset_pending',
        'cycle_base_qty_coin', 'pending_new_entry', 'cycle_start_ts', 'last_fill_ts',
    ]
    out = {}
    for k in keys:
        v = _state_get(snapshot, k, None)
        if isinstance(v, bool):
            out[k] = bool(v)
        elif isinstance(v, (int, float, Decimal)) or v is None:
            out[k] = _num_or_none(v)
        else:
            out[k] = _json_safe_value(v, max_items=10)
    out['lots_count'] = len(lots)
    out['lots_total_qty'] = total_qty
    out['lots_total_notional'] = total_notional
    out['lots_tail'] = lots[-8:]
    out['lots_lifo_top'] = lots[-1] if lots else None
    return out


def _strategy_state_diff(before: dict, after: dict) -> dict:
    before = before or {}
    after = after or {}
    diff = {}
    for k in sorted(set(before.keys()) | set(after.keys())):
        b = before.get(k)
        a = after.get(k)
        if b != a:
            diff[k] = {'before': b, 'after': a}
    return diff


def ensure_strategy_state_events_db(db_path: str) -> None:
    if not db_path:
        return
    con = sqlite3.connect(db_path)
    try:
        con.execute("""
        CREATE TABLE IF NOT EXISTS strategy_state_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts_utc TEXT NOT NULL,
            run_id TEXT,
            bar_time_utc TEXT,
            symbol TEXT,
            side TEXT,
            strategy_event TEXT,
            order_action TEXT,
            qty REAL,
            entry REAL,
            fill_price REAL,
            delta_qty REAL,
            state_before_json TEXT,
            state_after_json TEXT,
            state_diff_json TEXT,
            extra_json TEXT
        )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_strategy_state_events_run_time ON strategy_state_events(run_id, bar_time_utc, symbol, side)")
        con.commit()
    finally:
        con.close()


def _record_strategy_state_transition(*, strat, sym: str, event: str, qty: float, entry: float, fill_price=None, delta_qty=None, bar_time=None, extra=None, before_snapshot=None, after_snapshot=None):
    db_path = globals().get('LIVE_SESSION_DB_PATH') or ''
    run_id = globals().get('LIVE_RUN_ID') or ''
    results_dir = globals().get('LIVE_RESULTS_DIR') or ''
    if not db_path and not results_dir:
        return None
    side = str(getattr(strat, 'SIDE', '') or '').upper()
    before = _strategy_state_brief(before_snapshot)
    after = _strategy_state_brief(after_snapshot)
    diff = _strategy_state_diff(before, after)
    payload = {
        'ts_utc': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'run_id': run_id,
        'bar_time_utc': bar_time.isoformat() if hasattr(bar_time, 'isoformat') else (str(bar_time) if bar_time else ''),
        'symbol': sym,
        'side': side,
        'strategy_event': str(event or ''),
        'order_action': 'OPEN' if str(event or '').lower() in {'first', 'open', 'dca', 'dca_limit_fill', 'sync_reconcile'} else ('PARTIAL' if str(event or '').lower() == 'partial' else 'CLOSE'),
        'qty': _num_or_none(qty),
        'entry': _num_or_none(entry),
        'fill_price': _num_or_none(fill_price),
        'delta_qty': _num_or_none(delta_qty),
        'state_before': before,
        'state_after': after,
        'state_diff': diff,
        'extra': _json_safe_value(extra or {}, max_items=30),
    }
    try:
        if db_path:
            ensure_strategy_state_events_db(db_path)
            con = sqlite3.connect(db_path)
            try:
                con.execute(
                    """INSERT INTO strategy_state_events
                    (ts_utc, run_id, bar_time_utc, symbol, side, strategy_event, order_action, qty, entry, fill_price, delta_qty,
                     state_before_json, state_after_json, state_diff_json, extra_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        payload['ts_utc'], payload['run_id'], payload['bar_time_utc'], payload['symbol'], payload['side'],
                        payload['strategy_event'], payload['order_action'], payload['qty'], payload['entry'], payload['fill_price'], payload['delta_qty'],
                        json.dumps(before, ensure_ascii=False, sort_keys=True),
                        json.dumps(after, ensure_ascii=False, sort_keys=True),
                        json.dumps(diff, ensure_ascii=False, sort_keys=True),
                        json.dumps(payload['extra'], ensure_ascii=False, sort_keys=True),
                    )
                )
                con.commit()
            finally:
                con.close()
            try:
                debug_event(db_path, run_id, 'strategy_state_transition', payload, level='INFO')
            except Exception:
                pass
        if results_dir:
            try:
                with open(Path(results_dir) / 'strategy_state_events.jsonl', 'a', encoding='utf-8') as f:
                    f.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + '\n')
            except Exception:
                pass
    except Exception as e:
        try:
            if db_path:
                debug_event(db_path, run_id, 'strategy_state_transition_error', {'symbol': sym, 'side': side, 'event': event, 'error': str(e)}, level='ERROR')
        except Exception:
            pass
    return payload

def _strategy_sync_after_fill(strat, sym: str, *, qty: float, entry: float, fill_price=None, delta_qty=None, event: str = '', bar_time=None, extra=None):
    before_snapshot = _strategy_snapshot(strat, sym)
    fn = getattr(strat, 'sync_after_external_fill', None)
    used_native_sync = False
    if callable(fn):
        try:
            fn(sym, qty=qty, entry=entry, fill_price=fill_price, delta_qty=delta_qty, event=event)
            used_native_sync = True
        except Exception as exc:
            try:
                debug_event(globals().get('LIVE_SESSION_DB_PATH') or '', globals().get('LIVE_RUN_ID') or '', 'strategy_sync_after_fill_error', {'symbol': sym, 'event': event, 'error': str(exc)}, level='ERROR')
            except Exception:
                pass
    if not used_native_sync:
        try:
            st = strat._get_state(sym)
            st.pos_size = float(qty)
            st.avg_price = float(entry) if qty > 0 else None
            if hasattr(st, 'pos_value_usdt'):
                st.pos_value_usdt = float(qty) * float(entry) if qty > 0 else 0.0
        except Exception:
            pass
    after_snapshot = _strategy_snapshot(strat, sym)
    _record_strategy_state_transition(strat=strat, sym=sym, event=event, qty=qty, entry=entry, fill_price=fill_price, delta_qty=delta_qty, bar_time=bar_time, extra=extra, before_snapshot=before_snapshot, after_snapshot=after_snapshot)


class _PosLike:
    def __init__(self, rec: dict):
        self.qty = float(rec.get('qty', 0.0) or 0.0)
        self.entry = float(rec.get('entry', 0.0) or 0.0)
        self.side = str(rec.get('side', 'LONG')).upper()
        self.tp = rec.get('tp_price')
        self.sl = rec.get('sl_price')



LIVE_ARTIFACT_MANIFEST = {}
LIVE_SESSION_DB_PATH = ''
LIVE_RUN_ID = ''
LIVE_RESULTS_DIR = ''



def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _module_file_for_class_path(class_path: str) -> Optional[str]:
    try:
        mod_path, _cls_name = str(class_path).rsplit('.', 1)
        spec = importlib.util.find_spec(mod_path)
        origin = getattr(spec, 'origin', None)
        if origin and origin != 'built-in':
            return os.path.abspath(origin)
    except Exception:
        return None
    return None


def _build_live_artifact_manifest(cfg: dict) -> dict:
    """Build immutable artifact metadata for live reports.

    Stored in:
      - live_run_manifest.json
      - live_execution_metrics.json under artifacts
      - debug_events as live_run_manifest

    The config path/hash is injected by bt_live_paper_runner_separated_universe_4.py
    under cfg['_run_artifacts']['config'].
    """
    manifest = dict(cfg.get('_run_artifacts') or {})
    strategies = {}
    for role, key in (('long', 'strategy_class_long'), ('short', 'strategy_class_short')):
        class_path = str(cfg.get(key) or '')
        item = {'class_path': class_path}
        fpath = _module_file_for_class_path(class_path) if class_path else None
        if fpath and os.path.exists(fpath):
            item.update({
                'path': fpath,
                'basename': os.path.basename(fpath),
                'sha256': _sha256_file(fpath),
                'size_bytes': int(os.path.getsize(fpath)),
                'mtime_utc': _dt.datetime.fromtimestamp(os.path.getmtime(fpath), tz=_dt.timezone.utc).isoformat(),
            })
        else:
            item['error'] = 'strategy module file not found'
        strategies[role] = item
    manifest['strategies'] = strategies
    manifest['generated_at_utc'] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    return manifest


def _write_live_run_manifest(results_dir: str, session_db_path: str, run_id: str, manifest: dict) -> None:
    payload = {
        'run_id': run_id,
        'ts_utc': _dt.datetime.now(_dt.timezone.utc).isoformat(),
        'artifacts': manifest,
    }
    try:
        Path(results_dir).mkdir(parents=True, exist_ok=True)
        (Path(results_dir) / 'live_run_manifest.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
            encoding='utf-8',
        )
    except Exception:
        pass
    try:
        debug_event(session_db_path, run_id, 'live_run_manifest', payload, level='INFO')
    except Exception:
        pass



def _load_strategy_pair(cfg: dict):
    def _load(path_cls: str):
        mod_path, cls_name = path_cls.rsplit('.', 1)
        mod = importlib.import_module(mod_path)
        cls = getattr(mod, cls_name)
        return cls(cfg)
    return _load(cfg['strategy_class_long']), _load(cfg['strategy_class_short'])


def _resolve_runner_cfg(cfg: dict):
    tf_v, _ = _cfg_pick(cfg, ['timeframe', 'runner.timeframe', 'live.timeframe'], '30s')
    top_n_v, _ = _cfg_pick(cfg, ['top_n', 'runner.top_n', 'live.top_n'], 1)
    position_mode_v, _ = _cfg_pick(cfg, ['position_mode', 'runner.position_mode', 'live.position_mode'], 'hedge')
    notional_long_v, _ = _cfg_pick(cfg, ['portfolio.position_notional_long', 'portfolio.position_notional', 'position_notional_long'], 2.0)
    notional_short_v, _ = _cfg_pick(cfg, ['portfolio.position_notional_short', 'portfolio.position_notional', 'position_notional_short'], 20.0)
    dca_open_order_type_v, _ = _cfg_pick(cfg, ['live.dca_open_order_type', 'runner.dca_open_order_type', 'dca_open_order_type'], 'market')
    return {'timeframe': str(tf_v), 'top_n': int(top_n_v), 'position_mode': str(position_mode_v), 'notional_long': float(notional_long_v), 'notional_short': float(notional_short_v), 'dca_open_order_type': str(dca_open_order_type_v or 'market').lower().strip()}


def _fetch_order_fill(fetcher: CCXTFetcher, sym: str, order_id: str, wait_sec: float = ORDER_SYNC_WAIT_SEC, poll_sec: float = ORDER_SYNC_POLL_SEC):
    if not order_id:
        return None, None, None
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    deadline = time.time() + max(float(wait_sec), 0.0)
    last = None
    while True:
        try:
            od = fetcher.ex.fetch_order(order_id, ccxt_sym)
            sleep_ms(RATE_MS)
        except Exception as e:
            _dbg('fetch_order', sym, order_id, str(e))
            od = None
        if od:
            last = od
            status = str(od.get('status') or '').lower()
            avg = od.get('average') or od.get('price') or od.get('avgPrice') or od.get('avg_price')
            ts = od.get('timestamp')
            if ts is None:
                dt_str = od.get('datetime')
                if dt_str:
                    try:
                        ts = int(_dt.datetime.fromisoformat(dt_str.replace('Z', '+00:00')).timestamp() * 1000)
                    except Exception:
                        ts = None
            fill_dt = _dt.datetime.fromtimestamp(int(ts) / 1000.0, tz=_dt.timezone.utc) if ts is not None else None
            if status in {'closed', 'filled'} and avg is not None:
                return float(avg), fill_dt, od
            if status in {'canceled', 'rejected', 'expired'}:
                return None, fill_dt, od
        if time.time() >= deadline:
            return None, None, last
        time.sleep(max(float(poll_sec), 0.05))



def _cancel_order_and_fetch_final(fetcher: CCXTFetcher, sym: str, order_id: str, confirm_sec: float = 2.0, poll_sec: float = ORDER_SYNC_POLL_SEC):
    """Cancel an order and confirm final state.

    Returns: (state, fill_px, fill_dt, order)
      state in {'filled', 'canceled', 'rejected', 'expired', 'open', 'unknown'}

    This prevents the dangerous sequence:
      limit timeout -> cancel request -> immediately market fallback -> old limit fills later
    """
    if not order_id:
        return 'unknown', None, None, None
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    try:
        fetcher.ex.cancel_order(order_id, ccxt_sym)
        sleep_ms(RATE_MS)
    except Exception:
        pass

    deadline = time.time() + max(float(confirm_sec or 0.0), 0.0)
    last = None
    while True:
        try:
            od = fetcher.ex.fetch_order(order_id, ccxt_sym)
            sleep_ms(RATE_MS)
        except Exception:
            od = None
        if od:
            last = od
            status = str(od.get('status') or '').lower()
            avg = od.get('average') or od.get('avgPrice') or od.get('avg_price')
            # Do not use od.price as fill average if filled=0.
            filled = float(od.get('filled') or ((od.get('info') or {}).get('executedQty') or 0.0) or 0.0)
            ts = od.get('timestamp')
            if ts is None:
                dt_str = od.get('datetime')
                if dt_str:
                    try:
                        ts = int(_dt.datetime.fromisoformat(dt_str.replace('Z', '+00:00')).timestamp() * 1000)
                    except Exception:
                        ts = None
            fill_dt = _dt.datetime.fromtimestamp(int(ts) / 1000.0, tz=_dt.timezone.utc) if ts is not None else None
            if status in {'closed', 'filled'} and filled > 0:
                if avg is None:
                    avg = od.get('price') or (od.get('info') or {}).get('avgPrice')
                return 'filled', float(avg), fill_dt, od
            if status in {'canceled', 'cancelled'}:
                return 'canceled', None, fill_dt, od
            if status in {'rejected', 'expired'}:
                return status, None, fill_dt, od
            if status in {'open', 'new'}:
                pass
        if time.time() >= deadline:
            return 'open' if last else 'unknown', None, None, last
        time.sleep(max(float(poll_sec), 0.05))


def _normalize_position_side(raw) -> str:
    text = str(raw or '').strip().upper()
    if not text:
        return ''
    if text.startswith('LONG') or text in {'BUY', 'BID'}:
        return 'LONG'
    if text.startswith('SHORT') or text in {'SELL', 'ASK'}:
        return 'SHORT'
    if text == 'BOTH':
        return 'BOTH'
    return text

def _extract_signed_position_qty(p: dict):
    info = p.get('info', {}) if isinstance(p.get('info'), dict) else {}
    for source in (p, info):
        for k in ('contracts', 'positionAmt', 'positionAmount', 'position', 'size', 'availableAmt', 'holding'):
            v = _safe_float(source.get(k))
            if v is not None:
                return float(v)
    return 0.0

def _extract_position_side(p: dict) -> str:
    info = p.get('info', {}) if isinstance(p.get('info'), dict) else {}
    for raw in (
        info.get('positionSide'),
        p.get('positionSide'),
        info.get('posSide'),
        p.get('posSide'),
        p.get('side'),
        info.get('side'),
        info.get('position_side'),
    ):
        side = _normalize_position_side(raw)
        if side in {'LONG', 'SHORT', 'BOTH'}:
            return side
    return ''

def _fetch_exchange_position(fetcher: CCXTFetcher, sym: str, side: str):
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    want_side = str(side).upper()
    try:
        pos_list = fetcher.ex.fetch_positions([ccxt_sym])
        sleep_ms(RATE_MS)
    except Exception:
        try:
            pos_list = fetcher.ex.fetch_positions()
            sleep_ms(RATE_MS)
        except Exception:
            return None

    rows = []
    has_negative_signed_qty = False
    for p in (pos_list or []):
        try:
            got_sym = fetcher.resolve_symbol(p.get('symbol')) or p.get('symbol')
            if got_sym != ccxt_sym:
                continue
            signed_qty = _extract_signed_position_qty(p)
            qty = abs(float(signed_qty or 0.0))
            if qty <= 0:
                continue
            ps = _extract_position_side(p)
            if signed_qty < 0:
                has_negative_signed_qty = True
            entry = _safe_float(p.get('entryPrice')) or _safe_float(p.get('entry')) or _safe_float((p.get('info') or {}).get('avgPrice')) or _safe_float((p.get('info') or {}).get('entryPrice')) or 0.0
            rows.append({'symbol': ccxt_sym, 'raw_side': ps, 'signed_qty': float(signed_qty or 0.0), 'qty': qty, 'entry': float(entry or 0.0), '_raw': p})
        except Exception:
            continue

    exact = []
    for r in rows:
        ps = r['raw_side']
        inferred = ''
        if ps in {'LONG', 'SHORT'}:
            inferred = ps
        elif ps == 'BOTH':
            inferred = ''
        else:
            # Conservative inference:
            # - negative signed qty is safely SHORT
            # - positive signed qty is LONG only when the exchange returned a mixed signed set
            #   (typical hedge-style pair with one positive and one negative leg)
            if r['signed_qty'] < 0:
                inferred = 'SHORT'
            elif r['signed_qty'] > 0 and has_negative_signed_qty:
                inferred = 'LONG'
        if inferred == want_side:
            exact.append({'symbol': ccxt_sym, 'side': inferred, 'qty': r['qty'], 'entry': r['entry']})

    if exact:
        exact.sort(key=lambda r: r['qty'], reverse=True)
        return exact[0]
    return None


def _calc_equity_snapshot(fetcher: CCXTFetcher) -> dict:
    try:
        bal = fetcher.ex.fetch_balance(); sleep_ms(RATE_MS)
    except Exception:
        return {'equity': 0.0, 'cash': 0.0, 'position_value': 0.0, 'realized_pnl_cum': 0.0, 'unrealized_pnl': 0.0}
    equity = cash = 0.0
    if isinstance(bal, dict):
        total = bal.get('total') or {}
        free = bal.get('free') or {}
        equity = _safe_float(total.get('USDT')) or _safe_float(bal.get('equity')) or _safe_float((bal.get('info') or {}).get('equity')) or 0.0
        cash = _safe_float(free.get('USDT')) or equity
    position_value = unrealized = 0.0
    try:
        for p in fetcher.ex.fetch_positions() or []:
            qty = abs(float(p.get('contracts') or 0.0))
            entry = _safe_float(p.get('entryPrice')) or _safe_float(p.get('entry')) or 0.0
            mark = fetcher.fetch_mark_price(p.get('symbol') or '') or fetcher.fetch_ticker_price(p.get('symbol') or '') or entry
            position_value += qty * max(float(mark or 0.0), 0.0)
            unrealized += _safe_float(p.get('unrealizedPnl')) or 0.0
    except Exception:
        pass
    return {'equity': float(equity), 'cash': float(cash), 'position_value': float(position_value), 'realized_pnl_cum': 0.0, 'unrealized_pnl': float(unrealized)}



_EXEC_METRICS = {}

def _exec_metrics_key(symbol: str, side: str, type_: str, order_type: str = 'market') -> str:
    return f"{symbol}|{side}|{type_}|{order_type}".upper()

def _exec_metric_inc(symbol: str, side: str, type_: str, order_type: str, field: str, amount: float = 1.0):
    key = _exec_metrics_key(symbol, side, type_, order_type)
    rec = _EXEC_METRICS.setdefault(key, {
        'symbol': symbol,
        'side': side,
        'type': type_,
        'order_type': order_type,
        'signals': 0,
        'submitted': 0,
        'filled': 0,
        'rejected': 0,
        'canceled_no_fill': 0,
        'reverted': 0,
        'qty_requested': 0.0,
        'qty_filled': 0.0,
        'notional_requested': 0.0,
        'notional_filled': 0.0,
        'adverse_slip_bp_sum': 0.0,
        'signed_slip_bp_sum': 0.0,
    })
    rec[field] = float(rec.get(field, 0.0) or 0.0) + float(amount or 0.0)
    return rec

def _exec_metric_snapshot():
    out = []
    totals = {
        'signals': 0.0, 'submitted': 0.0, 'filled': 0.0, 'rejected': 0.0,
        'canceled_no_fill': 0.0, 'reverted': 0.0,
        'qty_requested': 0.0, 'qty_filled': 0.0,
        'notional_requested': 0.0, 'notional_filled': 0.0,
    }
    for rec in _EXEC_METRICS.values():
        r = dict(rec)
        submitted = float(r.get('submitted', 0.0) or 0.0)
        signals = float(r.get('signals', 0.0) or 0.0)
        filled = float(r.get('filled', 0.0) or 0.0)
        r['submit_rate'] = submitted / signals if signals > 0 else None
        r['fill_rate'] = filled / submitted if submitted > 0 else None
        r['signal_to_fill_rate'] = filled / signals if signals > 0 else None
        r['avg_adverse_slip_bp'] = float(r.get('adverse_slip_bp_sum', 0.0) or 0.0) / filled if filled > 0 else None
        r['avg_signed_slip_bp'] = float(r.get('signed_slip_bp_sum', 0.0) or 0.0) / filled if filled > 0 else None
        out.append(r)
        for k in totals:
            totals[k] += float(r.get(k, 0.0) or 0.0)
    totals['fill_rate'] = totals['filled'] / totals['submitted'] if totals['submitted'] > 0 else None
    totals['signal_to_fill_rate'] = totals['filled'] / totals['signals'] if totals['signals'] > 0 else None
    return {'totals': totals, 'by_key': sorted(out, key=lambda x: (x.get('symbol',''), x.get('side',''), x.get('type',''), x.get('order_type','')))}

def _write_exec_metrics(results_dir: str, session_db_path: str, run_id: str, event_type: str = 'execution_metrics'):
    snap = _exec_metric_snapshot()
    snap['run_id'] = run_id
    snap['ts_utc'] = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if LIVE_ARTIFACT_MANIFEST:
        snap['artifacts'] = LIVE_ARTIFACT_MANIFEST
    try:
        Path(results_dir).mkdir(parents=True, exist_ok=True)
        path = Path(results_dir) / 'live_execution_metrics.json'
        path.write_text(json.dumps(snap, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
    except Exception:
        pass
    try:
        debug_event(session_db_path, run_id, event_type, snap, level='INFO')
    except Exception:
        pass
    return snap


def _calibration_cfg(cfg: dict) -> dict:
    runner = cfg.get('runner') or {}
    cal = dict(runner.get('slippage_calibration') or {})
    cal.setdefault('enabled', False)
    cal.setdefault('min_obs', 30)
    cal.setdefault('window_obs', 500)
    cal.setdefault('recency_hours', 72)
    cal.setdefault('update_every_bars', 1)
    cal.setdefault('output_json', 'live_slippage_calibration.json')
    cal.setdefault('suggested_yaml', 'backtest_slippage_suggestion.yaml')
    vr = dict(cal.get('volume_regime') or {})
    vr.setdefault('enabled', True)
    vr.setdefault('metric', 'quote_volume')
    vr.setdefault('recent_bars', 60)
    vr.setdefault('baseline_bars', 360)
    vr.setdefault('shift_ratio', 2.0)
    cal['volume_regime'] = vr
    return cal

def _median(vals):
    vals = sorted([float(v) for v in vals if v is not None and math.isfinite(float(v))])
    n = len(vals)
    if n <= 0:
        return None
    mid = n // 2
    return vals[mid] if n % 2 else 0.5 * (vals[mid-1] + vals[mid])

def _read_recent_slippage_observations(session_db_path: str, cal: dict):
    try:
        con = sqlite3.connect(session_db_path)
        q = """
        SELECT id, ts_utc, bar_time_utc, symbol, strategy_side, order_action, order_direction,
               qty, requested_price, fill_price, actual_adverse_bp, snapshot_est_sweep_bp,
               snapshot_spread_bp, best_bid, best_ask, mid_price
        FROM slippage_observations
        ORDER BY id DESC
        LIMIT ?
        """
        rows = [dict(zip([c[0] for c in cur.description], row)) for cur in [con.execute(q, (int(cal.get('window_obs', 500)),))] for row in cur.fetchall()]
        con.close()
    except Exception:
        return []
    # newest first -> oldest first for fitting/reporting
    rows = list(reversed(rows))
    if cal.get('recency_hours'):
        try:
            cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=float(cal.get('recency_hours')))
            keep = []
            for r in rows:
                ts = str(r.get('ts_utc') or '').replace('Z', '+00:00')
                try:
                    dt = _dt.datetime.fromisoformat(ts)
                    if dt.tzinfo is None: dt = dt.replace(tzinfo=_dt.timezone.utc)
                    if dt >= cutoff: keep.append(r)
                except Exception:
                    keep.append(r)
            rows = keep
        except Exception:
            pass
    return rows

def _volume_regime_status(cache_db_path: str, cal: dict) -> dict:
    vr = cal.get('volume_regime') or {}
    if not vr.get('enabled', True) or not cache_db_path:
        return {'enabled': False, 'interrupted': False}
    metric = str(vr.get('metric', 'quote_volume'))
    recent_n = int(vr.get('recent_bars', 60) or 60)
    base_n = int(vr.get('baseline_bars', 360) or 360)
    shift = float(vr.get('shift_ratio', 2.0) or 2.0)
    try:
        con = sqlite3.connect(cache_db_path)
        q = f"SELECT datetime_utc, {metric} FROM price_indicators WHERE {metric} IS NOT NULL ORDER BY datetime_utc DESC LIMIT ?"
        rows = con.execute(q, (max(recent_n + base_n, base_n),)).fetchall()
        con.close()
    except Exception as e:
        return {'enabled': True, 'interrupted': False, 'error': str(e), 'metric': metric}
    vals = [float(r[1]) for r in rows if r and r[1] is not None and math.isfinite(float(r[1]))]
    if len(vals) < max(10, recent_n):
        return {'enabled': True, 'interrupted': False, 'metric': metric, 'n': len(vals), 'reason': 'not_enough_volume_rows'}
    recent = vals[:recent_n]
    baseline = vals[recent_n:recent_n+base_n] or vals
    med_recent = _median(recent)
    med_base = _median(baseline)
    ratio = (med_recent / med_base) if med_recent is not None and med_base and med_base > 0 else None
    interrupted = bool(ratio is not None and (ratio >= shift or ratio <= 1.0 / max(shift, 1e-9)))
    return {'enabled': True, 'interrupted': interrupted, 'metric': metric, 'recent_median': med_recent, 'baseline_median': med_base, 'ratio': ratio, 'shift_ratio': shift, 'recent_bars': recent_n, 'baseline_bars': base_n}

def _fit_slippage_calibration(rows: list) -> dict:
    y = []
    x = []
    spread = []
    groups = {}
    for r in rows:
        try:
            yy = float(r.get('actual_adverse_bp'))
            xx = float(r.get('snapshot_est_sweep_bp') or 0.0)
            sp = float(r.get('snapshot_spread_bp') or 0.0)
        except Exception:
            continue
        if not math.isfinite(yy) or not math.isfinite(xx):
            continue
        y.append(yy); x.append(xx); spread.append(sp)
        key = f"{r.get('strategy_side')}|{r.get('order_action')}|{r.get('order_direction')}"
        groups.setdefault(key, []).append(yy)
    n = len(y)
    if n <= 0:
        return {'n': 0}
    y_mean = sum(y)/n; x_mean = sum(x)/n
    var_x = sum((v-x_mean)**2 for v in x)
    slope = sum((x[i]-x_mean)*(y[i]-y_mean) for i in range(n))/var_x if var_x > 1e-12 else 1.0
    intercept = y_mean - slope*x_mean
    pred = [intercept + slope*v for v in x]
    abs_err = [abs(pred[i]-y[i]) for i in range(n)]
    y_abs = [abs(v) for v in y]
    y_nonneg = [max(0.0, v) for v in y]
    y_abs_sorted = sorted(y_abs)
    def q(vals, p):
        vals = sorted(vals)
        if not vals: return None
        k = (len(vals)-1)*p
        lo = int(math.floor(k)); hi = int(math.ceil(k))
        if lo == hi: return vals[lo]
        return vals[lo]*(hi-k)+vals[hi]*(k-lo)
    group_stats = {}
    for k, vals in groups.items():
        group_stats[k] = {'n': len(vals), 'mean_adverse_bp': sum(vals)/len(vals), 'mean_abs_bp': sum(abs(v) for v in vals)/len(vals)}
    return {
        'n': n,
        'mean_adverse_bp': y_mean,
        'mean_nonnegative_adverse_bp': sum(y_nonneg)/n,
        'mean_abs_bp': sum(y_abs)/n,
        'median_adverse_bp': q(y, 0.5),
        'p90_abs_bp': q(y_abs, 0.90),
        'p95_abs_bp': q(y_abs, 0.95),
        'max_abs_bp': max(y_abs),
        'mean_sweep_est_bp': sum(x)/n,
        'mean_spread_bp': sum(spread)/len(spread) if spread else None,
        'orderbook_sweep_linear': {'kind': 'orderbook_sweep_linear', 'intercept_bp': intercept, 'sweep_coeff': slope, 'clip_min_bp': 0.0, 'clip_max_bp': q(y_abs, 0.95) or max(y_abs)},
        'static_suggestion_bp': sum(y_nonneg)/n,
        'fit_mae_bp': sum(abs_err)/n,
        'fit_bias_bp': sum(pred[i]-y[i] for i in range(n))/n,
        'by_group': group_stats,
    }

def _maybe_update_slippage_calibration(cfg: dict, results_dir: str, session_db_path: str, cache_db_path: str, run_id: str, force: bool = False):
    cal = _calibration_cfg(cfg)
    if not cal.get('enabled', False):
        return None
    # Cheap throttle: use function attribute, no DB write every tick if not needed.
    now = time.time()
    last = getattr(_maybe_update_slippage_calibration, '_last_ts', 0.0)
    interval = float(cal.get('min_update_interval_sec', 30.0) or 30.0)
    if not force and now - last < interval:
        return None
    _maybe_update_slippage_calibration._last_ts = now
    rows = _read_recent_slippage_observations(session_db_path, cal)
    vol = _volume_regime_status(cache_db_path, cal)
    status = 'ok'
    if len(rows) < int(cal.get('min_obs', 30) or 30):
        status = 'collecting'
    if vol.get('interrupted'):
        status = 'interrupted_volume_regime_shift'
    fit = _fit_slippage_calibration(rows) if rows else {'n': 0}
    payload = {'status': status, 'run_id': run_id, 'ts_utc': _dt.datetime.now(_dt.timezone.utc).isoformat(), 'calibration_cfg': cal, 'volume_regime': vol, 'fit': fit}
    try:
        Path(results_dir).mkdir(parents=True, exist_ok=True)
        out_path = Path(results_dir) / str(cal.get('output_json', 'live_slippage_calibration.json'))
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding='utf-8')
        # This YAML can be copied into backtest.slippage after enough observations.
        sugg = {
            'backtest': {
                'use_live_sync': False,
                'slippage': {
                    'enabled': True,
                    'mode': 'static' if status != 'ok' else 'static',
                    'static_bp': round(float(fit.get('static_suggestion_bp') or 0.0), 4),
                    'note': f"online calibration status={status}, n={fit.get('n', 0)}; orderbook_sweep_linear stored in live_slippage_calibration.json"
                }
            }
        }
        (Path(results_dir) / str(cal.get('suggested_yaml', 'backtest_slippage_suggestion.yaml'))).write_text(json.dumps(sugg, ensure_ascii=False, indent=2), encoding='utf-8')
    except Exception:
        pass
    try:
        debug_event(session_db_path, run_id, 'slippage_calibration_update', payload, level='WARNING' if status.startswith('interrupted') else 'INFO')
    except Exception:
        pass
    return payload

def _record_order(db_path: str, *, bar_time, symbol: str, side: str, type_: str, price: float, qty: float, status: str, reason: str, run_id: str, exchange_order_id: str = '', extra=None):
    insert_order_row(db_path, {'order_id': exchange_order_id or str(uuid.uuid4()), 'ts_utc': _dt.datetime.now(_dt.timezone.utc).isoformat(), 'bar_time_utc': bar_time.isoformat() if hasattr(bar_time, 'isoformat') else str(bar_time), 'mode': 'live', 'symbol': symbol, 'side': side, 'type': type_, 'price': float(price or 0.0), 'qty': float(qty or 0.0), 'status': status, 'reason': reason, 'run_id': run_id, 'extra': json.dumps(extra or {}, ensure_ascii=False, sort_keys=True)})


def _place_tp_sl_after_open(fetcher: CCXTFetcher, sym: str, side: str, qty: float, tp_price, sl_price, position_mode: str):
    if not ENABLE_FALLBACK_TPSL:
        return
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    pos_oneway = str(position_mode or '').lower().startswith('one')
    _, lot_step, min_qty = _market_steps(fetcher, sym)
    qty_eff = float(qty or 0.0)
    if lot_step:
        qty_eff = _round_to_step(qty_eff, lot_step)
        if qty_eff > qty:
            qty_eff = _floor_step(qty, lot_step)
    if min_qty and qty_eff < min_qty:
        return
    close_side = _order_close_side(side)
    ex_id = _exchange_id(fetcher)
    base = {'positionSide': 'BOTH' if pos_oneway else side}
    if not (ex_id == 'bingx' and not pos_oneway):
        base['reduceOnly'] = True
    if tp_price is not None and float(tp_price) > 0:
        for otype, oprice, params in [('take_profit', float(tp_price), dict(base)), ('take_profit_market', None, {**base, 'triggerPrice': float(tp_price)}), ('limit', float(tp_price), {**base, 'takeProfit': True})]:
            try:
                fetcher.ex.create_order(ccxt_sym, otype, close_side, qty_eff, oprice, params); sleep_ms(RATE_MS); break
            except Exception:
                pass
    if sl_price is not None and float(sl_price) > 0:
        for params in ({**base, 'triggerPrice': float(sl_price)}, {**base, 'stopPrice': float(sl_price)}):
            try:
                fetcher.ex.create_order(ccxt_sym, 'stop_market', close_side, qty_eff, None, params); sleep_ms(RATE_MS); break
            except Exception:
                pass


def _compute_entry_qty(sig, side: str, entry_px: float, notional_long: float, notional_short: float) -> float:
    qty = _safe_float(_sig_get(sig, 'qty'))
    if qty is not None and qty > 0:
        return float(qty)
    notional = float(notional_long if str(side).upper() == 'LONG' else notional_short)
    return notional / max(float(entry_px or 0.0), 1e-12)


def place_open_qty(fetcher: CCXTFetcher, sym: str, side: str, qty: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    qty = _normalize_order_qty(fetcher, sym, qty, is_close=False)
    if qty <= 0:
        return {'ok': False, 'error': 'open_qty_zero_after_normalize', 'qty': qty}
    order_side = _order_open_side(side)
    params = {'positionSide': str(side).upper()} if str(position_mode or '').lower() == 'hedge' else {}
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'market', order_side, qty, None, params); sleep_ms(RATE_MS)
        return {'ok': True, 'order': od, 'qty': qty, 'params': params}
    except Exception as e:
        msg = str(e).lower()
        if ('one-way mode' in msg) or ('positionside' in msg):
            try:
                od = fetcher.ex.create_order(ccxt_sym, 'market', order_side, qty, None, {}); sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': {}, 'retry': True}
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty}
        err = str(e)
        msg = err.lower()
        if ('no position to close' in msg) or ('code":101205' in msg) or ("code': 101205" in msg):
            return {'ok': False, 'error': 'no_position', 'qty': qty}
        return {'ok': False, 'error': err, 'qty': qty}


def place_reduce_only(fetcher: CCXTFetcher, sym: str, entry_side: str, qty: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
    qty = _normalize_order_qty(fetcher, sym, qty, is_close=True)
    if qty <= 0:
        return {'ok': False, 'error': 'close_qty_zero_after_normalize', 'qty': qty}
    close_side = _order_close_side(entry_side)
    ex_id = _exchange_id(fetcher)
    params = {}
    if str(position_mode or '').lower() == 'hedge':
        params['positionSide'] = str(entry_side).upper()
        # BingX hedge mode rejects reduceOnly on market close orders.
        if ex_id != 'bingx':
            params['reduceOnly'] = True
    else:
        params['reduceOnly'] = True
    try:
        od = fetcher.ex.create_order(ccxt_sym, 'market', close_side, qty, None, params); sleep_ms(RATE_MS)
        return {'ok': True, 'order': od, 'qty': qty, 'params': params}
    except Exception as e:
        msg = str(e).lower()
        if ('reduceonly' in msg or 'reduce only' in msg) and 'reduceOnly' in params:
            try:
                p2 = {k: v for k, v in params.items() if k != 'reduceOnly'}
                od = fetcher.ex.create_order(ccxt_sym, 'market', close_side, qty, None, p2); sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': p2, 'retry': True}
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty}
        if ('one-way mode' in msg) or ('positionside' in msg):
            try:
                base = {'reduceOnly': True}
                if ex_id == 'bingx' and str(position_mode or '').lower() == 'hedge':
                    base = {}
                od = fetcher.ex.create_order(ccxt_sym, 'market', close_side, qty, None, base); sleep_ms(RATE_MS)
                return {'ok': True, 'order': od, 'qty': qty, 'params': base, 'retry': True}
            except Exception as e2:
                return {'ok': False, 'error': str(e2), 'qty': qty}
        err = str(e)
        msg = err.lower()
        if ('no position to close' in msg) or ('code":101205' in msg) or ("code': 101205" in msg):
            return {'ok': False, 'error': 'no_position', 'qty': qty}
        return {'ok': False, 'error': err, 'qty': qty}



def _finalize_open_success(fetcher, strat, rec, *, sym, side, requested_px, qty_requested, ex_order_id, bar_close, fill_px, fill_dt, session_db_path, bot_id, results_dir, positions, run_id, position_mode, strategy_event='open'):
    fill = float(fill_px) if fill_px is not None else float(requested_px)
    adverse_bp = _adverse_slip_bp(side, requested_px, fill, is_close=False)
    if MAX_ENTRY_SLIP_BP > 0 and adverse_bp > MAX_ENTRY_SLIP_BP:
        close_res = place_reduce_only(fetcher, sym, side, qty_requested, position_mode)
        _exec_metric_inc(sym, side, 'OPEN', 'unknown', 'reverted', 1.0)
        _write_exec_metrics(results_dir, session_db_path, run_id, event_type='execution_metrics_open_reverted')
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty_requested, status='REVERTED', reason=f'max_entry_slip {adverse_bp:.3f}bp > {MAX_ENTRY_SLIP_BP:.3f}bp', run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'close_res': close_res})
        return False, {'error': 'max_entry_slip'}
    ex_pos = _fetch_exchange_position(fetcher, sym, side)
    qty = float(ex_pos.get('qty') if ex_pos else qty_requested)
    entry = float(ex_pos.get('entry') if ex_pos else fill)
    rec.update({'symbol': sym, 'side': side, 'qty': qty, 'entry': entry, 'ts_open': rec.get('ts_open') or bar_close.isoformat(), 'run_id': run_id, 'order_id': rec.get('order_id') or str(uuid.uuid4()), 'exchange_order_id': ex_order_id, 'entry_fill': fill, 'entry_fill_ts': fill_dt.isoformat() if fill_dt else None, 'entry_slip_bp': _signed_slip_bp(side, requested_px, fill, is_close=False), 'entry_lag_sec': (fill_dt - bar_close).total_seconds() if fill_dt and hasattr(bar_close, 'timestamp') else None, 'entry_mark_price': fetcher.fetch_mark_price(sym)})
    positions[pos_key(sym, side)] = rec
    save_positions(results_dir, positions)
    db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN'})
    _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='FILLED', reason='open', run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'entry': entry, 'strategy_event': strategy_event})
    _strategy_sync_after_fill(strat, sym, qty=qty, entry=entry, fill_price=fill, delta_qty=qty_requested, event=strategy_event, bar_time=bar_close, extra={'exchange_order_id': ex_order_id, 'requested_px': requested_px, 'order_type': rec.get('entry_order_type') or 'market'})
    try:
        _place_tp_sl_after_open(fetcher, sym, side, qty, rec.get('tp_price'), rec.get('sl_price'), position_mode)
    except Exception:
        pass
    cprint('[open OK]', sym, side, f'qty={qty:.6g} px={entry:.6g}' + (f' id={ex_order_id}' if ex_order_id else ''), fg='green', bold=True)
    _emit_runtime_debug(session_db_path, run_id, 'open_ok', {'symbol': sym, 'side': side, 'qty': qty, 'entry': entry, 'fill': fill, 'order_id': ex_order_id, 'entry_lag_sec': rec.get('entry_lag_sec'), 'entry_slip_bp': rec.get('entry_slip_bp')}, level='INFO', fg='green')
    return True, rec


def _execute_open_with_rollback(fetcher, strat, *, sym, side, requested_px, qty, bar_close, position_mode, snapshot, session_db_path, run_id, bot_id, results_dir, positions, tp_price=None, sl_price=None, order_type='market', limit_price=None, post_only=True, strategy_event='open'):
    pre_book = safe_fetch_order_book(fetcher, sym, limit=10)
    pre_snapshot = record_pretrade_snapshot(
        session_db_path,
        run_id=run_id,
        bar_time_utc=bar_close.isoformat(),
        symbol=sym,
        strategy_side=side,
        order_action='OPEN',
        qty=qty,
        requested_price=requested_px,
        book=pre_book,
    )
    order_type_norm = 'limit' if str(order_type or 'market').lower() in {'limit', 'maker', 'maker_limit'} and limit_price not in (None, '') else 'market'
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'signals', 1.0)
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'submitted', 1.0)
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'qty_requested', qty)
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'notional_requested', float(qty or 0.0) * float(requested_px or 0.0))
    if pre_snapshot and pre_snapshot.get('spread_bp') is not None:
        _emit_runtime_debug(session_db_path, run_id, 'orderbook_pre_open', {'symbol': sym, 'side': side, 'qty': qty, 'spread_bp': pre_snapshot.get('spread_bp'), 'est_sweep_bp': pre_snapshot.get('est_sweep_slip_bp'), 'imbalance': pre_snapshot.get('book_imbalance'), 'order_type': order_type_norm}, level='INFO', fg='cyan')
    if order_type_norm == 'limit':
        raw_limit_price = float(limit_price)
        safe_limit_price = _sanitize_post_only_limit_price(
            fetcher, sym, side, raw_limit_price, pre_snapshot,
            passive_ticks=ENTRY_LIMIT_PASSIVE_TICKS,
            enabled=bool(post_only) and ENTRY_LIMIT_USE_BOOK_PASSIVE,
        )
        if abs(float(safe_limit_price) - float(raw_limit_price)) > 1e-12:
            _emit_runtime_debug(
                session_db_path, run_id, 'post_only_reprice',
                {
                    'symbol': sym,
                    'side': side,
                    'raw_limit_price': raw_limit_price,
                    'safe_limit_price': safe_limit_price,
                    'best_bid': (pre_snapshot or {}).get('best_bid'),
                    'best_ask': (pre_snapshot or {}).get('best_ask'),
                    'passive_ticks': ENTRY_LIMIT_PASSIVE_TICKS,
                },
                level='INFO', fg='cyan'
            )
        res = place_open_qty_limit(fetcher, sym, side, qty, float(safe_limit_price), position_mode, post_only=bool(post_only))
    else:
        res = place_open_qty(fetcher, sym, side, qty, position_mode)
    if not res.get('ok'):
        fail_reason = str(res.get('error') or res.get('skip_reason') or 'open_fail')
        _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'rejected', 1.0)
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='REJECTED', reason=fail_reason, run_id=run_id, extra={'order_type': order_type_norm})
        if order_type_norm == 'limit' and ENTRY_LIMIT_FALLBACK_TO_MARKET and ENTRY_LIMIT_FALLBACK_ON_REJECT:
            return _place_market_fallback_open(fetcher, strat, sym=sym, side=side, requested_px=requested_px, qty=qty, bar_close=bar_close, position_mode=position_mode, session_db_path=session_db_path, run_id=run_id, bot_id=bot_id, results_dir=results_dir, positions=positions, tp_price=tp_price, sl_price=sl_price, snapshot=snapshot, pre_snapshot=pre_snapshot, fallback_reason=f'limit_reject:{fail_reason}', strategy_event=strategy_event)
        _strategy_restore(strat, sym, snapshot); _strategy_rejected(strat, sym, 'open', res)
        _write_exec_metrics(results_dir, session_db_path, run_id, event_type='execution_metrics_open_fail')
        _emit_runtime_debug(session_db_path, run_id, 'open_fail', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': fail_reason, 'exchange_response': res, 'order_type': order_type_norm}, level='ERROR', fg='red')
        return False, res
    ex_order_id = _extract_order_id(res.get('order'))
    wait_sec = ENTRY_LIMIT_TTL_SEC if order_type_norm == 'limit' else ORDER_SYNC_WAIT_SEC
    fill_px, fill_dt, od = _fetch_order_fill(fetcher, sym, ex_order_id, wait_sec=wait_sec)
    if fill_px is None:
        cancel_state, cancel_fill_px, cancel_fill_dt, cancel_od = _cancel_order_and_fetch_final(
            fetcher, sym, ex_order_id, confirm_sec=ENTRY_LIMIT_CANCEL_CONFIRM_SEC
        )
        if cancel_state == 'filled' and cancel_fill_px is not None:
            fill_px, fill_dt, od = cancel_fill_px, cancel_fill_dt, cancel_od
            _emit_runtime_debug(session_db_path, run_id, 'limit_filled_during_cancel', {'symbol': sym, 'side': side, 'order_id': ex_order_id, 'fill_px': fill_px}, level='WARNING', fg='yellow')
        else:
            reason = str(((cancel_od or od or {}).get('info') or {}).get('reason') or (cancel_od or od or {}).get('status') or f'timeout_no_fill:{cancel_state}')
            _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'canceled_no_fill', 1.0)
            _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='CANCELED' if cancel_state == 'canceled' else 'UNKNOWN', reason=reason, run_id=run_id, exchange_order_id=ex_order_id, extra={'order_type': order_type_norm, 'cancel_state': cancel_state})
            if cancel_state == 'canceled' and order_type_norm == 'limit' and ENTRY_LIMIT_FALLBACK_TO_MARKET and ENTRY_LIMIT_FALLBACK_ON_TIMEOUT:
                return _place_market_fallback_open(fetcher, strat, sym=sym, side=side, requested_px=requested_px, qty=qty, bar_close=bar_close, position_mode=position_mode, session_db_path=session_db_path, run_id=run_id, bot_id=bot_id, results_dir=results_dir, positions=positions, tp_price=tp_price, sl_price=sl_price, snapshot=snapshot, pre_snapshot=pre_snapshot, fallback_reason=f'limit_timeout:{reason}', strategy_event=strategy_event)
            # If cancel is not confirmed, do NOT market fallback. Otherwise we risk double-entry:
            # old limit fills late + fallback market also opens.
            _strategy_restore(strat, sym, snapshot); _strategy_rejected(strat, sym, 'open', {'reason': reason, 'order_id': ex_order_id, 'cancel_state': cancel_state})
            _write_exec_metrics(results_dir, session_db_path, run_id, event_type='execution_metrics_open_nofill')
            _emit_runtime_debug(session_db_path, run_id, 'open_nofill', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': reason, 'order_id': ex_order_id, 'cancel_state': cancel_state, 'order_type': order_type_norm}, level='ERROR', fg='red')
            return False, {'error': reason, 'order': cancel_od or od, 'cancel_state': cancel_state}
    record_fill_observation(
        session_db_path,
        run_id=run_id,
        bar_time_utc=bar_close.isoformat(),
        symbol=sym,
        strategy_side=side,
        order_action='OPEN',
        qty=qty,
        requested_price=requested_px,
        fill_price=fill_px,
        pre_snapshot=pre_snapshot,
    )
    _online_update_slippage_model(session_db_path, results_dir, sym, side, 'OPEN', qty, requested_px, fill_px, pre_snapshot)
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'filled', 1.0)
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'qty_filled', qty)
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'notional_filled', float(qty or 0.0) * float(fill_px or requested_px or 0.0))
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'adverse_slip_bp_sum', _adverse_slip_bp(side, requested_px, fill_px, is_close=False))
    _exec_metric_inc(sym, side, 'OPEN', order_type_norm, 'signed_slip_bp_sum', _signed_slip_bp(side, requested_px, fill_px, is_close=False))
    _write_exec_metrics(results_dir, session_db_path, run_id, event_type='execution_metrics_open_fill')
    rec = {'symbol': sym, 'side': side, 'qty': qty, 'entry': requested_px, 'tp_price': tp_price, 'sl_price': sl_price, 'ts_open': bar_close.isoformat(), 'run_id': run_id, 'order_id': str(uuid.uuid4())}
    return _finalize_open_success(fetcher, strat, rec, sym=sym, side=side, requested_px=requested_px, qty_requested=qty, ex_order_id=ex_order_id, bar_close=bar_close, fill_px=fill_px, fill_dt=fill_dt, session_db_path=session_db_path, bot_id=bot_id, results_dir=results_dir, positions=positions, run_id=run_id, position_mode=position_mode, strategy_event=strategy_event)


def _execute_reduce_with_rollback(fetcher, strat, *, sym, side, qty, requested_px, bar_close, position_mode, snapshot, session_db_path, run_id, close_reason, rec, positions, results_dir, bot_id, event_type, partial=False):
    action_name = 'PARTIAL' if partial else 'CLOSE'
    ex_before = _fetch_exchange_position(fetcher, sym, side)
    if not ex_before or float(ex_before.get('qty') or 0.0) <= 1e-12:
        key = pos_key(sym, side)
        _sync_close_local_only(strat, key, rec, positions, results_dir, session_db_path, bot_id, run_id, 'exchange_no_position_before_close')
        return True, {'closed': True, 'synced_only': True}
    qty = _normalize_order_qty(fetcher, sym, min(float(qty or 0.0), float(ex_before.get('qty') or 0.0)), is_close=True, max_qty=float(ex_before.get('qty') or 0.0))
    if qty <= 0:
        _strategy_restore(strat, sym, snapshot)
        return False, {'error': 'zero_close_qty_after_reconcile'}
    pre_book = safe_fetch_order_book(fetcher, sym, limit=10)
    pre_snapshot = record_pretrade_snapshot(
        session_db_path,
        run_id=run_id,
        bar_time_utc=bar_close.isoformat(),
        symbol=sym,
        strategy_side=side,
        order_action=action_name,
        qty=qty,
        requested_price=requested_px,
        book=pre_book,
    )
    if pre_snapshot and pre_snapshot.get('spread_bp') is not None:
        _emit_runtime_debug(session_db_path, run_id, 'orderbook_pre_close', {'symbol': sym, 'side': side, 'qty': qty, 'spread_bp': pre_snapshot.get('spread_bp'), 'est_sweep_bp': pre_snapshot.get('est_sweep_slip_bp'), 'imbalance': pre_snapshot.get('book_imbalance'), 'partial': partial}, level='INFO', fg='cyan')
    res = place_reduce_only(fetcher, sym, side, qty, position_mode)
    if not res.get('ok'):
        fail_reason = str(res.get('error') or 'close_fail')
        if fail_reason == 'no_position':
            key = pos_key(sym, side)
            _sync_close_local_only(strat, key, rec, positions, results_dir, session_db_path, bot_id, run_id, 'exchange_no_position_on_reduce')
            _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='CLOSE_PARTIAL' if partial else 'CLOSE', price=requested_px, qty=qty, status='SYNCED', reason=fail_reason, run_id=run_id)
            return True, {'closed': True, 'synced_only': True}
        _strategy_restore(strat, sym, snapshot); _strategy_rejected(strat, sym, event_type, res)
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='CLOSE_PARTIAL' if partial else 'CLOSE', price=requested_px, qty=qty, status='REJECTED', reason=fail_reason, run_id=run_id)
        _emit_runtime_debug(session_db_path, run_id, 'close_fail', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': fail_reason, 'exchange_response': res, 'partial': partial}, level='ERROR', fg='red')
        return False, res
    ex_order_id = _extract_order_id(res.get('order'))
    fill_px, fill_dt, od = _fetch_order_fill(fetcher, sym, ex_order_id)
    if fill_px is None:
        try:
            if ex_order_id:
                fetcher.ex.cancel_order(ex_order_id, fetcher.resolve_symbol(sym) or sym); sleep_ms(RATE_MS)
        except Exception:
            pass
        _strategy_restore(strat, sym, snapshot); reason = str(((od or {}).get('info') or {}).get('reason') or (od or {}).get('status') or 'timeout_no_fill')
        _strategy_rejected(strat, sym, event_type, {'reason': reason, 'order_id': ex_order_id})
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='CLOSE_PARTIAL' if partial else 'CLOSE', price=requested_px, qty=qty, status='CANCELED', reason=reason, run_id=run_id, exchange_order_id=ex_order_id)
        _emit_runtime_debug(session_db_path, run_id, 'close_nofill', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': reason, 'order_id': ex_order_id, 'order': od, 'partial': partial, 'pre_snapshot': pre_snapshot}, level='ERROR', fg='red')
        return False, {'error': reason, 'order': od}
    fill = float(fill_px)
    record_fill_observation(
        session_db_path,
        run_id=run_id,
        bar_time_utc=bar_close.isoformat(),
        symbol=sym,
        strategy_side=side,
        order_action=action_name,
        qty=qty,
        requested_price=requested_px,
        fill_price=fill,
        pre_snapshot=pre_snapshot,
    )
    _online_update_slippage_model(session_db_path, results_dir, sym, side, action_name, qty, requested_px, fill, pre_snapshot)
    ex_pos = _fetch_exchange_position(fetcher, sym, side)
    if not ex_pos or float(ex_pos.get('qty') or 0.0) <= 1e-12:
        positions.pop(pos_key(sym, side), None); save_positions(results_dir, positions)
        db_mark_closed(session_db_path, bot_id, rec.get('order_id'), _dt.datetime.now(_dt.timezone.utc).isoformat(), exit_fill=fill, exit_fill_ts=fill_dt.isoformat() if fill_dt else None, exit_slip_bp=_signed_slip_bp(side, requested_px, fill, is_close=True), exit_lag_sec=(fill_dt - bar_close).total_seconds() if fill_dt else None, exit_mark_price=fetcher.fetch_mark_price(sym), close_reason=_normalize_close_reason(close_reason, 'close'))
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='CLOSE_PARTIAL' if partial else 'CLOSE', price=requested_px, qty=qty, status='FILLED', reason=_normalize_close_reason(close_reason, 'close'), run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'closed': True, 'strategy_event': 'close'})
        _strategy_sync_after_fill(strat, sym, qty=0.0, entry=0.0, fill_price=fill, delta_qty=qty, event='close', bar_time=bar_close, extra={'exchange_order_id': ex_order_id, 'requested_px': requested_px, 'close_reason': _normalize_close_reason(close_reason, 'close'), 'closed': True})
        _emit_runtime_debug(session_db_path, run_id, 'close_ok', {'symbol': sym, 'side': side, 'qty': qty, 'fill': fill, 'closed': True, 'close_reason': _normalize_close_reason(close_reason, 'close')}, level='INFO', fg='green')
        return True, {'closed': True}
    new_qty = float(ex_pos['qty']); new_entry = float(ex_pos['entry'])
    rec.update({'qty': new_qty, 'entry': new_entry, 'exit_fill': fill, 'exit_fill_ts': fill_dt.isoformat() if fill_dt else None, 'exit_slip_bp': _signed_slip_bp(side, requested_px, fill, is_close=True), 'exit_lag_sec': (fill_dt - bar_close).total_seconds() if fill_dt else None, 'exit_mark_price': fetcher.fetch_mark_price(sym)})
    positions[pos_key(sym, side)] = rec; save_positions(results_dir, positions)
    db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN', 'close_reason': _normalize_close_reason(close_reason)})
    _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='CLOSE_PARTIAL' if partial else 'CLOSE', price=requested_px, qty=qty, status='FILLED', reason=_normalize_close_reason(close_reason, 'close'), run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'remaining_qty': new_qty, 'remaining_entry': new_entry, 'strategy_event': 'partial' if partial else 'close'})
    _strategy_sync_after_fill(strat, sym, qty=new_qty, entry=new_entry, fill_price=fill, delta_qty=qty, event='partial' if partial else 'close', bar_time=bar_close, extra={'exchange_order_id': ex_order_id, 'requested_px': requested_px, 'close_reason': _normalize_close_reason(close_reason, 'close'), 'closed': False, 'remaining_qty': new_qty, 'remaining_entry': new_entry})
    _emit_runtime_debug(session_db_path, run_id, 'close_ok', {'symbol': sym, 'side': side, 'qty': qty, 'fill': fill, 'closed': False, 'remaining_qty': new_qty, 'remaining_entry': new_entry, 'close_reason': _normalize_close_reason(close_reason, 'close'), 'partial': partial}, level='INFO', fg='green')
    return True, {'closed': False, 'qty': new_qty, 'entry': new_entry}


def _maybe_apply_manage_result(fetcher, key: str, rec: dict, row: dict, strat, positions: dict, results_dir: str, position_mode: str, session_db_path: str, bot_id: str, run_id: str, *, cfg: dict = None, pending_entries: dict = None):
    pending_entries = pending_entries if pending_entries is not None else {}
    sym, side = split_pos_key(key)
    pos_before = _PosLike(rec)
    qty_before, entry_before = float(pos_before.qty), float(pos_before.entry)
    snapshot = _strategy_snapshot(strat, sym)
    ex_sig = strat.manage_position(sym, row, pos_before, ctx={})
    action = str(getattr(ex_sig, 'action', '') or '').upper()
    reason = _normalize_close_reason(getattr(ex_sig, 'reason', None), action.lower())
    bar_dt = _dt.datetime.fromisoformat(str(row['datetime_utc']).replace('Z', '+00:00'))
    requested_px = _choose_requested_price(fetcher, sym, float(row.get('close') or 0.0))
    if action in {'TP', 'SL', 'EXIT'}:
        qty_close = float(rec.get('qty', 0.0) or 0.0)
        if qty_close <= 0:
            _strategy_restore(strat, sym, snapshot); return False
        pending_entries.pop(key, None)
        return _execute_reduce_with_rollback(fetcher, strat, sym=sym, side=side, qty=qty_close, requested_px=requested_px, bar_close=bar_dt, position_mode=position_mode, snapshot=snapshot, session_db_path=session_db_path, run_id=run_id, close_reason=reason, rec=rec, positions=positions, results_dir=results_dir, bot_id=bot_id, event_type='close', partial=False)[0]
    if action == 'TP_PARTIAL':
        qty_frac = max(0.0, min(1.0, float(getattr(ex_sig, 'qty_frac', 0.0) or 0.0)))
        qty_close = float(rec.get('qty', 0.0) or 0.0) * qty_frac
        if qty_close <= 0:
            _strategy_restore(strat, sym, snapshot); return False
        return _execute_reduce_with_rollback(fetcher, strat, sym=sym, side=side, qty=qty_close, requested_px=requested_px, bar_close=bar_dt, position_mode=position_mode, snapshot=snapshot, session_db_path=session_db_path, run_id=run_id, close_reason=reason or action.lower(), rec=rec, positions=positions, results_dir=results_dir, bot_id=bot_id, event_type='partial', partial=True)[0]
    qty_after, entry_after = float(pos_before.qty), float(pos_before.entry)
    if qty_after > qty_before + 1e-12:
        delta_qty = qty_after - qty_before
        if _pending_entry_order_type(cfg or {}) in {'limit', 'maker', 'maker_limit'}:
            limit_px = _snapshot_limit_price(snapshot, requested_px)
            res = place_open_qty_limit(fetcher, sym, side, delta_qty, limit_px, position_mode)
            if not res.get('ok'):
                _strategy_restore(strat, sym, snapshot); _strategy_rejected(strat, sym, 'dca_limit_open', res)
                return False
            ex_order_id = _extract_order_id(res.get('order'))
            pending_entries[key] = {'symbol': sym, 'side': side, 'exchange_order_id': ex_order_id, 'created_bar_iso': bar_dt.isoformat(), 'limit_price': float(limit_px), 'delta_qty': float(delta_qty), 'strategy_snapshot': snapshot, 'applied_filled_qty': 0.0, 'last_order': res.get('order'), 'status': str((res.get('order') or {}).get('status') or '').lower()}
            _sync_pending_entry_orders(fetcher, pending_entries, positions, results_dir, session_db_path, bot_id, strat, strat, bar_dt.isoformat(), run_id=run_id)
            pend = pending_entries.get(key)
            if pend is None and float(positions.get(key, rec).get('qty', 0.0) or 0.0) > qty_before + 1e-12:
                positions[key]['order_id'] = rec.get('order_id') or positions[key].get('order_id')
                save_positions(results_dir, positions)
                db_upsert_open_position(session_db_path, bot_id, {**positions[key], 'status': 'OPEN'})
                return True
            return False
        ok, _ = _execute_open_with_rollback(fetcher, strat, sym=sym, side=side, requested_px=requested_px, qty=delta_qty, bar_close=bar_dt, position_mode=position_mode, snapshot=snapshot, session_db_path=session_db_path, run_id=run_id, bot_id=bot_id, results_dir=results_dir, positions=positions, tp_price=rec.get('tp_price'), sl_price=rec.get('sl_price'), strategy_event='dca')
        if ok:
            new_rec = positions[pos_key(sym, side)]
            new_rec['order_id'] = rec.get('order_id') or new_rec.get('order_id')
            positions[pos_key(sym, side)] = new_rec; save_positions(results_dir, positions)
            db_upsert_open_position(session_db_path, bot_id, {**new_rec, 'status': 'OPEN'})
            # State sync is already performed inside _finalize_open_success with strategy_event='dca'.
            pass
        return ok
    if abs(entry_after - entry_before) > 1e-12:
        rec['entry'] = entry_after; positions[key] = rec; save_positions(results_dir, positions); db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN'}); return True
    return False


def _attempt_entry(fetcher, sym: str, side: str, strat, row: dict, positions: dict, results_dir: str, position_mode: str, session_db_path: str, bot_id: str, run_id: str, *, notional_long: float, notional_short: float):
    if pos_key(sym, side) in positions:
        return False
    snapshot = _strategy_snapshot(strat, sym)
    sig = strat.entry_signal(True, sym, row, ctx={})
    if sig is None:
        return False
    requested_px = _choose_requested_price(fetcher, sym, float(row.get('close') or 0.0))
    qty = _compute_entry_qty(sig, side, requested_px, notional_long, notional_short)
    order_type = str(_sig_get(sig, 'order_type', 'market') or 'market').lower().strip()
    limit_price = _sig_get(sig, 'limit_price', None)
    post_only = bool(_sig_get(sig, 'entry_limit_post_only', True))
    if limit_price in (None, '') and order_type in {'limit', 'maker', 'maker_limit'}:
        limit_price = requested_px
    return _execute_open_with_rollback(fetcher, strat, sym=sym, side=side, requested_px=requested_px, qty=qty, bar_close=_dt.datetime.fromisoformat(str(row['datetime_utc']).replace('Z', '+00:00')), position_mode=position_mode, snapshot=snapshot, session_db_path=session_db_path, run_id=run_id, bot_id=bot_id, results_dir=results_dir, positions=positions, tp_price=_sig_get(sig, 'tp'), sl_price=_sig_get(sig, 'sl'), order_type=order_type, limit_price=limit_price, post_only=post_only, strategy_event='first')[0]



def _strategy_prewarm_requirement_bars(strategies, tf_sec: int, cfg: dict, args) -> int:
    vals = []
    for strat in strategies:
        fn = getattr(strat, 'warmup_requirements', None)
        if callable(fn):
            try:
                req = fn(timeframe_seconds=tf_sec)
                if isinstance(req, dict):
                    vals.append(int(req.get('min_bars', 0) or 0))
            except Exception:
                pass
    try:
        vals.append(int(getattr(args, 'prewarm_bars', 0) or 0))
    except Exception:
        pass
    try:
        vals.append(int(((cfg.get('runner') or {}).get('prewarm') or {}).get('bars', 0) or 0))
    except Exception:
        pass
    try:
        h = int(getattr(args, 'prewarm_hours', 0) or 0)
        if h > 0:
            vals.append(int(math.ceil(h * 3600 / max(1, int(tf_sec)))))
    except Exception:
        pass
    try:
        h = int(((cfg.get('runner') or {}).get('prewarm') or {}).get('hours', 0) or 0)
        if h > 0:
            vals.append(int(math.ceil(h * 3600 / max(1, int(tf_sec)))))
    except Exception:
        pass
    return max([0] + vals)


def _rows_from_feats_df(feats_df):
    rows = []
    try:
        for idx, r in feats_df.iterrows():
            row = r.to_dict()
            try:
                row['datetime_utc'] = idx.isoformat()
            except Exception:
                row['datetime_utc'] = str(idx)
            rows.append(row)
    except Exception:
        return []
    return rows


def _strategy_warmup_pair(strat_long, strat_short, sym: str, rows, session_db_path: str = '', run_id: str = '') -> bool:
    ok = True
    for side, strat in (('LONG', strat_long), ('SHORT', strat_short)):
        fn = getattr(strat, 'warmup_history', None)
        if callable(fn):
            try:
                res = fn(sym, rows, ctx={'source': 'live_runner_prewarm', 'side': side})
                ready_fn = getattr(strat, 'is_warm_ready', None)
                ready = bool(ready_fn(sym)) if callable(ready_fn) else True
                ok = ok and ready
                try:
                    debug_event(session_db_path, run_id, 'strategy_warmup', {'symbol': sym, 'side': side, 'bars': len(rows), 'result': res, 'ready': ready}, level='INFO')
                except Exception:
                    pass
            except Exception as e:
                ok = False
                try:
                    debug_event(session_db_path, run_id, 'strategy_warmup_error', {'symbol': sym, 'side': side, 'error': str(e)}, level='ERROR')
                except Exception:
                    pass
    return ok

def run_live(cfg: dict, args):
    global DEBUG_OPEN, DEBUG_RUNTIME, DEBUG_EVENT_PAYLOAD, CONSOLE_ERROR_SUMMARY, CONSOLE_ERROR_DETAILS
    runner_cfg0 = (cfg.get('runner') or {})
    console_cfg = (runner_cfg0.get('console') or {})
    DEBUG_RUNTIME = bool(getattr(args, 'debug', False) or runner_cfg0.get('debug_console', False) or console_cfg.get('debug', False))
    DEBUG_EVENT_PAYLOAD = bool(getattr(args, 'debug', False) or runner_cfg0.get('debug_payload', False) or console_cfg.get('debug_payload', False))
    CONSOLE_ERROR_SUMMARY = bool(console_cfg.get('error_summary', True))
    CONSOLE_ERROR_DETAILS = bool(console_cfg.get('error_details', False))
    globals()['ENTRY_LIMIT_USE_BOOK_PASSIVE'] = bool(runner_cfg0.get('entry_limit_use_book_passive_price', True))
    globals()['ENTRY_LIMIT_PASSIVE_TICKS'] = int(runner_cfg0.get('entry_limit_passive_ticks', 0) or 0)
    globals()['ENTRY_LIMIT_TTL_SEC'] = float(runner_cfg0.get('entry_limit_ttl_sec', ENTRY_LIMIT_TTL_SEC) or ENTRY_LIMIT_TTL_SEC)
    globals()['ENTRY_LIMIT_FALLBACK_TO_MARKET'] = bool(runner_cfg0.get('entry_limit_fallback_to_market', True))
    globals()['ENTRY_LIMIT_FALLBACK_ON_REJECT'] = bool(runner_cfg0.get('entry_limit_fallback_on_reject', True))
    globals()['ENTRY_LIMIT_FALLBACK_ON_TIMEOUT'] = bool(runner_cfg0.get('entry_limit_fallback_on_timeout', True))
    globals()['ENTRY_LIMIT_CANCEL_CONFIRM_SEC'] = float(runner_cfg0.get('entry_limit_cancel_confirm_sec', ENTRY_LIMIT_CANCEL_CONFIRM_SEC) or ENTRY_LIMIT_CANCEL_CONFIRM_SEC)
    assert ccxt is not None or str(args.exchange).lower() in {'virtual', 'sim', 'virtual_exchange', 'paper_virtual'}, 'ccxt required for LIVE mode'
    if getattr(args, 'env_file', '') and os.path.exists(args.env_file):
        for line in open(args.env_file, 'r', encoding='utf-8').read().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1); os.environ[k.strip()] = v.strip()
    fetcher = CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=getattr(args, 'debug', False))
    strat_long, strat_short = _load_strategy_pair(cfg)
    rcfg = _resolve_runner_cfg(cfg)
    tf, tf_sec, top_n = rcfg['timeframe'], _tf_to_seconds(rcfg['timeframe']), rcfg['top_n']
    md_tf = _normalize_fetch_timeframe(fetcher, tf)
    notional_long, notional_short, position_mode = rcfg['notional_long'], rcfg['notional_short'], rcfg['position_mode']
    dca_open_order_type = rcfg.get('dca_open_order_type', 'market')
    os.makedirs(args.results_dir, exist_ok=True)
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, getattr(args, 'session_db', ''), getattr(args, 'cache_out', ''))
    ensure_orders_db(session_db_path)
    ensure_exchange_trace_db(session_db_path)
    ensure_microstructure_tables(session_db_path)
    if ExchangeTraceProxy is not None and not isinstance(fetcher.ex, ExchangeTraceProxy):
        fetcher.ex = ExchangeTraceProxy(fetcher.ex, session_db_path, source='live_runner_dual', scenario_id='')
        try:
            fetcher.markets = fetcher.ex.load_markets()
        except Exception:
            fetcher.markets = getattr(fetcher.ex, 'markets', {}) or {}
    run_id = _dt.datetime.utcnow().strftime('LIVE_DUAL_%Y%m%d_%H%M%S')
    globals()['LIVE_SESSION_DB_PATH'] = session_db_path
    globals()['LIVE_RUN_ID'] = run_id
    globals()['LIVE_RESULTS_DIR'] = args.results_dir
    try:
        ensure_strategy_state_events_db(session_db_path)
    except Exception as e:
        _emit_runtime_debug(session_db_path, run_id, 'strategy_state_events_db_init_fail', {'error': str(e)}, level='ERROR', fg='red')
    if ExchangeTraceProxy is not None and isinstance(fetcher.ex, ExchangeTraceProxy):
        fetcher.ex._scenario_id = run_id
    globals()['LIVE_ARTIFACT_MANIFEST'] = _build_live_artifact_manifest(cfg)
    cfg['_live_artifact_manifest'] = LIVE_ARTIFACT_MANIFEST
    _write_live_run_manifest(args.results_dir, session_db_path, run_id, LIVE_ARTIFACT_MANIFEST)
    write_config_snapshot(session_db_path, run_id, cfg)
    DEBUG_OPEN = bool(getattr(args, 'debug', False) or cfg.get('debug_open', False))
    if DEBUG_OPEN:
        try:
            _auth_probe(fetcher, session_db_path, run_id)
        except Exception as e:
            _emit_runtime_debug(session_db_path, run_id, 'auth_probe_fail', {'error': str(e)}, level='ERROR', fg='red')
    try:
        _fit_bootstrap_slippage_model(session_db_path, args.results_dir, (args.symbol if hasattr(args, 'symbol') else None) or '')
    except Exception:
        pass
    positions = load_positions(args.results_dir)
    pending_entries = {}
    bot_id = make_bot_id(args.results_dir, args.exchange, tf)
    try:
        db_positions = db_load_open_positions(session_db_path, bot_id, include_side_in_key=True)
        if db_positions:
            positions = db_positions
    except Exception:
        pass
    if ENABLE_STARTUP_RECONCILE:
        try:
            _sync_pending_entry_orders(fetcher, pending_entries, positions, args.results_dir, session_db_path, bot_id, strat_long, strat_short, bar_close.isoformat(), run_id=run_id)
            for key, rec in list(positions.items()):
                sym0, side0 = split_pos_key(key)
                strat0 = strat_long if side0 == 'LONG' else strat_short
                _reconcile_position_with_exchange(fetcher, strat0, key, rec, positions, args.results_dir, session_db_path, bot_id, run_id)
        except Exception as e:
            _emit_runtime_debug(session_db_path, run_id, 'startup_position_reconcile_fail', {'error': str(e)}, level='ERROR', fg='red')
    last_bar_ts = None
    cprint('[live dual]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s', fg='cyan')
    if DEBUG_OPEN:
        _emit_runtime_debug(session_db_path, run_id, 'reconcile_flags', {'startup': ENABLE_STARTUP_RECONCILE, 'loop': ENABLE_LOOP_RECONCILE}, level='INFO', fg='cyan')
    if md_tf != tf:
        _emit_runtime_debug(session_db_path, run_id, 'market_data_timeframe_fallback', {'strategy_timeframe': tf, 'market_data_timeframe': md_tf, 'exchange': args.exchange}, level='WARNING', fg='yellow')

    prewarm_bars = _strategy_prewarm_requirement_bars((strat_long, strat_short), tf_sec, cfg, args)
    if prewarm_bars > 0:
        try:
            allow = list((cfg.get('universe', {}) or {}).get('allow', []) or [])
        except Exception:
            allow = []
        all_syms0 = sorted(set(fetcher.by_base.values()))
        universe0 = [s for s in all_syms0 if (not allow or s in allow)]
        _emit_runtime_debug(session_db_path, run_id, 'prewarm_start', {'bars': prewarm_bars, 'symbols': universe0, 'md_tf': md_tf}, level='INFO', fg='cyan')
        for ccxt_sym in universe0:
            try:
                df0 = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=md_tf, limit=max(60, int(prewarm_bars)))
                if df0 is None or len(df0) < 2:
                    _emit_runtime_debug(session_db_path, run_id, 'prewarm_no_data', {'symbol': ccxt_sym}, level='WARNING', fg='yellow')
                    continue
                feats0 = compute_feats(df0, tf_seconds=tf_sec)
                rows0 = _rows_from_feats_df(feats0)
                if getattr(args, 'hour_cache', 'off') in ('save', 'load'):
                    try: cache_out_upsert(cache_out_path, ccxt_sym, feats0)
                    except Exception: pass
                ready = _strategy_warmup_pair(strat_long, strat_short, ccxt_sym, rows0, session_db_path, run_id)
                _emit_runtime_debug(session_db_path, run_id, 'prewarm_done', {'symbol': ccxt_sym, 'bars_loaded': len(rows0), 'ready': ready}, level='INFO' if ready else 'WARNING', fg='green' if ready else 'yellow')
            except Exception as e:
                _emit_runtime_debug(session_db_path, run_id, 'prewarm_error', {'symbol': ccxt_sym, 'error': str(e)}, level='ERROR', fg='red')
    while True:
        now = _dt.datetime.now(_dt.timezone.utc)
        bar_close = _align_bar_close(now, tf_sec)
        allow = []
        try:
            allow_env = os.getenv('RS_UNIVERSE_ALLOW', '')
            if allow_env:
                allow = [s.strip() for s in allow_env.split(',') if s.strip()]
            if not allow:
                allow = list((cfg.get('universe', {}) or {}).get('allow', []) or [])
        except Exception:
            pass
        all_syms = sorted(set(fetcher.by_base.values()))
        universe = [s for s in all_syms if (not allow or s in allow)]
        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close
            md = {}
            for ccxt_sym in universe:
                feats = read_hour_cache_row(cache_out_path, ccxt_sym, bar_close) if getattr(args, 'hour_cache', 'off') == 'load' else {}
                if not feats:
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=md_tf, limit=max(60, getattr(args, 'limit_klines', 180), int(prewarm_bars or 0)))
                    if df is not None and len(df) >= 2:
                        feats_df = compute_feats(df, tf_seconds=tf_sec)
                        if getattr(args, 'hour_cache', 'off') in ('save', 'load'):
                            try: cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                            except Exception: pass
                        feats = feats_df.iloc[-1].to_dict()
                if not feats:
                    px = fetcher.fetch_ticker_price(ccxt_sym)
                    if px is not None:
                        feats = {'close': float(px), 'open': float(px), 'high': float(px), 'low': float(px), 'volume': 0.0, 'atr_ratio': 0.0, 'dp6h': 0.0, 'dp12h': 0.0, 'quote_volume': 0.0, 'qv_24h': 0.0}
                if feats:
                    feats['datetime_utc'] = bar_close.isoformat(); md[ccxt_sym] = feats
            _sync_pending_entry_orders(fetcher, pending_entries, positions, args.results_dir, session_db_path, bot_id, strat_long, strat_short, bar_close.isoformat(), run_id=run_id)
            for key, rec in list(positions.items()):
                sym, side = split_pos_key(key); row = md.get(sym)
                if row is None:
                    continue
                strat = strat_long if side == 'LONG' else strat_short
                rec2 = rec
                if ENABLE_LOOP_RECONCILE:
                    rec2 = _reconcile_position_with_exchange(fetcher, strat, key, rec, positions, args.results_dir, session_db_path, bot_id, run_id)
                    if rec2 is None:
                        continue
                _maybe_apply_manage_result(fetcher, key, rec2, row, strat, positions, args.results_dir, position_mode, session_db_path, bot_id, run_id, cfg=cfg, pending_entries=pending_entries)
            ranked = list(strat_long.rank(bar_close, md, strat_long.universe(bar_close, md))[:top_n]) if md else []
            for sym in ranked:
                row = md.get(sym)
                if row is None: continue
                _attempt_entry(fetcher, sym, 'LONG', strat_long, row, positions, args.results_dir, position_mode, session_db_path, bot_id, run_id, notional_long=notional_long, notional_short=notional_short)
                _attempt_entry(fetcher, sym, 'SHORT', strat_short, row, positions, args.results_dir, position_mode, session_db_path, bot_id, run_id, notional_long=notional_long, notional_short=notional_short)
            try: write_equity(session_db_path, bot_id, now, _calc_equity_snapshot(fetcher))
            except Exception as e: _dbg('write_equity_failed', str(e))
            _write_exec_metrics(args.results_dir, session_db_path, run_id, event_type='execution_metrics_bar')
            _maybe_update_slippage_calibration(cfg, args.results_dir, session_db_path, cache_out_path, run_id)
            cprint('[live dual]', f'bar={bar_close.isoformat()} open_legs={len(positions)}', fg='cyan', bold=(len(positions) > 0))
        else:
            dot()
        time.sleep(args.poll_sec)
