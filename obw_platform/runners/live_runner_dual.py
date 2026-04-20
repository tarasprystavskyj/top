
from __future__ import annotations

import copy, datetime as _dt, importlib, json, logging, math, os, time, uuid
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
ORDER_SYNC_WAIT_SEC = float(os.getenv("ORDER_SYNC_WAIT_SEC", "2.0"))
ORDER_SYNC_POLL_SEC = float(os.getenv("ORDER_SYNC_POLL_SEC", "0.25"))
MAX_ENTRY_SLIP_BP = float(os.getenv("MAX_ENTRY_SLIP_BP", "0"))
MAX_EXIT_SLIP_BP = float(os.getenv("MAX_EXIT_SLIP_BP", "0"))
ENABLE_FALLBACK_TPSL = os.getenv("ENABLE_FALLBACK_TPSL", "0") == "1"


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
def _emit_runtime_debug(session_db_path: str, run_id: str, event_type: str, payload=None, *, level: str = 'INFO', fg: str = 'yellow'):
    try:
        debug_event(session_db_path, run_id, event_type, payload or {}, level=level)
    except Exception:
        pass
    try:
        txt = payload if isinstance(payload, str) else json.dumps(payload or {}, ensure_ascii=False, sort_keys=True)
    except Exception:
        txt = str(payload)
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


def _strategy_sync_after_fill(strat, sym: str, *, qty: float, entry: float, fill_price=None, delta_qty=None, event: str = ''):
    fn = getattr(strat, 'sync_after_external_fill', None)
    if callable(fn):
        try:
            fn(sym, qty=qty, entry=entry, fill_price=fill_price, delta_qty=delta_qty, event=event)
            return
        except Exception:
            pass
    try:
        st = strat._get_state(sym)
        st.pos_size = float(qty)
        st.avg_price = float(entry) if qty > 0 else None
        if hasattr(st, 'pos_value_usdt'):
            st.pos_value_usdt = float(qty) * float(entry) if qty > 0 else 0.0
    except Exception:
        pass


class _PosLike:
    def __init__(self, rec: dict):
        self.qty = float(rec.get('qty', 0.0) or 0.0)
        self.entry = float(rec.get('entry', 0.0) or 0.0)
        self.side = str(rec.get('side', 'LONG')).upper()
        self.tp = rec.get('tp_price')
        self.sl = rec.get('sl_price')


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
    return {'timeframe': str(tf_v), 'top_n': int(top_n_v), 'position_mode': str(position_mode_v), 'notional_long': float(notional_long_v), 'notional_short': float(notional_short_v)}


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
    for p in (pos_list or []):
        try:
            got_sym = fetcher.resolve_symbol(p.get('symbol')) or p.get('symbol')
            if got_sym != ccxt_sym:
                continue
            info = p.get('info', {}) if isinstance(p.get('info'), dict) else {}
            ps = str(info.get('positionSide') or p.get('positionSide') or p.get('side') or '').upper()
            qty = _safe_float(p.get('contracts'))
            if qty is None:
                for k in ('positionAmt', 'positionAmount', 'position', 'size', 'contracts', 'availableAmt', 'holding'):
                    qty = _safe_float(info.get(k))
                    if qty is not None:
                        break
            qty = abs(float(qty or 0.0))
            if qty <= 0:
                continue
            if want_side and ps and not ps.startswith(want_side):
                continue
            entry = _safe_float(p.get('entryPrice')) or _safe_float(p.get('entry')) or _safe_float(info.get('avgPrice')) or _safe_float(info.get('entryPrice')) or 0.0
            return {'symbol': ccxt_sym, 'side': want_side, 'qty': qty, 'entry': float(entry or 0.0)}
        except Exception:
            continue
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
        return {'ok': False, 'error': str(e), 'qty': qty}


def place_reduce_only(fetcher: CCXTFetcher, sym: str, entry_side: str, qty: float, position_mode: str):
    ccxt_sym = fetcher.resolve_symbol(sym) or sym
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
        return {'ok': False, 'error': str(e), 'qty': qty}



