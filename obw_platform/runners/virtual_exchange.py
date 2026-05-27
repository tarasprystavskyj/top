from __future__ import annotations

import copy
import json
import math
import os
import random
import sqlite3
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd
from slippage_directional_model_v1 import predict_directional_slippage_bp


def _tf_to_seconds(tf: str) -> int:
    tf = str(tf or '1m').strip().lower()
    try:
        n = int(tf[:-1])
    except Exception:
        n = 1
    return n * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}.get(tf[-1], 60)


def _iso_from_ms(ms: int) -> str:
    return pd.to_datetime(ms, unit='ms', utc=True).isoformat()


@dataclass
class _Position:
    symbol: str
    side: str
    qty: float = 0.0
    entry: float = 0.0


class VirtualExchange:
    id = 'virtual'
    name = 'VirtualExchange'

    def __init__(
        self,
        *,
        npz_path: str = '',
        db_path: str = '',
        symbols: Optional[List[str]] = None,
        default_timeframe: str = '1m',
        mode: str = 'hedge',
        initial_balance: float = 1000.0,
        maker_fee: float = 0.0,
        taker_fee: float = 0.0005,
        base_slippage_bps: float = 1.0,
        volume_impact_bps: float = 35.0,
        max_participation: float = 0.15,
        default_quote_volume: float = 25000.0,
        order_ttl_bars: int = 1,
        seed: int = 42,
        error_config: Optional[Dict[str, Any]] = None,
        debug: bool = False,
        dynamic_slippage_model: Optional[Dict[str, Any]] = None,
        broker_id: str = 'generic',
        state_path: str = '',
    ):
        if not npz_path and not db_path:
            raise ValueError('Provide npz_path or db_path')
        self.debug = bool(debug)
        self.mode = str(mode or 'hedge').lower()
        self.timeframes = {'1s': '1s', '15s': '15s', '30s': '30s', '1m': '1m', '5m': '5m', '15m': '15m', '30m': '30m', '1h': '1h', '4h': '4h', '1d': '1d'}
        self.default_timeframe = default_timeframe
        self.enableRateLimit = True
        self.timeout = 0
        self.maker_fee = float(maker_fee)
        self.taker_fee = float(taker_fee)
        self.base_slippage_bps = float(base_slippage_bps)
        self.volume_impact_bps = float(volume_impact_bps)
        self.max_participation = float(max_participation)
        self.default_quote_volume = float(default_quote_volume)
        self.order_ttl_bars = int(order_ttl_bars)
        self.random = random.Random(seed)
        self.error_config = dict(error_config or {})
        self.dynamic_slippage_model = dict(dynamic_slippage_model or {})
        self.broker_id = str(broker_id or 'generic').lower()
        self.data = self._load_data(npz_path=npz_path, db_path=db_path, symbols=symbols)
        self.symbols = sorted(self.data.keys())
        self._cursor = {sym: 0 for sym in self.symbols}
        self._clock_ms = min(int(df['ts'].iloc[0]) for df in self.data.values()) if self.data else int(time.time() * 1000)
        self.markets = self._build_markets(self.symbols)
        self.balance_total = float(initial_balance)
        self.balance_free = float(initial_balance)
        self.positions: Dict[Tuple[str, str], _Position] = {}
        self.orders: Dict[str, Dict[str, Any]] = {}
        self.order_seq = 0
        self.rejections_left = int(self.error_config.get('reject_next_n_orders', 0) or 0)
        self.state_path = str(state_path or '')
        self._loading_state = False
        if self.state_path and os.path.exists(self.state_path):
            self.load_state(self.state_path)

    @classmethod
    def from_env(cls, debug: bool = False) -> 'VirtualExchange':
        err_cfg = {}
        raw = os.getenv('VIRTUAL_EXCHANGE_ERRORS_JSON', '').strip()
        if raw:
            try:
                err_cfg = json.loads(raw)
            except Exception:
                err_cfg = {}
        syms = [s.strip() for s in os.getenv('VIRTUAL_EXCHANGE_SYMBOLS', '').split(',') if s.strip()]
        return cls(
            npz_path=os.getenv('VIRTUAL_EXCHANGE_NPZ', ''),
            db_path=os.getenv('VIRTUAL_EXCHANGE_DB', ''),
            symbols=syms or None,
            default_timeframe=os.getenv('VIRTUAL_EXCHANGE_TF', '1m'),
            mode=os.getenv('VIRTUAL_EXCHANGE_MODE', 'hedge'),
            initial_balance=float(os.getenv('VIRTUAL_EXCHANGE_BALANCE', '1000')),
            maker_fee=float(os.getenv('VIRTUAL_EXCHANGE_MAKER_FEE', '0')),
            taker_fee=float(os.getenv('VIRTUAL_EXCHANGE_TAKER_FEE', '0.0005')),
            base_slippage_bps=float(os.getenv('VIRTUAL_EXCHANGE_BASE_SLIP_BPS', '1.0')),
            volume_impact_bps=float(os.getenv('VIRTUAL_EXCHANGE_VOL_IMPACT_BPS', '35.0')),
            max_participation=float(os.getenv('VIRTUAL_EXCHANGE_MAX_PARTICIPATION', '0.15')),
            default_quote_volume=float(os.getenv('VIRTUAL_EXCHANGE_DEFAULT_QV', '25000.0')),
            order_ttl_bars=int(os.getenv('VIRTUAL_EXCHANGE_ORDER_TTL_BARS', '1')),
            seed=int(os.getenv('VIRTUAL_EXCHANGE_SEED', '42')),
            error_config=err_cfg,
            debug=debug,
            dynamic_slippage_model=(json.loads(os.getenv('VIRTUAL_EXCHANGE_DYNAMIC_SLIPPAGE_JSON', '{}')) if os.getenv('VIRTUAL_EXCHANGE_DYNAMIC_SLIPPAGE_JSON', '').strip() else None),
            broker_id=os.getenv('VIRTUAL_EXCHANGE_BROKER_ID', os.getenv('VIRTUAL_EXCHANGE_EXCHANGE', 'generic')),
            state_path=os.getenv('VIRTUAL_EXCHANGE_STATE_PATH', ''),
        )

    def _log(self, *parts):
        if self.debug:
            print('[virtual-ex]', *parts, flush=True)

    def _parse_symbol(self, sym: str) -> Tuple[str, str]:
        s = str(sym)
        if '/' in s:
            base, rest = s.split('/', 1)
            return base, rest.split(':')[0]
        if s.endswith('USDT'):
            return s[:-4], 'USDT'
        return s, 'USDT'

    def _build_markets(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        out: Dict[str, Dict[str, Any]] = {}
        for sym in symbols:
            base, quote = self._parse_symbol(sym)
            out[sym] = {
                'id': sym.replace('/', '').replace(':', ''),
                'symbol': sym,
                'base': base,
                'quote': quote,
                'active': True,
                'swap': True,
                'future': True,
                'linear': True,
                'precision': {'amount': 0.001, 'price': 0.0001},
                'limits': {'amount': {'min': 0.001, 'step': 0.001}, 'price': {'step': 0.0001}, 'cost': {'min': 1.0}},
                'info': {'positionMode': self.mode, 'tickSize': '0.0001', 'stepSize': '0.001', 'minQty': '0.001'},
            }
        return out

    def _load_data(self, *, npz_path: str, db_path: str, symbols: Optional[List[str]]) -> Dict[str, pd.DataFrame]:
        if npz_path:
            return self._load_from_npz(npz_path, symbols)
        return self._load_from_db(db_path, symbols)

    def _load_from_npz(self, path: str, symbols: Optional[List[str]]) -> Dict[str, pd.DataFrame]:
        z = np.load(path, allow_pickle=True)
        files = set(z.files)
        out: Dict[str, pd.DataFrame] = {}
        def _frame_from_slice(sl: slice):
            base = {
                'ts': z['timestamp_s'][sl].astype('int64') * 1000,
                'open': z['open'][sl].astype('float64') if 'open' in files else z['close'][sl].astype('float64'),
                'high': z['high'][sl].astype('float64') if 'high' in files else z['close'][sl].astype('float64'),
                'low': z['low'][sl].astype('float64') if 'low' in files else z['close'][sl].astype('float64'),
                'close': z['close'][sl].astype('float64'),
                'volume': z['volume'][sl].astype('float64') if 'volume' in files else np.zeros_like(z['close'][sl].astype('float64')),
            }
            reserved = {'symbols','offsets','symbol','timestamp_s','open','high','low','close','volume'}
            for k in files - reserved:
                try:
                    base[k] = z[k][sl].astype('float64')
                except Exception:
                    pass
            return pd.DataFrame(base)
        if {'symbol', 'timestamp_s', 'close'}.issubset(files):
            sym = str(z['symbol'].item() if getattr(z['symbol'], 'shape', ()) == () else z['symbol'][0])
            if symbols and sym not in symbols:
                return {}
            out[sym] = _frame_from_slice(slice(None))
            return out
        req = {'symbols', 'offsets', 'timestamp_s', 'close'}
        if not req.issubset(files):
            raise ValueError(f'Unsupported NPZ format: {sorted(files)}')
        syms = [str(s) for s in z['symbols'].tolist()]
        offs = z['offsets'].astype('int64').tolist()
        if len(offs) == len(syms):
            offs = offs + [len(z['close'])]
        keep = set(symbols or syms)
        for i, sym in enumerate(syms):
            if sym not in keep:
                continue
            out[sym] = _frame_from_slice(slice(offs[i], offs[i+1]))
        return out

    def _load_from_db(self, path: str, symbols: Optional[List[str]]) -> Dict[str, pd.DataFrame]:
        con = sqlite3.connect(path)
        cols = {r[1] for r in con.execute('PRAGMA table_info(price_indicators)').fetchall()}
        select_cols = ['symbol', 'datetime_utc', 'open', 'high', 'low', 'close', 'volume']
        for extra in ['quote_volume', 'qv_24h', 'dp6h', 'dp12h', 'atr_ratio', 'vol_surge_mult']:
            if extra in cols:
                select_cols.append(extra)
        q = 'SELECT ' + ', '.join(select_cols) + ' FROM price_indicators'
        params: List[Any] = []
        if symbols:
            q += ' WHERE symbol IN (%s)' % ','.join(['?'] * len(symbols))
            params.extend(symbols)
        q += ' ORDER BY symbol ASC, datetime_utc ASC'
        df = pd.read_sql_query(q, con, params=params)
        con.close()
        out: Dict[str, pd.DataFrame] = {}
        for sym, part in df.groupby('symbol', sort=True):
            base = {'ts': (pd.to_datetime(part['datetime_utc'], utc=True).astype('int64') // 10**6).astype('int64').to_numpy()}
            for c in part.columns:
                if c in {'symbol', 'datetime_utc'}:
                    continue
                base[c] = part[c].astype('float64').to_numpy()
            out[str(sym)] = pd.DataFrame(base)
        return out

    def export_state(self) -> Dict[str, Any]:
        return {
            'schema': 'virtual_exchange_state_v1',
            'mode': self.mode,
            'default_timeframe': self.default_timeframe,
            'balance_total': self.balance_total,
            'balance_free': self.balance_free,
            'order_seq': self.order_seq,
            'cursor': dict(self._cursor),
            'clock_ms': self._clock_ms,
            'rejections_left': self.rejections_left,
            'orders': copy.deepcopy(self.orders),
            'positions': [
                {'symbol': p.symbol, 'side': p.side, 'qty': p.qty, 'entry': p.entry}
                for p in self.positions.values()
            ],
        }

    def import_state(self, state: Dict[str, Any]) -> None:
        if not isinstance(state, dict):
            raise ValueError('VirtualExchange state must be a dict')
        self._loading_state = True
        try:
            cursor = state.get('cursor') or {}
            for sym, idx in cursor.items():
                resolved = self.resolve_symbol(sym) or sym
                if resolved in self._cursor:
                    n = len(self.data[resolved])
                    self._cursor[resolved] = max(0, min(int(idx), n - 1))
            self._clock_ms = int(state.get('clock_ms') or self._clock_ms)
            self.balance_total = float(state.get('balance_total', self.balance_total))
            self.balance_free = float(state.get('balance_free', self.balance_free))
            self.order_seq = int(state.get('order_seq') or 0)
            self.rejections_left = int(state.get('rejections_left') or 0)
            self.orders = copy.deepcopy(state.get('orders') or {})
            self.positions = {}
            for raw in state.get('positions') or []:
                sym = self.resolve_symbol(str(raw.get('symbol') or '')) or str(raw.get('symbol') or '')
                side = str(raw.get('side') or '').upper()
                if not sym or side not in {'LONG', 'SHORT'}:
                    continue
                qty = float(raw.get('qty') or 0.0)
                if qty <= 0:
                    continue
                self.positions[(sym, side)] = _Position(symbol=sym, side=side, qty=qty, entry=float(raw.get('entry') or 0.0))
        finally:
            self._loading_state = False

    def save_state(self, path: Optional[str] = None) -> None:
        dst = str(path or self.state_path or '')
        if not dst:
            return
        parent = os.path.dirname(dst)
        if parent:
            os.makedirs(parent, exist_ok=True)
        tmp = dst + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self.export_state(), fh, ensure_ascii=False, sort_keys=True)
        os.replace(tmp, dst)

    def load_state(self, path: Optional[str] = None) -> None:
        src = str(path or self.state_path or '')
        if not src:
            return
        with open(src, 'r', encoding='utf-8') as fh:
            state = json.load(fh)
        self.import_state(state)

    def _persist_state(self) -> None:
        if self._loading_state or not self.state_path:
            return
        self.save_state(self.state_path)

    def load_markets(self) -> Dict[str, Dict[str, Any]]:
        return self.markets

    def market(self, symbol: str) -> Dict[str, Any]:
        return self.markets[symbol]

    def parse_timeframe(self, tf: str) -> int:
        return _tf_to_seconds(tf)

    def milliseconds(self) -> int:
        return int(self._clock_ms)

    def resolve_symbol(self, sym: str) -> Optional[str]:
        if sym in self.markets:
            return sym
        u = str(sym).upper().replace('-', '/')
        for cand in (u, u.replace('/USDT', '/USDT:USDT'), u.replace('/USDT:USDT', '/USDT')):
            if cand in self.markets:
                return cand
        base = u.split('/', 1)[0].replace('USDT', '')
        for s in self.symbols:
            if s.startswith(base + '/'):
                return s
        return None

    def _infer_base_tf_seconds(self, symbol: str) -> int:
        df = self.data[symbol]
        if len(df) < 2:
            return _tf_to_seconds(self.default_timeframe)
        diffs = np.diff(df['ts'].to_numpy()[: min(len(df), 1000)])
        diffs = diffs[diffs > 0]
        if len(diffs) == 0:
            return _tf_to_seconds(self.default_timeframe)
        return max(1, int(np.median(diffs) // 1000))

    def set_cursor(self, symbol: str, index: int) -> None:
        sym = self.resolve_symbol(symbol) or symbol
        n = len(self.data[sym])
        self._cursor[sym] = max(0, min(int(index), n - 1))
        self._clock_ms = int(self.data[sym]['ts'].iloc[self._cursor[sym]])
        self._expire_open_orders(sym)
        self._persist_state()

    def advance(self, steps: int = 1, symbol: Optional[str] = None) -> None:
        syms = [self.resolve_symbol(symbol) or symbol] if symbol else list(self.symbols)
        for sym in syms:
            n = len(self.data[sym])
            self._cursor[sym] = max(0, min(int(self._cursor[sym] + steps), n - 1))
            self._clock_ms = max(self._clock_ms, int(self.data[sym]['ts'].iloc[self._cursor[sym]]))
            self._expire_open_orders(sym)
        self._persist_state()

    def current_bar(self, symbol: str) -> Dict[str, Any]:
        sym = self.resolve_symbol(symbol) or symbol
        row = self.data[sym].iloc[self._cursor[sym]]
        out = {'symbol': sym, 'timestamp': int(row['ts']), 'datetime_utc': _iso_from_ms(int(row['ts']))}
        for k, v in row.items():
            if k == 'ts':
                continue
            try:
                out[k] = float(v)
            except Exception:
                out[k] = v
        out.setdefault('open', out.get('close', 0.0)); out.setdefault('high', out.get('close', 0.0)); out.setdefault('low', out.get('close', 0.0)); out.setdefault('volume', 0.0)
        return out

    def fetch_ohlcv(self, symbol: str, timeframe: str = '1m', since: Optional[int] = None, limit: Optional[int] = None):
        sym = self.resolve_symbol(symbol) or symbol
        base = self.data[sym].copy()
        base = base[base['ts'] <= self._clock_ms]
        if since is not None:
            base = base[base['ts'] >= int(since)]
        if base.empty:
            return []
        tf = str(timeframe or self.default_timeframe)
        base_sec = self._infer_base_tf_seconds(sym)
        req_sec = _tf_to_seconds(tf)
        if req_sec < base_sec:
            raise ValueError(f'cannot downsample below source timeframe {base_sec}s -> {req_sec}s')
        if req_sec > base_sec:
            slot_ms = req_sec * 1000
            work = base.copy()
            work['slot'] = (work['ts'] // slot_ms) * slot_ms
            base = work.groupby('slot', as_index=False).agg({'ts': 'max', 'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'})
        if limit is not None:
            base = base.tail(int(limit))
        return [[int(r.ts), float(r.open), float(r.high), float(r.low), float(r.close), float(r.volume)] for r in base.itertuples(index=False)]

    def _synthetic_spread_bps(self, symbol: str) -> float:
        bar = self.current_bar(symbol)
        open_px = max(float(bar.get('open') or bar.get('close') or 0.0), 1e-12)
        range_bp = 10000.0 * (float(bar.get('high') or open_px) - float(bar.get('low') or open_px)) / open_px
        volume = max(float(bar.get('volume') or 0.0), 1.0)
        spread_bp = 0.8 + 0.12 * max(range_bp, 0.0) + 0.25 * (1.0 / max(math.log1p(volume), 1e-6))
        return float(max(0.5, min(spread_bp, 25.0)))

    def fetch_order_book(self, symbol: str, limit: Optional[int] = None):
        sym = self.resolve_symbol(symbol) or symbol
        bar = self.current_bar(sym)
        mid = float(bar['close'])
        spread_bp = self._synthetic_spread_bps(sym)
        spread_abs = mid * spread_bp / 10000.0
        best_bid = max(mid - spread_abs / 2.0, 1e-12)
        best_ask = mid + spread_abs / 2.0
        base_qty = max(float(bar.get('volume') or 0.0) / 40.0, 10.0)
        depth_mult = [1.0, 1.15, 1.3, 1.5, 1.7, 1.9, 2.15, 2.4, 2.7, 3.0]
        lim = int(limit or 10)
        bids = []
        asks = []
        tick = max(mid * 0.0002, 1e-6)
        for i in range(min(lim, len(depth_mult))):
            qty = base_qty * depth_mult[i]
            bids.append([round(best_bid - i * tick, 8), round(qty, 6)])
            asks.append([round(best_ask + i * tick, 8), round(qty, 6)])
        return {'symbol': sym, 'timestamp': bar['timestamp'], 'datetime': _iso_from_ms(bar['timestamp']), 'bids': bids, 'asks': asks, 'nonce': int(bar['timestamp'])}

    def fetch_ticker(self, symbol: str) -> Dict[str, Any]:
        bar = self.current_bar(symbol)
        book = self.fetch_order_book(symbol, 1)
        bid = float(book['bids'][0][0]) if book.get('bids') else bar['close']
        ask = float(book['asks'][0][0]) if book.get('asks') else bar['close']
        return {'symbol': bar['symbol'], 'timestamp': bar['timestamp'], 'datetime': _iso_from_ms(bar['timestamp']), 'last': bar['close'], 'close': bar['close'], 'bid': bid, 'ask': ask, 'info': {'markPrice': bar['close'], 'spreadBps': self._synthetic_spread_bps(symbol)}}

    def fetch_positions(self, symbols: Optional[List[str]] = None):
        rows = []
        keep = set(symbols or self.symbols)
        for (sym, side), pos in sorted(self.positions.items()):
            if sym not in keep or pos.qty <= 0:
                continue
            mark = self.fetch_ticker(sym)['last']
            unreal = (mark - pos.entry) * pos.qty if side == 'LONG' else (pos.entry - mark) * pos.qty
            rows.append({'symbol': sym, 'side': side.lower(), 'contracts': pos.qty, 'entryPrice': pos.entry, 'entry': pos.entry, 'unrealizedPnl': unreal, 'info': {'positionSide': side, 'availableAmt': pos.qty, 'contracts': pos.qty}})
        return rows

    def fetch_balance(self) -> Dict[str, Any]:
        equity = self.balance_free
        total_upnl = sum(float(p.get('unrealizedPnl') or 0.0) for p in self.fetch_positions())
        equity += total_upnl
        return {'total': {'USDT': equity}, 'free': {'USDT': self.balance_free}, 'used': {'USDT': max(0.0, self.balance_total - self.balance_free)}, 'equity': equity, 'info': {'equity': equity, 'total': equity}}

    def fetch_open_orders(self, symbol: Optional[str] = None):
        out = []
        for od in self.orders.values():
            if od['status'] != 'open':
                continue
            if symbol and od['symbol'] != symbol:
                continue
            out.append(copy.deepcopy(od))
        return out

    def create_order(self, symbol: str, type: str, side: str, amount: float, price: Optional[float] = None, params: Optional[Dict[str, Any]] = None):
        params = dict(params or {})
        sym = self.resolve_symbol(symbol) or symbol
        side = str(side).lower()
        order_type = str(type or 'market').lower()
        qty = float(amount or 0.0)
        if qty <= 0:
            raise RuntimeError('amount must be > 0')
        if self.rejections_left > 0:
            self.rejections_left -= 1
            raise RuntimeError('VirtualExchange: injected create_order rejection')
        prob = float(self.error_config.get('reject_probability', 0.0) or 0.0)
        if prob > 0 and self.random.random() < prob:
            raise RuntimeError('VirtualExchange: probabilistic create_order rejection')
        if self.mode.startswith('hedge'):
            ps = str(params.get('positionSide') or '').upper()
            if ps not in {'LONG', 'SHORT'}:
                raise RuntimeError('positionSide required in hedge mode')
        else:
            if params.get('positionSide'):
                raise RuntimeError('positionSide not allowed in one-way mode')
        reduce_only = bool(params.get('reduceOnly'))
        if reduce_only and self.mode.startswith('hedge') and self.error_config.get('simulate_bingx_reduceonly_reject', False):
            raise RuntimeError("bingx {\"code\":109400,\"msg\":\"In the Hedge mode, the 'ReduceOnly' field can not be filled.\",\"data\":{}}")
        order_id = f'vex-{self.order_seq+1:08d}'
        self.order_seq += 1
        ts = self.milliseconds()
        ob = self.fetch_order_book(sym, 10)
        best_bid = float(ob['bids'][0][0]) if ob.get('bids') else None
        best_ask = float(ob['asks'][0][0]) if ob.get('asks') else None
        od = {'id': order_id, 'orderId': order_id, 'clientOrderId': params.get('clientOrderId') or order_id, 'symbol': sym, 'type': order_type, 'side': side, 'amount': qty, 'remaining': qty, 'filled': 0.0, 'average': None, 'price': price, 'status': 'open', 'timestamp': ts, 'datetime': _iso_from_ms(ts), 'reduceOnly': reduce_only, 'info': {'positionSide': params.get('positionSide') or 'BOTH', 'reduceOnly': reduce_only, 'bestBid': best_bid, 'bestAsk': best_ask, 'spreadBps': self._synthetic_spread_bps(sym)}, '_bar_index': self._cursor[sym], '_ttl_bars': self.order_ttl_bars}
        if order_type == 'market':
            fillable, exec_px, slip_bps, reason = self._decide_fill(sym, side, qty, requested_price=price)
            od.update({'remaining': 0.0 if fillable else qty, 'filled': qty if fillable else 0.0, 'average': exec_px if fillable else None, 'price': exec_px if fillable else price, 'status': 'closed' if fillable else 'open'})
            od['info'].update({'slippageBps': slip_bps, 'reason': reason})
            self.orders[order_id] = od
            if fillable:
                fee = abs(exec_px * qty) * self.taker_fee
                self._apply_fill(sym, side=side, qty=qty, px=exec_px, reduce_only=reduce_only, position_side=str(params.get('positionSide') or '').upper() or None)
                self.balance_free -= fee
                od['fee'] = {'cost': fee, 'currency': 'USDT'}
            self._persist_state()
            return copy.deepcopy(od)
        if order_type != 'limit':
            raise RuntimeError(f'unsupported order type: {order_type}')
        if price is None:
            raise RuntimeError('limit order requires price')
        od['price'] = float(price)
        od['info'].update({'reason': 'resting_limit'})
        self.orders[order_id] = od
        self._refresh_open_orders(symbol=sym)
        self._persist_state()
        return copy.deepcopy(self.orders[order_id])

    def fetch_order(self, id: str, symbol: Optional[str] = None):
        od = self.orders[str(id)]
        if od.get('status') == 'open' and str(od.get('type')).lower() == 'limit':
            self._refresh_open_orders(symbol=od['symbol'])
            od = self.orders[str(id)]
            self._persist_state()
        return copy.deepcopy(od)

    def cancel_order(self, id: str, symbol: Optional[str] = None, params: Optional[Dict[str, Any]] = None):
        od = self.orders[str(id)]
        if od['status'] == 'open':
            od['status'] = 'canceled'
            od['remaining'] = od['amount'] - od.get('filled', 0.0)
            od['info']['reason'] = 'canceled'
            self._persist_state()
        return copy.deepcopy(od)

    def _expire_open_orders(self, symbol: str):
        sym = self.resolve_symbol(symbol) or symbol
        cur_idx = self._cursor[sym]
        for od in self.orders.values():
            if od['symbol'] != sym or od['status'] != 'open':
                continue
            age = cur_idx - int(od.get('_bar_index') or cur_idx)
            if age >= int(od.get('_ttl_bars') or 1):
                od['status'] = 'canceled'
                od['info']['reason'] = 'timeout'
                od['remaining'] = max(0.0, float(od['amount']) - float(od.get('filled', 0.0)))
        self._persist_state()

    def _refresh_open_orders(self, symbol: Optional[str] = None) -> None:
        for od in list(self.orders.values()):
            if od.get('status') != 'open' or str(od.get('type')).lower() != 'limit':
                continue
            if symbol is not None and od.get('symbol') != symbol:
                continue
            cur_bar_idx = int(self._cursor[od['symbol']])
            if od.get('_last_refresh_bar_index') == cur_bar_idx:
                continue
            od['_last_refresh_bar_index'] = cur_bar_idx
            bar = self.current_bar(od['symbol'])
            if not self._limit_touched(od, bar):
                continue
            fill_ratio = self._limit_fill_fraction(od, bar)
            fill_qty = min(float(od.get('remaining') or 0.0), max(0.0, float(od.get('remaining') or 0.0) * fill_ratio))
            if fill_qty <= 1e-12:
                continue
            fill_px = float(od.get('price') if od.get('price') is not None else bar.get('close', 0.0))
            self._apply_fill(od['symbol'], side=od['side'], qty=fill_qty, px=fill_px, reduce_only=bool(od.get('reduceOnly')), position_side=str((od.get('info') or {}).get('positionSide') or '').upper() or None)
            self.balance_free -= abs(fill_px * fill_qty) * self.maker_fee
            od['average'] = fill_px
            od['filled'] = float(od.get('filled') or 0.0) + fill_qty
            od['remaining'] = max(0.0, float(od['amount']) - float(od['filled']))
            od['status'] = 'closed' if od['remaining'] <= 1e-12 else 'open'
            od['info'].update({'reason': 'limit_touch_fill', 'fill_price': fill_px, 'last_fill_qty': fill_qty, 'fill_ratio': fill_ratio, 'slippageBps': 0.0})
        self._persist_state()

    def _limit_touched(self, od: Dict[str, Any], bar: Dict[str, Any]) -> bool:
        px = float(od.get('price') if od.get('price') is not None else bar.get('close', 0.0))
        high = float(bar.get('high', bar.get('close', 0.0)))
        low = float(bar.get('low', bar.get('close', 0.0)))
        return low <= px if str(od.get('side')).lower() == 'buy' else high >= px

    def _limit_fill_fraction(self, od: Dict[str, Any], bar: Dict[str, Any]) -> float:
        side_key = 'buy' if str(od.get('side')).lower() == 'buy' else 'sell'
        for key in (f'limit_fill_fraction_{side_key}', 'limit_fill_fraction'):
            if key in bar and bar.get(key) not in (None, ''):
                try:
                    return min(1.0, max(0.0, float(bar.get(key))))
                except Exception:
                    pass
        return 1.0

    def _predict_dynamic_slippage_bps(self, bar: Dict[str, Any], side: str, qty: float, requested_price: float, is_exit: bool = False) -> float:
        if not self.dynamic_slippage_model:
            return 0.0
        kind = str(self.dynamic_slippage_model.get('kind', 'linear_bp'))
        action = 'CLOSE' if is_exit else 'OPEN'
        # convert exchange-side buy/sell + exit/open into strategy-side long/short direction used by model
        if str(side).lower() == 'buy':
            model_side = 'SHORT' if is_exit else 'LONG'
        else:
            model_side = 'LONG' if is_exit else 'SHORT'
        if kind == 'directional_knn_linear':
            row = {
                'open': float(bar.get('open') or bar.get('close') or 0.0),
                'high': float(bar.get('high') or bar.get('close') or 0.0),
                'low': float(bar.get('low') or bar.get('close') or 0.0),
                'close': float(bar.get('close') or 0.0),
                'volume': float(bar.get('volume') or 0.0),
                'quote_volume': max(float(bar.get('close') or 0.0) * float(bar.get('volume') or 0.0), self.default_quote_volume),
            }
            return float(predict_directional_slippage_bp(self.dynamic_slippage_model, row, model_side, action, float(qty)))
        open_px = max(float(bar.get('open') or bar.get('close') or 0.0), 1e-12)
        close_px = float(bar.get('close') or open_px)
        high_px = float(bar.get('high') or close_px)
        low_px = float(bar.get('low') or close_px)
        volume = float(bar.get('volume') or 0.0)
        quote_vol = max(volume * max(close_px, 1e-12), self.default_quote_volume)
        participation = abs(float(qty) * requested_price) / max(quote_vol, 1e-12)
        signed_body_bp = 10000.0 * (close_px - open_px) / open_px
        range_bp = 10000.0 * (high_px - low_px) / open_px
        feats = {
            'log_volume': math.log1p(max(volume, 0.0)),
            'log_quote_volume': math.log1p(max(quote_vol, 0.0)),
            'signed_body_bp': signed_body_bp,
            'range_bp': range_bp,
            'participation': participation,
            'log_participation': math.log(max(participation, 1e-12)),
            'side_long': 1.0 if side == 'buy' else 0.0,
            'side_short': 0.0 if side == 'buy' else 1.0,
            'is_open': 0.0 if is_exit else 1.0,
            'is_exit': 1.0 if is_exit else 0.0,
            'side_x_body_signed_bp': signed_body_bp * (1.0 if side == 'buy' else -1.0),
            'side_x_range_bp': range_bp * (1.0 if side == 'buy' else -1.0),
        }
        val = float(self.dynamic_slippage_model.get('base_bp', 0.0))
        for k, w in (self.dynamic_slippage_model.get('coefficients') or {}).items():
            val += float(w) * float(feats.get(k, 0.0))
        clip_min = float(self.dynamic_slippage_model.get('clip_min_bp', 0.0))
        clip_max = float(self.dynamic_slippage_model.get('clip_max_bp', 1000.0))
        return float(np.clip(val, clip_min, clip_max))


    def _decide_fill(self, symbol: str, side: str, qty: float, requested_price: Optional[float] = None):
        bar = self.current_bar(symbol)
        px0 = float(bar['close']) if requested_price is None else float(requested_price)
        notional = abs(px0 * qty)
        quote_vol = max(float(bar.get('volume') or 0.0) * max(float(bar['close']), 1e-12), self.default_quote_volume)
        participation = 0.0 if quote_vol <= 0 else notional / quote_vol
        if participation > self.max_participation:
            return False, None, None, f'participation {participation:.4f} > {self.max_participation:.4f}'
        slip_bps = self.base_slippage_bps + self.volume_impact_bps * participation
        slip_bps = max(slip_bps, self._predict_dynamic_slippage_bps(bar, side, qty, px0, is_exit=False))
        max_slip = self.error_config.get('max_slippage_bps')
        if max_slip is not None and slip_bps > float(max_slip):
            return False, None, slip_bps, f'slippage {slip_bps:.2f}bp > max {float(max_slip):.2f}bp'
        hi = float(bar['high']); lo = float(bar['low']); close = float(bar['close'])
        if side == 'buy':
            exec_px = min(hi if hi > 0 else close, close * (1.0 + slip_bps / 10000.0))
        else:
            exec_px = max(lo if lo > 0 else close, close * (1.0 - slip_bps / 10000.0))
        return True, exec_px, slip_bps, 'filled'

    def _apply_fill(self, symbol: str, side: str, qty: float, px: float, reduce_only: bool, position_side: Optional[str]):
        side_up = str(position_side or ('LONG' if side == 'buy' else 'SHORT')).upper()
        if reduce_only:
            if self.mode.startswith('hedge'):
                key = (symbol, side_up)
                pos = self.positions.get(key)
                if not pos:
                    return
                close_qty = min(pos.qty, qty)
                pnl = (px - pos.entry) * close_qty if side_up == 'LONG' else (pos.entry - px) * close_qty
                self.balance_free += pnl
                pos.qty -= close_qty
                if pos.qty <= 1e-12:
                    self.positions.pop(key, None)
                else:
                    self.positions[key] = pos
            return
        if self.mode.startswith('hedge'):
            key = (symbol, side_up)
            pos = self.positions.get(key)
            if pos is None or pos.qty <= 0:
                self.positions[key] = _Position(symbol=symbol, side=side_up, qty=qty, entry=px)
            else:
                new_qty = pos.qty + qty
                pos.entry = ((pos.entry * pos.qty) + (px * qty)) / max(new_qty, 1e-12)
                pos.qty = new_qty
                self.positions[key] = pos
