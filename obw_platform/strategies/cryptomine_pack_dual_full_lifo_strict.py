# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass, field
from collections import deque
import datetime as _dt
import math
import copy
from typing import Any, Dict, List, Optional, Tuple
try:
    from obw_platform.runners.strategy_intents import StrategyRejectionBackoff
except Exception:
    try:
        from runners.strategy_intents import StrategyRejectionBackoff
    except Exception:
        StrategyRejectionBackoff = None

@dataclass
class Sig:
    side: str
    tp: Optional[float] = None
    sl: Optional[float] = None
    reason: str = ""
    qty: Optional[float] = None
    order_type: str = "market"
    limit_price: Optional[float] = None
    maker_fee_rate: Optional[float] = None

@dataclass
class ExitSig:
    action: str
    exit_price: float
    qty_frac: float = 1.0
    reason: str = ""

@dataclass
class _State:
    pos_size: float = 0.0
    pos_value_usdt: float = 0.0
    avg_price: Optional[float] = None
    num_fills: int = 0
    last_fill_price: Optional[float] = None
    next_level_price: Optional[float] = None
    lots: List[Tuple[float, float]] = field(default_factory=list)
    trailing_active: bool = False
    trailing_ref: Optional[float] = None
    reset_pending: bool = False
    cycle_base_qty_coin: Optional[float] = None
    trend_bucket: Optional[str] = None
    trend_htf_closes: deque = field(default_factory=deque)
    trend_ma_series: deque = field(default_factory=deque)
    trend_ma_sum: float = 0.0
    pending_new_entry: Optional[float] = None
    pending_comp_usdt: float = 0.0
    pending_exit_meta: Optional[Dict[str, Any]] = None
    close_history_24h: deque = field(default_factory=deque)
    gain_24h_ready: bool = False
    last_gain_24h_before: float = 0.0

