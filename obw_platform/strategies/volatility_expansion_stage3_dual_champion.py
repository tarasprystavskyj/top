# -*- coding: utf-8 -*-
from __future__ import annotations

from dataclasses import dataclass, field
import datetime as _dt
from typing import Any, Dict, List, Optional


@dataclass
class Sig:
    side: str
    tp: Optional[float] = None
    sl: Optional[float] = None
    reason: str = ""
    qty: Optional[float] = None


@dataclass
class ExitSig:
    action: str
    exit_price: float
    qty_frac: float = 1.0
    reason: str = ""


@dataclass
class _AggBar:
    bucket_start_s: int
    open: float
    high: float
    low: float
    close: float
    volume: float
    quote_volume: float


@dataclass
class _TradeState:
    active: bool = False
    entry_price: Optional[float] = None
    entry_bar_low: Optional[float] = None
    stop_price: Optional[float] = None
    tp_price: Optional[float] = None
    atr_entry: Optional[float] = None
    bars_held: int = 0
    best_low: Optional[float] = None
    trailing_active: bool = False
    current_multiplier: float = 1.0
    pending_entry_setup: Optional[dict] = None


@dataclass
class _State:
    current: Optional[_AggBar] = None
    closes: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    quote_volumes: List[float] = field(default_factory=list)
    true_ranges: List[float] = field(default_factory=list)
    absrets: List[float] = field(default_factory=list)
    ranges_frac: List[float] = field(default_factory=list)
    ema_fast: Optional[float] = None
    ema_slow: Optional[float] = None
    closed_bar_ts: Optional[int] = None
    closed_bar_data: Optional[dict] = None
    processed_entry_bar_ts: Optional[int] = None
    processed_manage_bar_ts: Optional[int] = None
    prev_is_comp: Optional[bool] = None
    wins_streak: int = 0
    last_outcome: Optional[str] = None
    sizing_equity_usdt: Optional[float] = None
    trade: _TradeState = field(default_factory=_TradeState)


class _NoopLongStrategy:
    SIDE = 'LONG'
    def __init__(self, cfg: Dict[str, Any], params_key: str = 'strategy_params_long'):
        self.cfg = cfg
    def universe(self, md_map):
        return list(md_map.keys())
    def rank(self, md_map, universe_syms):
        return list(universe_syms)
    def entry_signal(self, is_opening, sym, row, ctx=None):
        return None
    def manage_position(self, sym, row, pos, ctx=None):
        return None
    def sync_after_external_fill(self, sym, qty, entry, fill_price, delta_qty, event):
        return None