def _finalize_open_success(fetcher, strat, rec, *, sym, side, requested_px, qty_requested, ex_order_id, bar_close, fill_px, fill_dt, session_db_path, bot_id, results_dir, positions, run_id, position_mode):
    fill = float(fill_px) if fill_px is not None else float(requested_px)
    adverse_bp = _adverse_slip_bp(side, requested_px, fill, is_close=False)
    if MAX_ENTRY_SLIP_BP > 0 and adverse_bp > MAX_ENTRY_SLIP_BP:
        close_res = place_reduce_only(fetcher, sym, side, qty_requested, position_mode)
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty_requested, status='REVERTED', reason=f'max_entry_slip {adverse_bp:.3f}bp > {MAX_ENTRY_SLIP_BP:.3f}bp', run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'close_res': close_res})
        return False, {'error': 'max_entry_slip'}
    ex_pos = _fetch_exchange_position(fetcher, sym, side)
    qty = float(ex_pos.get('qty') if ex_pos else qty_requested)
    entry = float(ex_pos.get('entry') if ex_pos else fill)
    rec.update({'symbol': sym, 'side': side, 'qty': qty, 'entry': entry, 'ts_open': rec.get('ts_open') or bar_close.isoformat(), 'run_id': run_id, 'order_id': rec.get('order_id') or str(uuid.uuid4()), 'exchange_order_id': ex_order_id, 'entry_fill': fill, 'entry_fill_ts': fill_dt.isoformat() if fill_dt else None, 'entry_slip_bp': _signed_slip_bp(side, requested_px, fill, is_close=False), 'entry_lag_sec': (fill_dt - bar_close).total_seconds() if fill_dt and hasattr(bar_close, 'timestamp') else None, 'entry_mark_price': fetcher.fetch_mark_price(sym)})
    positions[pos_key(sym, side)] = rec
    save_positions(results_dir, positions)
    db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN'})
    _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='FILLED', reason='open', run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'entry': entry})
    _strategy_sync_after_fill(strat, sym, qty=qty, entry=entry, fill_price=fill, delta_qty=qty, event='open')
    try:
        _place_tp_sl_after_open(fetcher, sym, side, qty, rec.get('tp_price'), rec.get('sl_price'), position_mode)
    except Exception:
        pass
    cprint('[open OK]', sym, side, f'qty={qty:.6g} px={entry:.6g}' + (f' id={ex_order_id}' if ex_order_id else ''), fg='green', bold=True)
    _emit_runtime_debug(session_db_path, run_id, 'open_ok', {'symbol': sym, 'side': side, 'qty': qty, 'entry': entry, 'fill': fill, 'order_id': ex_order_id, 'entry_lag_sec': rec.get('entry_lag_sec'), 'entry_slip_bp': rec.get('entry_slip_bp')}, level='INFO', fg='green')
    return True, rec


def _execute_open_with_rollback(fetcher, strat, *, sym, side, requested_px, qty, bar_close, position_mode, snapshot, session_db_path, run_id, bot_id, results_dir, positions, tp_price=None, sl_price=None):
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
    if pre_snapshot and pre_snapshot.get('spread_bp') is not None:
        _emit_runtime_debug(session_db_path, run_id, 'orderbook_pre_open', {'symbol': sym, 'side': side, 'qty': qty, 'spread_bp': pre_snapshot.get('spread_bp'), 'est_sweep_bp': pre_snapshot.get('est_sweep_slip_bp'), 'imbalance': pre_snapshot.get('book_imbalance')}, level='INFO', fg='cyan')
    res = place_open_qty(fetcher, sym, side, qty, position_mode)
    if not res.get('ok'):
        _strategy_restore(strat, sym, snapshot); _strategy_rejected(strat, sym, 'open', res)
        fail_reason = str(res.get('error') or res.get('skip_reason') or 'open_fail')
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='REJECTED', reason=fail_reason, run_id=run_id)
        _emit_runtime_debug(session_db_path, run_id, 'open_fail', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': fail_reason, 'exchange_response': res}, level='ERROR', fg='red')
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
        _strategy_rejected(strat, sym, 'open', {'reason': reason, 'order_id': ex_order_id})
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='OPEN', price=requested_px, qty=qty, status='CANCELED', reason=reason, run_id=run_id, exchange_order_id=ex_order_id)
        _emit_runtime_debug(session_db_path, run_id, 'open_nofill', {'symbol': sym, 'side': side, 'qty': qty, 'requested_px': requested_px, 'reason': reason, 'order_id': ex_order_id, 'order': od, 'pre_snapshot': pre_snapshot}, level='ERROR', fg='red')
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
    rec = {'symbol': sym, 'side': side, 'qty': qty, 'entry': requested_px, 'tp_price': tp_price, 'sl_price': sl_price, 'ts_open': bar_close.isoformat(), 'run_id': run_id, 'order_id': str(uuid.uuid4())}
    return _finalize_open_success(fetcher, strat, rec, sym=sym, side=side, requested_px=requested_px, qty_requested=qty, ex_order_id=ex_order_id, bar_close=bar_close, fill_px=fill_px, fill_dt=fill_dt, session_db_path=session_db_path, bot_id=bot_id, results_dir=results_dir, positions=positions, run_id=run_id, position_mode=position_mode)