class _PackAdaptiveBase:
    SIDE = 'LONG'
    def __init__(self, cfg: Dict[str, Any], params_key: str):
        self.cfg = cfg
        sp = cfg.get(params_key, {}) or {}
        self.first_qty_coin = float(sp.get('firstBuyQtyCoin' if self.SIDE=='LONG' else 'firstSellQtyCoin', 0.09))
        self.min_order_qty_coin = float(sp.get('minOrderQtyCoin', 0.09))
        self.min_order_usdt = float(sp.get('minOrderUSDT', 0.0))
        # Base order notional for live minimal-size testing.
        # This is NOT a fixed-$2-for-every-order mode:
        # first order = baseOrderUSDT / price;
        # DCA orders still use base_qty * multipliers.
        self.use_base_order_usdt = bool(sp.get('useBaseOrderUSDT', False))
        self.base_order_usdt = float(sp.get('baseOrderUSDT', 0.0))
        # Live minimal-risk sizing mode:
        # useFixedOrderUSDT=true + fixedOrderUSDT=2.0 makes every OPEN/DCA order
        # calculate coin qty from current price: qty = 2.0 / current_price.
        # If applyMultipliersToFixedOrderUSDT=true, DCA multipliers scale the USDT notional;
        # for strict minimum-live testing keep it false.
        self.use_fixed_order_usdt = bool(sp.get('useFixedOrderUSDT', False))
        self.fixed_order_usdt = float(sp.get('fixedOrderUSDT', 0.0))
        self.apply_multipliers_to_fixed_order_usdt = bool(sp.get('applyMultipliersToFixedOrderUSDT', False))
        # Optional maker/limit entry mode. This affects FIRST entries only.
        # DCA can stay market or use existing runner/backtester DCA-limit mode separately.
        self.use_entry_limit_orders = bool(sp.get('useEntryLimitOrders', False))
        self.entry_limit_offset_bp = float(sp.get('entryLimitOffsetBp', 0.0))
        self.entry_limit_post_only = bool(sp.get('entryLimitPostOnly', True))
        self.maker_fee_rate = float((cfg.get('portfolio') or {}).get('maker_fee_rate', (cfg.get('portfolio') or {}).get('fee_rate', 0.0)))
        self.use_equity_pct_base = bool(sp.get('useEquityPctBase', True))
        self.base_order_pct_eq = float(sp.get('baseOrderPctEq', 1.0))
        self.equity_for_sizing = float(sp.get('equityForSizingUSDT', 300.0))
        self.tp_percent = float(sp.get('tpPercent', 0.22))
        self.callback_percent = float(sp.get('callbackPercent', 0.10))
        self.margin_call_limit = int(sp.get('marginCallLimit', 244))
        self.linear_step_percent = float(sp.get('linearDropPercent' if self.SIDE=='LONG' else 'linearRisePercent', 0.16))
        self.auto_merge = bool(sp.get('autoMerge', False))
        self.subsell_tp_percent = float(sp.get('subSellTPPercent', 0.36))
        self.require_close_beyond_full_tp = bool(sp.get('requireCloseAboveFullTP' if self.SIDE=='LONG' else 'requireCloseBelowFullTP', True))
        self.subsell_close_confirm_mode = str(sp.get('subSellCloseConfirmMode' if self.SIDE=='LONG' else 'subCoverCloseConfirmMode', 'breakeven'))
        self.require_close_beyond_dca = bool(sp.get('requireCloseBelowDcaLevel' if self.SIDE=='LONG' else 'requireCloseAboveDcaLevel', True))
        self.block_dca_on_tp_touch = bool(sp.get('blockDcaOnTpTouch', False))
        self.use_high_low_touch = bool(sp.get('useHighLowTouch', True))
        self.max_orders_per_3min = int(sp.get('maxOrdersPer3Min', 14))
        self.step1 = float(sp.get('drop1' if self.SIDE=='LONG' else 'rise1', 0.3))
        self.step2 = float(sp.get('drop2' if self.SIDE=='LONG' else 'rise2', 0.4))
        self.step3 = float(sp.get('drop3' if self.SIDE=='LONG' else 'rise3', 0.6))
        self.step4 = float(sp.get('drop4' if self.SIDE=='LONG' else 'rise4', 0.8))
        self.step5 = float(sp.get('drop5' if self.SIDE=='LONG' else 'rise5', 0.8))
        self.mult2 = float(sp.get('mult2', 1.5))
        self.mult3 = float(sp.get('mult3', 1.0))
        self.mult4 = float(sp.get('mult4', 2.0))
        self.mult5 = float(sp.get('mult5', 3.5))
        self.max_fills_per_bar = int(sp.get('maxFillsPerBar', 6))
        self.max_subsells_per_bar = int(sp.get('maxSubSellsPerBar', 10))
        self.use_live_sync_start = bool(sp.get('useLiveSyncStart', False))
        self.live_start_time = sp.get('liveStartTime', 0)
        self.use_even_bars = bool(sp.get('useEvenBars', True))
        self.use_trend_adaptive_sizing = bool(sp.get('useTrendAdaptiveSizing', True))
        self.trend_ma_tf = str(sp.get('trendMaTf', 'W'))
        self.trend_ma_len = int(sp.get('trendMaLen', 20))
        self.trend_slope_bars = int(sp.get('trendSlopeBars', 3))
        self.trend_slope_long_bound = float(sp.get('trendSlopeLongBoundPct', 1.0))
        self.trend_slope_short_bound = float(sp.get('trendSlopeShortBoundPct', -1.0))
        self.trend_score_min = float(sp.get('trendScoreMinPct', 45.0))
        self.trend_score_max = float(sp.get('trendScoreMaxPct', 75.0))
        self.min_invest_pct = float(sp.get('minLongInvestPct' if self.SIDE=='LONG' else 'minShortInvestPct', sp.get('minShortInvestPct', 0.5)))
        self.max_invest_pct = float(sp.get('maxLongInvestPct' if self.SIDE=='LONG' else 'maxShortInvestPct', sp.get('maxShortInvestPct', 2.0)))
        self.hard_breakeven_deleverage_pct = float(sp.get('hardBreakevenDeleveragePct', 50.0))
        # v18-enabled DCA guard:
        # If gain_24h_before goes against the active leg beyond this threshold,
        # skip new DCA fills for this bar. Existing position stays open.
        # LONG blocks when gain < -threshold; SHORT blocks when gain > +threshold.
        # 0 = disabled. v18 uses LONG=0.10, SHORT=0.08. Values are fractions: 0.10 = 10%.
        self.dca_block_counter_gain_abs = float(sp.get('dcaBlockCounterTrendGainAbs', 0.0))
        # new: carry forward USDT deficits caused by live slippage on closes
        self.enable_slip_loss_comp = bool(sp.get('enableSlipLossCompensation', True))
        self.max_comp_step_pct = float(sp.get('maxCompensationStepPct', 3.0))
        self.comp_roundtrip_fee_rate = float((cfg.get('portfolio') or {}).get('fee_rate', 0.001))
        self.close_fee_rate = float((cfg.get('portfolio') or {}).get('fee_rate', 0.001))
        self.timeframe = str(cfg.get('timeframe', '1m'))
        self._states: Dict[str, _State] = {}
        self._bar_key = None
        self._orders_this_bar = 0
        self._recent_bar_fills = deque()
        self._recent_history_fills = 0
        # Hot-path caches for research/backtest speed.
        self._tf_sec_cached = self._parse_tf_seconds(self.timeframe)
        self._recent_hist_limit = max(0, int(math.ceil(180.0 / max(1, self._tf_sec_cached))) - 1)
        self._trend_tf_key = str(self.trend_ma_tf).strip().upper()
        self._trend_tf_sec_cached = self._parse_tf_seconds(str(self.trend_ma_tf).lower())
        dca_order_type = sp.get('dcaOrderType', sp.get('dca_order_type', cfg.get('dca_open_order_type', 'market')))
        self.dca_order_type = str(dca_order_type or 'market').lower().strip()
        self.allow_entry_market_fallback = bool(sp.get('allowEntryMarketFallback', False))
        self.entry_market_fallback_on_reject = bool(sp.get('entryMarketFallbackOnReject', False))
        self.entry_market_fallback_on_timeout = bool(sp.get('entryMarketFallbackOnTimeout', False))
        self.execution_backoff_max_failures = int(sp.get('executionBackoffMaxFailures', 1))
        self.execution_backoff_cooldown_sec = float(sp.get('executionBackoffCooldownSec', 60.0))
        self._execution_backoffs: Dict[str, Any] = {}
        self._broker_min_order_usdt_by_symbol: Dict[str, Dict[str, Any]] = {}
        self._live_start_epoch_s = None
        if self.use_live_sync_start and self.live_start_time not in (None, 0, '0'):
            try:
                self._live_start_epoch_s = self._epoch_s(self.live_start_time)
            except Exception:
                self._live_start_epoch_s = None

    # --------- state / runner hooks ----------
    _SNAPSHOT_FIELDS = (
        'pos_size', 'pos_value_usdt', 'avg_price', 'num_fills',
        'last_fill_price', 'next_level_price', 'lots',
        'trailing_active', 'trailing_ref', 'reset_pending',
        'cycle_base_qty_coin', 'pending_new_entry',
        'tp_levels_done', 'pending_comp_usdt', 'pending_exit_meta',
    )

    def export_state_snapshot(self, sym: str):
        """Lightweight rollback snapshot for backtester/live order failures.

        Do NOT deepcopy rolling indicator histories here:
        - close_history_24h
        - trend_htf_closes
        - trend_ma_series

        Those deques can contain thousands of bars after warmup and made yearly
        fast backtests crawl. Rollback needs trade/order state, not historical
        feature context.
        """
        st = self._states.get(sym)
        if st is None:
            return None
        snap = {}
        for k in self._SNAPSHOT_FIELDS:
            v = getattr(st, k, None)
            if k in ('lots', 'tp_levels_done'):
                snap[k] = list(v or [])
            elif k == 'pending_exit_meta':
                snap[k] = dict(v or {}) if v else None
            else:
                snap[k] = v
        return snap

    def restore_state_snapshot(self, sym: str, snapshot):
        if snapshot is None:
            self._states.pop(sym, None)
            return
        st = self._get_state(sym)
        # Preserve warmed indicator histories in the current state object.
        for k, v in dict(snapshot).items():
            if k in ('lots', 'tp_levels_done'):
                setattr(st, k, list(v or []))
            elif k == 'pending_exit_meta':
                setattr(st, k, dict(v or {}) if v else None)
            else:
                setattr(st, k, v)

    def on_order_rejected(self, sym: str, event: str = '', details=None):
        st = self._get_state(sym)
        st.pending_exit_meta = None

    def sync_after_external_fill(self, sym: str, *, qty: float, entry: float, fill_price=None, delta_qty=None, event: str = ''):
        st = self._get_state(sym)
        fill = float(fill_price or 0.0 or 0.0)
        ev = str(event or '').lower()

        if ev in ('open', 'dca'):
            st.pos_size = float(qty)
            st.avg_price = float(entry) if qty > 0 else None
            st.pos_value_usdt = float(qty) * float(entry) if qty > 0 else 0.0
            st.pending_exit_meta = None
            return

        meta = dict(st.pending_exit_meta or {})
        st.pos_size = float(qty)
        st.avg_price = float(entry) if qty > 0 else None
        st.pos_value_usdt = float(qty) * float(entry) if qty > 0 else 0.0
        st.pending_exit_meta = None

        if not self.enable_slip_loss_comp or not meta:
            return

        basis = float(meta.get('basis_price') or 0.0)
        qty_close = float(meta.get('qty_close') or 0.0)
        baseline_ref = float(meta.get('baseline_ref_price') or 0.0)
        if basis <= 0 or qty_close <= 0 or fill <= 0 or baseline_ref <= 0:
            return

        if self.SIDE == 'LONG':
            baseline_pnl = (baseline_ref - basis) * qty_close
            actual_pnl = (fill - basis) * qty_close
        else:
            baseline_pnl = (basis - baseline_ref) * qty_close
            actual_pnl = (basis - fill) * qty_close

        # Net of close-side fee. Any outcome better than baseline reduces the accumulated deficit.
        # Any outcome worse than baseline increases it. Because targets are distributed across future exits,
        # we do not force the whole deficit to be recovered in one step.
        actual_pnl_net = actual_pnl - self.comp_roundtrip_fee_rate * fill * qty_close
        over_baseline = max(0.0, actual_pnl_net - baseline_pnl)
        under_baseline = max(0.0, baseline_pnl - actual_pnl_net)
        st.pending_comp_usdt = max(0.0, float(st.pending_comp_usdt) - over_baseline) + under_baseline

    # --------- general helpers ----------
    def universe(self, t, md_map): return list(md_map.keys())
    def rank(self, t, md_map, universe_syms): return list(universe_syms)
    def _get_state(self, sym):
        if sym not in self._states: self._states[sym] = _State()
        return self._states[sym]

    @staticmethod
    def _parse_tf_seconds(tf):
        s = str(tf or '1m').strip().lower()
        if s in ('m', '1m'): return 60
        if s.endswith('m'): return max(1, int(round(float(s[:-1] or 1) * 60)))
        if s in ('h', '1h'): return 3600
        if s.endswith('h'): return max(1, int(round(float(s[:-1] or 1) * 3600)))
        if s in ('d', '1d'): return 86400
        if s.endswith('d'): return max(1, int(round(float(s[:-1] or 1) * 86400)))
        if s in ('w', '1w'): return 604800
        if s.endswith('w'): return max(1, int(round(float(s[:-1] or 1) * 604800)))
        return 60

    def _epoch_s(self, t):
        if isinstance(t, (int, float)):
            return int(t)
        if isinstance(t, _dt.datetime):
            if t.tzinfo:
                return int(t.timestamp())
            return int(t.replace(tzinfo=_dt.timezone.utc).timestamp())
        dt = _dt.datetime.fromisoformat(str(t).replace('Z','+00:00'))
        if dt.tzinfo:
            return int(dt.timestamp())
        return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp())

    def _parse_time(self, t):
        if isinstance(t, _dt.datetime): return t
        if isinstance(t, (int, float)):
            return _dt.datetime.fromtimestamp(int(t), tz=_dt.timezone.utc)
        return _dt.datetime.fromisoformat(str(t).replace('Z','+00:00'))

    def _tf_seconds(self, tf=None):
        if tf is None or str(tf) == str(self.timeframe):
            return self._tf_sec_cached
        return self._parse_tf_seconds(tf)

    def _allow_this_bar(self, t):
        if not self.use_even_bars: return True
        # Anchor parity on epoch seconds. For minute/second bars this is identical
        # to year-anchor parity and avoids constructing datetimes per call.
        return int(self._epoch_s(t) // max(1, self._tf_sec_cached)) % 2 == 0

    def _live_now(self, t):
        if not self.use_live_sync_start or self.live_start_time in (None,0,'0'): return True
        if self._live_start_epoch_s is None:
            return True
        return self._epoch_s(t) >= self._live_start_epoch_s

    def _roll_bar(self, t):
        key = self._epoch_s(t)
        if self._bar_key is None:
            self._bar_key = key
            self._orders_this_bar = 0
            return
        if key != self._bar_key:
            hist_lim = self._recent_hist_limit
            if hist_lim > 0:
                self._recent_bar_fills.append(self._orders_this_bar)
                self._recent_history_fills += self._orders_this_bar
                while len(self._recent_bar_fills) > hist_lim:
                    self._recent_history_fills -= self._recent_bar_fills.popleft()
            else:
                self._recent_bar_fills.clear()
                self._recent_history_fills = 0
            self._orders_this_bar = 0
            self._bar_key = key

    def _can_place_order(self,t):
        self._roll_bar(t)
        return self._live_now(t) and self._allow_this_bar(t) and (self._recent_history_fills + self._orders_this_bar) < self.max_orders_per_3min
    def _register_order(self): self._orders_this_bar += 1
    def get_execution_policy(self, sym, event: str = ''):
        ev = str(event or '').lower()
        if ev == 'dca':
            return {'order_type': self.dca_order_type}
        if ev in {'open', 'entry', 'first'}:
            return {
                'allow_market_fallback': self.allow_entry_market_fallback,
                'fallback_on_reject': self.entry_market_fallback_on_reject,
                'fallback_on_timeout': self.entry_market_fallback_on_timeout,
            }
        return {}
    def get_execution_backoff(self, sym):
        if StrategyRejectionBackoff is None:
            return None
        key = str(sym or '')
        if key not in self._execution_backoffs:
            self._execution_backoffs[key] = StrategyRejectionBackoff(max_failures=self.execution_backoff_max_failures, cooldown_sec=self.execution_backoff_cooldown_sec)
        return self._execution_backoffs[key]
    def can_submit_order(self, sym, event: str = '', details=None):
        if str(event or '').lower() in {'close', 'partial', 'reduce', 'tp', 'sl', 'exit'}:
            return True
        backoff = self.get_execution_backoff(sym)
        return True if backoff is None else bool(backoff.can_submit())
    def can_submit_close_order(self, sym, event: str = '', details=None):
        return True
    def set_broker_min_order_usdt(self, sym: str, value: float, meta=None):
        self._broker_min_order_usdt_by_symbol[str(sym or '')] = {'value': float(value or 0.0), 'meta': dict(meta or {})}
        self.min_order_usdt = max(float(self.min_order_usdt or 0.0), float(value or 0.0))
    def _get_step(self, num):
        nf=num+1
        return self.step1 if nf==2 else self.step2 if nf==3 else self.step3 if nf==4 else self.step4 if nf==5 else self.step5 if nf==6 else self.linear_step_percent
    def _get_mult(self, num):
        nf=num+1
        return self.mult2 if nf==2 else self.mult3 if nf==3 else self.mult4 if nf==4 else self.mult5 if nf==5 else 1.0
    def _next_level(self,last,num):
        return last*(1.0 - self._get_step(num)/100.0) if self.SIDE=='LONG' else last*(1.0 + self._get_step(num)/100.0)
    def _trigger_prices(self,row):
        close=float(row.get('close') or 0.0); op=float(row.get('open', close) or close); hi=float(row.get('high', close) or close); lo=float(row.get('low', close) or close)
        if self.use_high_low_touch: return hi, lo, close
        return max(op, close), min(op, close), close
    def _trend_bucket_id(self, t):
        tf = self._trend_tf_key
        if tf == 'W':
            dt = self._parse_time(t)
            if dt.tzinfo:
                dt = dt.astimezone(_dt.timezone.utc)
            else:
                dt = dt.replace(tzinfo=_dt.timezone.utc)
            iso = dt.isocalendar()
            year = getattr(iso, 'year', iso[0])
            week = getattr(iso, 'week', iso[1])
            return (int(year), int(week))
        if tf == 'D':
            return self._epoch_s(t) // 86400
        return self._epoch_s(t) // max(1, self._trend_tf_sec_cached)

    def _update_trend(self, st, t, close):
        bucket = self._trend_bucket_id(t)
        close = float(close)
        n = int(self.trend_ma_len)

        if st.trend_bucket is None:
            st.trend_bucket = bucket
            st.trend_htf_closes.append(close)
            st.trend_ma_sum += close
        elif bucket != st.trend_bucket:
            # New HTF bucket: append close and drop the old nth-from-end from the rolling window.
            if len(st.trend_htf_closes) >= n and n > 0:
                st.trend_ma_sum -= float(st.trend_htf_closes[-n])
            st.trend_bucket = bucket
            st.trend_htf_closes.append(close)
            st.trend_ma_sum += close
        else:
            # Same HTF bucket: replace the last close by delta. Last close is always inside the MA window.
            if st.trend_htf_closes:
                old_last = float(st.trend_htf_closes[-1])
                st.trend_htf_closes[-1] = close
                st.trend_ma_sum += close - old_last
            else:
                st.trend_htf_closes.append(close)
                st.trend_ma_sum += close

        ma = (st.trend_ma_sum / n) if (n > 0 and len(st.trend_htf_closes) >= n) else None

        st.trend_ma_series.append(ma)
        ma_lim = max(self.trend_slope_bars + 5, 256)
        while len(st.trend_ma_series) > ma_lim:
            st.trend_ma_series.popleft()

        prev = st.trend_ma_series[-1 - self.trend_slope_bars] if len(st.trend_ma_series) > self.trend_slope_bars else None
        slope = ((ma - prev) / prev) * 100.0 if (ma is not None and prev not in (None, 0)) else 0.0
        rng = max(abs(self.trend_slope_long_bound - self.trend_slope_short_bound), 1e-6)
        if self.SIDE == 'SHORT':
            strength = max(0.0, min(100.0, 100.0 * (self.trend_slope_long_bound - slope) / rng))
        else:
            strength = max(0.0, min(100.0, 100.0 * (slope - self.trend_slope_short_bound) / rng))
        score_rng = max(abs(self.trend_score_max - self.trend_score_min), 1e-6)
        factor = max(0.0, min(1.0, (strength - self.trend_score_min) / score_rng))
        return self.min_invest_pct + (self.max_invest_pct - self.min_invest_pct) * factor

    def _cached_target_pct(self, row):
        if not self.use_trend_adaptive_sizing:
            return self.base_order_pct_eq
        if not isinstance(row, dict):
            return None
        keys = ('trend_target_pct_long', 'trend_target_pct_LONG', 'cached_trend_target_pct_long') if self.SIDE=='LONG' else ('trend_target_pct_short', 'trend_target_pct_SHORT', 'cached_trend_target_pct_short')
        for key in keys:
            val = row.get(key)
            if val is None:
                continue
            try:
                f = float(val)
                if math.isfinite(f):
                    return f
            except Exception:
                pass
        return None
    def _qty_for_order_usdt(self, order_usdt, price):
        price = float(price or 0.0)
        if price <= 0:
            return 0.0
        return float(order_usdt or 0.0) / price

    def _calc_base_qty(self, close, target_invest_pct):
        close = float(close or 0.0)
        if close <= 0:
            return 0.0
        if self.use_base_order_usdt and self.base_order_usdt > 0.0:
            raw = self._qty_for_order_usdt(self.base_order_usdt, close)
        elif self.use_fixed_order_usdt and self.fixed_order_usdt > 0.0:
            raw = self._qty_for_order_usdt(self.fixed_order_usdt, close)
        else:
            sizing_pct=target_invest_pct if self.use_trend_adaptive_sizing else self.base_order_pct_eq
            raw=((self.equity_for_sizing*sizing_pct/100.0)/close) if self.use_equity_pct_base else self.first_qty_coin
        min_usdt_qty = self._qty_for_order_usdt(self.min_order_usdt, close) if self.min_order_usdt > 0.0 else 0.0
        return max(float(raw or 0.0), float(self.min_order_qty_coin or 0.0), min_usdt_qty)

    def _calc_dca_qty(self, close, mult, cycle_base_qty_coin=None):
        close = float(close or 0.0)
        if close <= 0:
            return 0.0
        if self.use_fixed_order_usdt and self.fixed_order_usdt > 0.0:
            order_usdt = self.fixed_order_usdt * (float(mult or 1.0) if self.apply_multipliers_to_fixed_order_usdt else 1.0)
            raw = self._qty_for_order_usdt(order_usdt, close)
        else:
            base_qty = cycle_base_qty_coin if cycle_base_qty_coin is not None else self._calc_base_qty(close, 0.0)
            raw = float(base_qty or 0.0) * float(mult or 1.0)
        min_usdt_qty = self._qty_for_order_usdt(self.min_order_usdt, close) if self.min_order_usdt > 0.0 else 0.0
        return max(float(raw or 0.0), float(self.min_order_qty_coin or 0.0), min_usdt_qty)
    def _entry_order_type(self):
        return 'limit' if self.use_entry_limit_orders else 'market'

    def _entry_limit_price(self, price):
        price = float(price or 0.0)
        if price <= 0 or not self.use_entry_limit_orders:
            return None
        off = max(0.0, float(self.entry_limit_offset_bp or 0.0)) / 10000.0
        # LONG opens are buys: place below current price.
        # SHORT opens are sells: place above current price.
        return price * (1.0 - off) if self.SIDE == 'LONG' else price * (1.0 + off)

    def _entry_tp(self, price):
        return price*(1.0+self.tp_percent/100.0) if self.SIDE=='LONG' else price*(1.0-self.tp_percent/100.0)
    def _order_value_ok(self, price, qty):
        return (price * qty) >= self.min_order_usdt - 1e-12

    def _comp_extra_px(self, st, qty_close, total_pos_qty, basis_price):
        if (not self.enable_slip_loss_comp) or qty_close <= 0 or total_pos_qty <= 0 or basis_price <= 0:
            return 0.0
        allocated_usdt = float(st.pending_comp_usdt or 0.0) * min(1.0, float(qty_close) / max(float(total_pos_qty), 1e-12))
        raw = allocated_usdt / max(float(qty_close), 1e-12)
        cap = basis_price * self.max_comp_step_pct / 100.0
        return max(0.0, min(raw, cap))

    def _record_pending_exit(self, st, *, qty_close, basis_price, baseline_ref_price, signal_ref_price):
        if not self.enable_slip_loss_comp:
            st.pending_exit_meta = None
            return
        st.pending_exit_meta = {
            'qty_close': float(qty_close),
            'basis_price': float(basis_price),
            'baseline_ref_price': float(baseline_ref_price),
            'signal_ref_price': float(signal_ref_price),
            'pending_comp_before': float(st.pending_comp_usdt or 0.0),
        }

    def _safe_float(self, value, default=0.0):
        try:
            f = float(value)
            return f if math.isfinite(f) else default
        except Exception:
            return default

    def _update_close_history_and_gain(self, st, row, t, close):
        """Update rolling 24h close history and return gain_24h_before as FRACTION.

        Important: cache builders store gain_24h_before as fraction:
        0.10 means +10%, -0.10 means -10%. v18 thresholds use the same scale.
        """
        row_gain = None
        if isinstance(row, dict):
            for key in (
                'gain_24h_before',
                'gain24h_before',
                'gain_24h',
                'dp24h',
                'change_24h_frac',
            ):
                if key in row and row.get(key) is not None:
                    row_gain = self._safe_float(row.get(key), 0.0)
                    break
            # Some exchange APIs expose percent naming. Convert only for explicit *_pct keys.
            if row_gain is None:
                for key in ('change_24h_pct', 'price_change_24h_pct'):
                    if key in row and row.get(key) is not None:
                        row_gain = self._safe_float(row.get(key), 0.0) / 100.0
                        break

        try:
            ts = float(self._epoch_s(t))
            close_f = float(close)
        except Exception:
            st.last_gain_24h_before = float(row_gain or 0.0)
            st.gain_24h_ready = row_gain is not None
            return st.last_gain_24h_before

        dq = st.close_history_24h
        cutoff = ts - 86400.0

        # Keep one reference bar at/before cutoff, remove older useless bars.
        while len(dq) >= 2 and float(dq[1][0]) <= cutoff:
            dq.popleft()

        local_gain = None
        if dq and float(dq[0][0]) <= cutoff:
            ref_price = float(dq[0][1])
            if ref_price > 0:
                local_gain = close_f / ref_price - 1.0

        if dq and float(dq[-1][0]) == ts:
            dq[-1] = (ts, close_f)
        else:
            dq.append((ts, close_f))

        while len(dq) > 6000:
            dq.popleft()

        if local_gain is not None:
            st.gain_24h_ready = True
            st.last_gain_24h_before = float(local_gain)
        elif row_gain is not None:
            st.gain_24h_ready = True
            st.last_gain_24h_before = float(row_gain)
        else:
            st.gain_24h_ready = False
            st.last_gain_24h_before = 0.0
        return st.last_gain_24h_before

    def _dca_blocked_by_counter_trend(self, st, row, t, close):
        if self.dca_block_counter_gain_abs <= 0.0:
            return False
        gain = self._update_close_history_and_gain(st, row, t, close)
        if not st.gain_24h_ready:
            # Do not invent regime data. Existing positions may still be managed;
            # new entries are blocked separately until warmup is ready.
            return False
        if self.SIDE == 'LONG':
            return gain < -self.dca_block_counter_gain_abs
        return gain > self.dca_block_counter_gain_abs

    def is_warm_ready(self, sym: str) -> bool:
        st = self._get_state(sym)
        if self.dca_block_counter_gain_abs > 0.0 and not bool(getattr(st, 'gain_24h_ready', False)):
            return False
        return True

    def warmup_requirements(self, timeframe_seconds: int = None):
        tf = int(timeframe_seconds or self._tf_seconds())
        bars_24h = max(1, int(math.ceil(24 * 3600 / max(1, tf))))
        need = bars_24h + 2 if self.dca_block_counter_gain_abs > 0.0 else 0
        return {
            'min_bars': int(need),
            'hours': 24 if self.dca_block_counter_gain_abs > 0.0 else 0,
            'reason': 'gain_24h_before for v18 dcaBlockCounterTrendGainAbs' if need else 'none',
        }

    def warmup_history(self, sym: str, rows, ctx=None):
        st = self._get_state(sym)
        count = 0
        for row in rows or []:
            try:
                t = row.get('datetime_utc')
                _, _, close = self._trigger_prices(row)
                self._update_close_history_and_gain(st, row, t, close)
                if self.use_trend_adaptive_sizing:
                    # No orders are produced here. This only warms the same internal trend state
                    # that live manage/entry would otherwise build blindly from startup.
                    self._update_trend(st, t, close)
                count += 1
            except Exception:
                continue
        return {'bars': count, 'ready': self.is_warm_ready(sym), 'last_gain_24h_before': float(getattr(st, 'last_gain_24h_before', 0.0) or 0.0)}

    def _lot_min_close_price(self, entry_price: float, *, tp_percent: float = 0.0) -> float:
        entry_price = float(entry_price or 0.0)
        fee_rate = float(getattr(self, 'close_fee_rate', getattr(self, 'comp_roundtrip_fee_rate', 0.0)) or 0.0)
        tp_frac = float(tp_percent or 0.0) / 100.0
        if self.SIDE == 'LONG':
            return entry_price * (1.0 + fee_rate + tp_frac)
        return entry_price * (1.0 - fee_rate - tp_frac)

    def _lot_breakeven_price(self, entry_price: float) -> float:
        return self._lot_min_close_price(entry_price, tp_percent=0.0)

    def entry_signal(self,is_opening,sym,row,ctx=None):
        t=row.get('ts_s', row.get('timestamp_s', row.get('datetime_utc')))
        if not is_opening or not self._can_place_order(t): return None
        st=self._get_state(sym)
        _,_,close=self._trigger_prices(row)
        _ = self._update_close_history_and_gain(st, row, t, close)
        target_pct=self._cached_target_pct(row)
        if target_pct is None:
            target_pct = self._update_trend(st,t,close) if self.use_trend_adaptive_sizing else self.base_order_pct_eq
        if st.reset_pending or (st.pos_size==0 and len(st.lots)==0):
            if not self.is_warm_ready(sym): return None
            st.reset_pending=False; st.trailing_active=False; st.trailing_ref=None; st.pending_new_entry=None; st.pending_exit_meta=None
            st.cycle_base_qty_coin=self._calc_base_qty(close,target_pct)
            qty0=st.cycle_base_qty_coin; value=qty0*close
            if not self._order_value_ok(close, qty0):
                return None
            st.pos_value_usdt=value; st.pos_size=qty0; st.avg_price=close; st.num_fills=1; st.last_fill_price=close; st.next_level_price=self._next_level(close,1); st.lots=[(qty0,close)]
            self._register_order()
            return Sig(side=self.SIDE, tp=self._entry_tp(close), sl=None, reason='First', qty=qty0, order_type=self._entry_order_type(), limit_price=self._entry_limit_price(close), maker_fee_rate=self.maker_fee_rate)
        return None

    def manage_position(self,sym,row,pos,ctx=None):
        t=row.get('ts_s', row.get('timestamp_s', row.get('datetime_utc'))); self._roll_bar(t)
        if not self._live_now(t) or not self._allow_this_bar(t): return None
        st=self._get_state(sym)
        hi,lo,close=self._trigger_prices(row)
        if self._cached_target_pct(row) is None and self.use_trend_adaptive_sizing:
            _=self._update_trend(st,t,close)
        if st.pos_size>0 and pos.qty is not None and pos.qty>0 and abs(pos.qty-st.pos_size)/max(st.pos_size,1e-12)>1e-6:
            ratio=pos.qty/st.pos_size; st.lots=[(q*ratio,p) for q,p in st.lots]; st.pos_size=float(pos.qty)
        if st.pending_new_entry is not None:
            pos.entry=st.pending_new_entry; st.avg_price=st.pending_new_entry; st.pending_new_entry=None
        if st.avg_price is None: st.avg_price=float(pos.entry)

        max_budget=self.equity_for_sizing
        base_tp_price=self._entry_tp(st.avg_price)
        extra_px_full = self._comp_extra_px(st, float(st.pos_size or 0.0), float(st.pos_size or 0.0), float(st.avg_price or 0.0))
        tp_price = base_tp_price + extra_px_full if self.SIDE=='LONG' else base_tp_price - extra_px_full

        tp_touch=(hi>=tp_price) if self.SIDE=='LONG' else (lo<=tp_price)
        tp_close_ok=((close>=tp_price) if self.SIDE=='LONG' else (close<=tp_price)) if self.require_close_beyond_full_tp else True
        tp_close_confirmed=tp_touch and tp_close_ok
        tp_blocks_dca = tp_touch if self.block_dca_on_tp_touch else tp_close_confirmed
        if tp_touch:
            if self.callback_percent>0:
                st.trailing_active=True
                if self.SIDE=='LONG':
                    st.trailing_ref = hi if st.trailing_ref is None else max(st.trailing_ref, hi)
                    trail_stop = st.trailing_ref*(1.0-self.callback_percent/100.0)
                    fire = tp_close_confirmed and close <= trail_stop
                else:
                    st.trailing_ref = lo if st.trailing_ref is None else min(st.trailing_ref, lo)
                    trail_stop = st.trailing_ref*(1.0+self.callback_percent/100.0)
                    fire = tp_close_confirmed and close >= trail_stop
                if fire and self._can_place_order(t):
                    basis=float(st.avg_price or pos.entry or close); qty_close=float(st.pos_size or pos.qty or 0.0)
                    if not self._order_value_ok(close, qty_close): return None
                    self._record_pending_exit(st, qty_close=qty_close, basis_price=basis, baseline_ref_price=base_tp_price, signal_ref_price=close)
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None
                    self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full (Trailing)')
            else:
                if tp_close_confirmed and self._can_place_order(t):
                    basis=float(st.avg_price or pos.entry or close); qty_close=float(st.pos_size or pos.qty or 0.0)
                    if not self._order_value_ok(close, qty_close): return None
                    self._record_pending_exit(st, qty_close=qty_close, basis_price=basis, baseline_ref_price=base_tp_price, signal_ref_price=close)
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None
                    self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full')
        if not tp_touch: st.trailing_active=False; st.trailing_ref=None
        dca_block_risk = self._dca_blocked_by_counter_trend(st, row, t, close)
        fills=0
        while (not tp_blocks_dca and not dca_block_risk and st.num_fills<self.margin_call_limit and fills<self.max_fills_per_bar and st.next_level_price is not None and ((lo<=st.next_level_price) if self.SIDE=='LONG' else (hi>=st.next_level_price)) and (((not self.require_close_beyond_dca) or (close<=st.next_level_price)) if self.SIDE=='LONG' else ((not self.require_close_beyond_dca) or (close>=st.next_level_price))) and self._can_place_order(t)):
            mult=self._get_mult(st.num_fills)
            if st.cycle_base_qty_coin is None: st.cycle_base_qty_coin=self._calc_base_qty(close,0.0)
            qty_add=self._calc_dca_qty(close, mult, st.cycle_base_qty_coin); value=qty_add*close
            if not self._order_value_ok(close, qty_add): break
            if (st.pos_value_usdt + value) > max_budget: break
            trigger_level=st.next_level_price
            new_value=float(pos.entry)*float(pos.qty) + value
            new_qty=float(pos.qty)+qty_add
            new_entry=new_value/max(new_qty,1e-12)
            pos.qty=new_qty; pos.entry=new_entry
            st.pos_value_usdt += value; st.pos_size=new_qty; st.avg_price=new_entry; st.lots.append((qty_add,close)); st.num_fills += 1; st.last_fill_price=trigger_level; st.next_level_price=self._next_level(trigger_level, st.num_fills); fills += 1; self._register_order(); st.trailing_active=False; st.trailing_ref=None
        subs=0
        while (not tp_blocks_dca and st.num_fills>5 and st.lots and subs<self.max_subsells_per_bar and self._can_place_order(t)):
            qty_last, entry_last = st.lots[-1]
            exposure_pct=((st.pos_size*close)/self.equity_for_sizing*100.0) if self.equity_for_sizing>0 else 0.0
            force_be=exposure_pct > self.hard_breakeven_deleverage_pct
            normal_tp_floor = self._lot_min_close_price(float(entry_last), tp_percent=self.subsell_tp_percent)
            breakeven_floor = self._lot_breakeven_price(float(entry_last))
            extra_px_sub = 0.0 if force_be else self._comp_extra_px(st, float(qty_last), float(st.pos_size or qty_total), float(entry_last))
            if self.SIDE=='LONG':
                touch_level = breakeven_floor if force_be else (normal_tp_floor + extra_px_sub)
            else:
                touch_level = breakeven_floor if force_be else (normal_tp_floor - extra_px_sub)
            touch = (hi>=touch_level) if self.SIDE=='LONG' else (lo<=touch_level)
            confirm_level = touch_level
            close_ok=(close>=confirm_level) if self.SIDE=='LONG' else (close<=confirm_level)
            if not (touch and close_ok): break
            mode = 'deleverage_breakeven' if force_be else 'normal_lot_tp_floor'
            qty_total=float(pos.qty); qty_close=min(float(qty_last), qty_total)
            if qty_total<=0 or qty_close<=0: break
            if not self._order_value_ok(close, qty_close): break
            qty_frac=max(0.0,min(1.0, qty_close/max(qty_total,1e-12)))
            total_cost=sum(q*p for q,p in st.lots)
            profit = qty_close*((close-entry_last) if self.SIDE=='LONG' else (entry_last-close))
            remaining_qty=qty_total-qty_close
            remaining_cost=total_cost - qty_close*entry_last
            if self.auto_merge and remaining_qty>0: remaining_cost -= profit
            self._record_pending_exit(st, qty_close=qty_close, basis_price=float(entry_last), baseline_ref_price=confirm_level, signal_ref_price=close)
            if remaining_qty>0:
                st.pending_new_entry = remaining_cost/max(remaining_qty,1e-12); st.avg_price=st.pending_new_entry
            pos.entry=float(entry_last)
            st.lots.pop(); st.num_fills=max(st.num_fills-1,0); st.pos_size=max(0.0, qty_total-qty_close)
            if st.lots:
                st.last_fill_price=st.lots[-1][1]; st.next_level_price=self._next_level(st.last_fill_price, st.num_fills)
            else:
                st.last_fill_price=None; st.next_level_price=None
            self._register_order(); return ExitSig(action='TP_PARTIAL', exit_price=close, qty_frac=qty_frac, reason=(('Sub-sell last lot' if self.SIDE=='LONG' else 'Sub-cover last lot') + f' | mode={mode} entry={float(entry_last):.8f} min_allowed={float(confirm_level):.8f} qty={float(qty_close):.8f}'))
        return None

class CryptomineLongPackAdaptiveEven(_PackAdaptiveBase):
    SIDE='LONG'
    def __init__(self,cfg): super().__init__(cfg,'strategy_params_long')

class CryptomineShortPackAdaptiveEven(_PackAdaptiveBase):
    SIDE='SHORT'
    def __init__(self,cfg): super().__init__(cfg,'strategy_params_short')