class _ShortStage3Base:
    SIDE = 'SHORT'
    def __init__(self, cfg: Dict[str, Any], params_key: str = 'strategy_params_short'):
        self.cfg = cfg
        sp = cfg.get(params_key, {}) or {}
        pf = cfg.get('portfolio', {}) or {}
        self.signal_tf_sec = self._tf_seconds(str(sp.get('signalTf', sp.get('signalTF', '15m'))))
        self.comp_len = int(sp.get('comp_len', 4))
        self.base_len = int(sp.get('base_len', 24))
        self.brk_len = int(sp.get('brk_len', 8))
        self.vol_mult = float(sp.get('vol_mult', 1.0))
        self.comp_ratio = float(sp.get('comp_ratio', 0.48))
        self.range_ratio = float(sp.get('range_ratio', 1.1))
        self.ema_fast_len = int(sp.get('ema_fast', 20))
        self.ema_slow_len = int(sp.get('ema_slow', 48))
        self.stop_atr = float(sp.get('stop_atr', 2.2))
        self.tp_atr = float(sp.get('tp_atr', 10.5))
        self.max_hold = int(sp.get('max_hold', 48))
        self.trail_activate_frac = float(sp.get('trail_activate_frac', 0.63))
        self.trail_atr = float(sp.get('trail_atr', 2.18))
        self.use_equity_pct_base = bool(sp.get('useEquityPctBase', True))
        self.base_order_pct_eq = float(sp.get('baseOrderPctEq', 100.0))
        self.equity_for_sizing = float(sp.get('equityForSizingUSDT', 100.0))
        self.first_qty_coin = float(sp.get('firstSellQtyCoin', 0.0))
        self.min_order_qty_coin = float(sp.get('minOrderQtyCoin', 0.0))
        self.min_order_usdt = float(sp.get('minOrderUSDT', 0.0))
        self.mult_after_loss = float(sp.get('after_loss', sp.get('after_loss_mult', 0.7)))
        self.mult_after_flat = float(sp.get('after_flat', sp.get('after_flat_mult', 1.0)))
        self.mult_1_win = float(sp.get('1_win', sp.get('after_1_win_mult', 1.3)))
        self.mult_2_wins = float(sp.get('2_wins', sp.get('after_2_wins_mult', 2.0)))
        # Map higher streaks to provided overrides if present, else champion 3x behavior.
        self.mult_3_wins = float(sp.get('3_wins', sp.get('after_3plus_wins_mult', 3.0)))
        self.mult_4_wins = float(sp.get('4_wins', self.mult_3_wins))
        self.mult_5plus_wins = float(sp.get('5plus_wins', self.mult_3_wins))
        self.fee_rate = float(pf.get('fee_rate', sp.get('fee_rate', 0.0)))
        self._states: Dict[str, _State] = {}

    def universe(self, md_map):
        return list(md_map.keys())

    def rank(self, md_map, universe_syms):
        return list(universe_syms)

    def _get_state(self, sym: str) -> _State:
        if sym not in self._states:
            st = _State()
            st.sizing_equity_usdt = float(self.equity_for_sizing)
            self._states[sym] = st
        return self._states[sym]

    def _tf_seconds(self, tf: str) -> int:
        s = str(tf).strip().lower()
        if s.endswith('m'):
            return max(60, int(round(float(s[:-1]) * 60)))
        if s.endswith('h'):
            return max(3600, int(round(float(s[:-1]) * 3600)))
        if s.endswith('d'):
            return max(86400, int(round(float(s[:-1]) * 86400)))
        if s.endswith('w'):
            return 7 * 86400
        return int(s)

    def _bucket_start(self, ts_s: int) -> int:
        return int(ts_s // self.signal_tf_sec) * self.signal_tf_sec

    def _parse_row_ts(self, row: Dict[str, Any]) -> int:
        if 'ts_s' in row:
            return int(row['ts_s'])
        dt = _dt.datetime.fromisoformat(str(row['datetime_utc']).replace('Z', '+00:00'))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        else:
            dt = dt.astimezone(_dt.timezone.utc)
        return int(dt.timestamp())

    def _is_bucket_close_row(self, ts_s: int) -> bool:
        return int(ts_s % self.signal_tf_sec) == (self.signal_tf_sec - 30)

    def _ema_update(self, prev: Optional[float], val: float, length: int) -> float:
        alpha = 2.0 / (float(length) + 1.0)
        if prev is None:
            return float(val)
        return float(alpha * val + (1.0 - alpha) * prev)

    def _mean_last(self, values: List[float], n: int) -> Optional[float]:
        if len(values) < n:
            return None
        return float(sum(values[-n:]) / n)

    def _mean_prev(self, values: List[float], n: int) -> Optional[float]:
        if len(values) <= n:
            return None
        tail = values[-(n + 1):-1]
        return float(sum(tail) / n)

    def _min_prev_low(self, lows: List[float], n: int) -> Optional[float]:
        if len(lows) <= n:
            return None
        return float(min(lows[-(n + 1):-1]))

    def _calc_multiplier(self, st: _State) -> float:
        if st.last_outcome == 'loss':
            return self.mult_after_loss
        wins = int(st.wins_streak)
        if wins <= 0:
            return self.mult_after_flat
        if wins == 1:
            return self.mult_1_win
        if wins == 2:
            return self.mult_2_wins
        if wins == 3:
            return self.mult_3_wins
        if wins == 4:
            return self.mult_4_wins
        return self.mult_5plus_wins

    def _base_qty(self, st: _State, close_px: float) -> float:
        if self.use_equity_pct_base:
            eq = float(st.sizing_equity_usdt or self.equity_for_sizing)
            raw = (eq * self.base_order_pct_eq / 100.0) / max(close_px, 1e-12)
        else:
            raw = self.first_qty_coin
        return max(float(raw), float(self.min_order_qty_coin))

    def _qty_for_entry(self, st: _State, close_px: float) -> float:
        qty = self._base_qty(st, close_px) * self._calc_multiplier(st)
        return float(qty)

    def _order_value_ok(self, price: float, qty: float) -> bool:
        return float(price) * float(qty) >= float(self.min_order_usdt) - 1e-12

    def _ingest_row(self, sym: str, row: Dict[str, Any]) -> _State:
        st = self._get_state(sym)
        ts_s = self._parse_row_ts(row)
        bucket = self._bucket_start(ts_s)
        op = float(row.get('open', row.get('close', 0.0)) or 0.0)
        hi = float(row.get('high', row.get('close', 0.0)) or 0.0)
        lo = float(row.get('low', row.get('close', 0.0)) or 0.0)
        cl = float(row.get('close', 0.0) or 0.0)
        vol = float(row.get('volume', 0.0) or 0.0)
        qv = float(row.get('quote_volume', cl * vol) or (cl * vol))

        if st.current is None or st.current.bucket_start_s != bucket:
            st.current = _AggBar(bucket_start_s=bucket, open=op, high=hi, low=lo, close=cl, volume=vol, quote_volume=qv)
        else:
            st.current.high = max(st.current.high, hi)
            st.current.low = min(st.current.low, lo)
            st.current.close = cl
            st.current.volume += vol
            st.current.quote_volume += qv

        if self._is_bucket_close_row(ts_s) and st.closed_bar_ts != ts_s:
            self._finalize_current_bar(st, ts_s)
        return st

    def _finalize_current_bar(self, st: _State, ts_s: int) -> None:
        bar = st.current
        if bar is None:
            return
        prev_close = st.closes[-1] if st.closes else bar.close
        tr = max(bar.high - bar.low, abs(bar.high - prev_close), abs(bar.low - prev_close))
        absret = abs((bar.close - prev_close) / max(prev_close, 1e-12)) if st.closes else 0.0
        rng_frac = (bar.high - bar.low) / max(bar.close, 1e-12)

        st.closes.append(float(bar.close))
        st.highs.append(float(bar.high))
        st.lows.append(float(bar.low))
        st.quote_volumes.append(float(bar.quote_volume))
        st.true_ranges.append(float(tr))
        st.absrets.append(float(absret))
        st.ranges_frac.append(float(rng_frac))
        st.ema_fast = self._ema_update(st.ema_fast, float(bar.close), self.ema_fast_len)
        st.ema_slow = self._ema_update(st.ema_slow, float(bar.close), self.ema_slow_len)

        comp_short = self._mean_last(st.absrets, self.comp_len)
        comp_base = self._mean_last(st.absrets, self.base_len)
        rng_short = self._mean_last(st.ranges_frac, self.comp_len)
        rng_base = self._mean_last(st.ranges_frac, self.base_len)
        qv_prev = self._mean_prev(st.quote_volumes, self.base_len)
        low_prev = self._min_prev_low(st.lows, self.brk_len)
        atr_cur = self._mean_last(st.true_ranges, self.comp_len)

        is_comp = None
        if None not in (comp_short, comp_base, rng_short, rng_base):
            is_comp = (comp_short / max(comp_base, 1e-12) < self.comp_ratio) and (rng_short / max(rng_base, 1e-12) < self.range_ratio)

        vol_ok = None if qv_prev is None else (bar.quote_volume > qv_prev * self.vol_mult)
        breakout_low = None if low_prev is None else low_prev
        ema_ok = None if (st.ema_fast is None or st.ema_slow is None) else (st.ema_fast < st.ema_slow)

        st.closed_bar_ts = ts_s
        st.closed_bar_data = {
            'ts_s': ts_s,
            'open': float(bar.open),
            'high': float(bar.high),
            'low': float(bar.low),
            'close': float(bar.close),
            'quote_volume': float(bar.quote_volume),
            'atr': None if atr_cur is None else float(atr_cur),
            'is_comp': is_comp,
            'prev_is_comp': st.prev_is_comp,
            'vol_ok': vol_ok,
            'breakout_low': breakout_low,
            'ema_ok': ema_ok,
        }
        st.prev_is_comp = is_comp
        st.current = None

    def _eligible_entry_bar(self, st: _State) -> Optional[dict]:
        bar = st.closed_bar_data
        if not bar:
            return None
        if st.processed_entry_bar_ts == bar['ts_s']:
            return None
        if len(st.closes) < max(self.base_len, self.ema_slow_len, self.brk_len) + 2:
            return None
        return bar

    def _eligible_manage_bar(self, st: _State) -> Optional[dict]:
        bar = st.closed_bar_data
        if not bar:
            return None
        if st.processed_manage_bar_ts == bar['ts_s']:
            return None
        return bar

    def entry_signal(self, is_opening, sym, row, ctx=None):
        if not is_opening:
            return None
        st = self._ingest_row(sym, row)
        trade = st.trade
        bar = self._eligible_entry_bar(st)
        if bar is None or trade.active:
            return None
        st.processed_entry_bar_ts = bar['ts_s']
        if not (
            bar.get('prev_is_comp') is True and
            bar.get('vol_ok') is True and
            bar.get('ema_ok') is True and
            bar.get('breakout_low') is not None and
            float(bar['close']) < float(bar['breakout_low']) and
            bar.get('atr') is not None
        ):
            return None
        qty = self._qty_for_entry(st, float(bar['close']))
        if qty <= 0 or not self._order_value_ok(float(bar['close']), qty):
            return None
        atr = float(bar['atr'])
        trade.pending_entry_setup = {
            'atr_entry': atr,
            'entry_bar_low': float(bar['low']),
            'current_multiplier': self._calc_multiplier(st),
        }
        # return planned levels based on reference close for visibility only; actual levels are bound to fill.
        entry_ref = float(bar['close'])
        return Sig(
            side='SHORT',
            tp=entry_ref - self.tp_atr * atr,
            sl=entry_ref + self.stop_atr * atr,
            reason='stage3_short_entry',
            qty=qty,
        )

    def manage_position(self, sym, row, pos, ctx=None):
        st = self._ingest_row(sym, row)
        trade = st.trade
        bar = self._eligible_manage_bar(st)
        if bar is None or not trade.active or trade.entry_price is None:
            return None
        st.processed_manage_bar_ts = bar['ts_s']
        trade.bars_held += 1
        hi = float(bar['high'])
        lo = float(bar['low'])
        cl = float(bar['close'])

        if trade.best_low is None:
            trade.best_low = lo
        else:
            trade.best_low = min(float(trade.best_low), lo)

        if trade.atr_entry is not None:
            activate_move = self.trail_activate_frac * self.tp_atr * float(trade.atr_entry)
            if (float(trade.entry_price) - float(trade.best_low)) >= activate_move:
                trade.trailing_active = True

        trail_stop = None
        if trade.trailing_active and trade.atr_entry is not None and trade.best_low is not None:
            trail_stop = float(trade.best_low) + self.trail_atr * float(trade.atr_entry)

        # Exact champion ordering: stop -> trail -> TP -> timeout
        if trade.stop_price is not None and hi >= float(trade.stop_price):
            return ExitSig(action='SL', exit_price=float(trade.stop_price), reason='stop_short')
        if trail_stop is not None and hi >= float(trail_stop):
            return ExitSig(action='EXIT', exit_price=float(trail_stop), reason='trail_short')
        if trade.tp_price is not None and lo <= float(trade.tp_price):
            return ExitSig(action='TP', exit_price=float(trade.tp_price), reason='tp_short')
        if trade.bars_held >= self.max_hold:
            return ExitSig(action='EXIT', exit_price=cl, reason='timeout_short')
        return None

    def sync_after_external_fill(self, sym, qty, entry, fill_price, delta_qty, event):
        st = self._get_state(sym)
        trade = st.trade
        ev = str(event)
        if ev == 'open':
            setup = trade.pending_entry_setup or {}
            trade.active = True
            trade.entry_price = float(fill_price)
            trade.entry_bar_low = float(setup.get('entry_bar_low', fill_price)) if setup else float(fill_price)
            trade.atr_entry = float(setup.get('atr_entry', 0.0)) if setup else 0.0
            trade.stop_price = float(fill_price) + self.stop_atr * float(trade.atr_entry)
            trade.tp_price = float(fill_price) - self.tp_atr * float(trade.atr_entry)
            trade.bars_held = 0
            trade.best_low = min(float(fill_price), float(trade.entry_bar_low))
            trade.trailing_active = False
            trade.current_multiplier = float(setup.get('current_multiplier', 1.0))
            trade.pending_entry_setup = None
        elif ev in {'close', 'partial'}:
            was_active = trade.active and trade.entry_price is not None
            if was_active:
                raw_ret = ((float(fill_price) - float(trade.entry_price)) / max(float(trade.entry_price), 1e-12) * -1.0) - 2.0 * self.fee_rate
                sized_ret = raw_ret * float(trade.current_multiplier)
                base_eq = float(st.sizing_equity_usdt or self.equity_for_sizing)
                st.sizing_equity_usdt = max(0.0, base_eq * (1.0 + sized_ret))
                if raw_ret > 0.0:
                    st.last_outcome = 'win'
                    st.wins_streak += 1
                elif raw_ret < 0.0:
                    st.last_outcome = 'loss'
                    st.wins_streak = 0
                else:
                    st.last_outcome = 'flat'
            trade.active = False
            trade.entry_price = None
            trade.entry_bar_low = None
            trade.stop_price = None
            trade.tp_price = None
            trade.atr_entry = None
            trade.bars_held = 0
            trade.best_low = None
            trade.trailing_active = False
            trade.current_multiplier = 1.0
            trade.pending_entry_setup = None


class VolatilityExpansionLongDisabled(_NoopLongStrategy):
    pass


class VolatilityExpansionShortStage3Champion(_ShortStage3Base):
    pass