def _execute_reduce_with_rollback(fetcher, strat, *, sym, side, qty, requested_px, bar_close, position_mode, snapshot, session_db_path, run_id, close_reason, rec, positions, results_dir, bot_id, event_type, partial=False):
    action_name = 'PARTIAL' if partial else 'CLOSE'
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
        _strategy_restore(strat, sym, snapshot); _strategy_rejected(strat, sym, event_type, res)
        fail_reason = str(res.get('error') or 'close_fail')
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
        _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='CLOSE_PARTIAL' if partial else 'CLOSE', price=requested_px, qty=qty, status='FILLED', reason=_normalize_close_reason(close_reason, 'close'), run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'closed': True})
        _strategy_sync_after_fill(strat, sym, qty=0.0, entry=0.0, fill_price=fill, delta_qty=qty, event='close')
        _emit_runtime_debug(session_db_path, run_id, 'close_ok', {'symbol': sym, 'side': side, 'qty': qty, 'fill': fill, 'closed': True, 'close_reason': _normalize_close_reason(close_reason, 'close')}, level='INFO', fg='green')
        return True, {'closed': True}
    new_qty = float(ex_pos['qty']); new_entry = float(ex_pos['entry'])
    rec.update({'qty': new_qty, 'entry': new_entry, 'exit_fill': fill, 'exit_fill_ts': fill_dt.isoformat() if fill_dt else None, 'exit_slip_bp': _signed_slip_bp(side, requested_px, fill, is_close=True), 'exit_lag_sec': (fill_dt - bar_close).total_seconds() if fill_dt else None, 'exit_mark_price': fetcher.fetch_mark_price(sym)})
    positions[pos_key(sym, side)] = rec; save_positions(results_dir, positions)
    db_upsert_open_position(session_db_path, bot_id, {**rec, 'status': 'OPEN', 'close_reason': _normalize_close_reason(close_reason)})
    _record_order(session_db_path, bar_time=bar_close, symbol=sym, side=side, type_='CLOSE_PARTIAL' if partial else 'CLOSE', price=requested_px, qty=qty, status='FILLED', reason=_normalize_close_reason(close_reason, 'close'), run_id=run_id, exchange_order_id=ex_order_id, extra={'fill': fill, 'remaining_qty': new_qty, 'remaining_entry': new_entry})
    _strategy_sync_after_fill(strat, sym, qty=new_qty, entry=new_entry, fill_price=fill, delta_qty=qty, event='partial' if partial else 'close')
    _emit_runtime_debug(session_db_path, run_id, 'close_ok', {'symbol': sym, 'side': side, 'qty': qty, 'fill': fill, 'closed': False, 'remaining_qty': new_qty, 'remaining_entry': new_entry, 'close_reason': _normalize_close_reason(close_reason, 'close'), 'partial': partial}, level='INFO', fg='green')
    return True, {'closed': False, 'qty': new_qty, 'entry': new_entry}


def _maybe_apply_manage_result(fetcher, key: str, rec: dict, row: dict, strat, positions: dict, results_dir: str, position_mode: str, session_db_path: str, bot_id: str, run_id: str):
    sym, side = split_pos_key(key)
    pos_before = _PosLike(rec)
    qty_before, entry_before = float(pos_before.qty), float(pos_before.entry)
    snapshot = _strategy_snapshot(strat, sym)
    ex_sig = strat.manage_position(sym, row, pos_before, ctx={})
    action = str(getattr(ex_sig, 'action', '') or '').upper()
    reason = _normalize_close_reason(getattr(ex_sig, 'reason', None), action.lower())
    bar_dt = _dt.datetime.fromisoformat(str(row['datetime_utc']).replace('Z', '+00:00'))
    requested_px = float(fetcher.fetch_ticker_price(sym) or row.get('close') or 0.0)
    if action in {'TP', 'SL', 'EXIT'}:
        qty_close = float(rec.get('qty', 0.0) or 0.0)
        if qty_close <= 0:
            _strategy_restore(strat, sym, snapshot); return False
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
        ok, _ = _execute_open_with_rollback(fetcher, strat, sym=sym, side=side, requested_px=requested_px, qty=delta_qty, bar_close=bar_dt, position_mode=position_mode, snapshot=snapshot, session_db_path=session_db_path, run_id=run_id, bot_id=bot_id, results_dir=results_dir, positions=positions, tp_price=rec.get('tp_price'), sl_price=rec.get('sl_price'))
        if ok:
            new_rec = positions[pos_key(sym, side)]
            new_rec['order_id'] = rec.get('order_id') or new_rec.get('order_id')
            positions[pos_key(sym, side)] = new_rec; save_positions(results_dir, positions)
            db_upsert_open_position(session_db_path, bot_id, {**new_rec, 'status': 'OPEN'})
            _strategy_sync_after_fill(strat, sym, qty=float(new_rec['qty']), entry=float(new_rec['entry']), fill_price=_safe_float(new_rec.get('entry_fill'), new_rec.get('entry')), delta_qty=delta_qty, event='dca')
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
    requested_px = float(fetcher.fetch_ticker_price(sym) or row.get('close') or 0.0)
    qty = _compute_entry_qty(sig, side, requested_px, notional_long, notional_short)
    return _execute_open_with_rollback(fetcher, strat, sym=sym, side=side, requested_px=requested_px, qty=qty, bar_close=_dt.datetime.fromisoformat(str(row['datetime_utc']).replace('Z', '+00:00')), position_mode=position_mode, snapshot=snapshot, session_db_path=session_db_path, run_id=run_id, bot_id=bot_id, results_dir=results_dir, positions=positions, tp_price=_sig_get(sig, 'tp'), sl_price=_sig_get(sig, 'sl'))[0]


def run_live(cfg: dict, args):
    global DEBUG_OPEN
    assert ccxt is not None or str(args.exchange).lower() in {'virtual', 'sim', 'virtual_exchange', 'paper_virtual'}, 'ccxt required for LIVE mode'
    if getattr(args, 'env_file', '') and os.path.exists(args.env_file):
        for line in open(args.env_file, 'r', encoding='utf-8').read().splitlines():
            if '=' in line and not line.strip().startswith('#'):
                k, v = line.split('=', 1); os.environ[k.strip()] = v.strip()
    fetcher = CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=getattr(args, 'debug', False))
    strat_long, strat_short = _load_strategy_pair(cfg)
    rcfg = _resolve_runner_cfg(cfg)
    tf, tf_sec, top_n = rcfg['timeframe'], _tf_to_seconds(rcfg['timeframe']), rcfg['top_n']
    notional_long, notional_short, position_mode = rcfg['notional_long'], rcfg['notional_short'], rcfg['position_mode']
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
    if ExchangeTraceProxy is not None and isinstance(fetcher.ex, ExchangeTraceProxy):
        fetcher.ex._scenario_id = run_id
    write_config_snapshot(session_db_path, run_id, cfg)
    DEBUG_OPEN = bool(getattr(args, 'debug', False) or cfg.get('debug_open', False))
    if DEBUG_OPEN:
        try:
            _auth_probe(fetcher, session_db_path, run_id)
        except Exception as e:
            _emit_runtime_debug(session_db_path, run_id, 'auth_probe_fail', {'error': str(e)}, level='ERROR', fg='red')
    try:
        _fit_bootstrap_slippage_model(session_db_path, results_dir, (args.symbol if hasattr(args, 'symbol') else None) or '')
    except Exception:
        pass
    positions = load_positions(args.results_dir)
    bot_id = make_bot_id(args.results_dir, args.exchange, tf)
    try:
        db_positions = db_load_open_positions(session_db_path, bot_id, include_side_in_key=True)
        if db_positions:
            positions = db_positions
    except Exception:
        pass
    last_bar_ts = None
    cprint('[live dual]', f'polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s', fg='cyan')
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
                    df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=tf, limit=max(60, getattr(args, 'limit_klines', 180)))
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
            for key, rec in list(positions.items()):
                sym, side = split_pos_key(key); row = md.get(sym)
                if row is None: continue
                strat = strat_long if side == 'LONG' else strat_short
                _maybe_apply_manage_result(fetcher, key, rec, row, strat, positions, args.results_dir, position_mode, session_db_path, bot_id, run_id)
            ranked = list(strat_long.rank(bar_close, md, strat_long.universe(bar_close, md))[:top_n]) if md else []
            for sym in ranked:
                row = md.get(sym)
                if row is None: continue
                _attempt_entry(fetcher, sym, 'LONG', strat_long, row, positions, args.results_dir, position_mode, session_db_path, bot_id, run_id, notional_long=notional_long, notional_short=notional_short)
                _attempt_entry(fetcher, sym, 'SHORT', strat_short, row, positions, args.results_dir, position_mode, session_db_path, bot_id, run_id, notional_long=notional_long, notional_short=notional_short)
            try: write_equity(session_db_path, bot_id, now, _calc_equity_snapshot(fetcher))
            except Exception as e: _dbg('write_equity_failed', str(e))
            cprint('[live dual]', f'bar={bar_close.isoformat()} open_legs={len(positions)}', fg='cyan', bold=(len(positions) > 0))
        else:
            dot()
        time.sleep(args.poll_sec)
