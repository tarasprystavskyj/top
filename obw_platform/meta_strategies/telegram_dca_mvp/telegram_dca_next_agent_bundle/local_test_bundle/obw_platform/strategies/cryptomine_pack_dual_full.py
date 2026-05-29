# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from collections import deque
import datetime as _dt
import math
import copy
from typing import Any, Dict, List, Optional, Tuple

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
class _State:
    pos_size: float = 0.0
    pos_value_usdt: float = 0.0   # long cost / short proceeds
    avg_price: Optional[float] = None
    num_fills: int = 0
    last_fill_price: Optional[float] = None
    next_level_price: Optional[float] = None
    lots: List[Tuple[float, float]] = None
    trailing_active: bool = False
    trailing_ref: Optional[float] = None
    reset_pending: bool = False
    cycle_base_qty_coin: Optional[float] = None
    trend_bucket: Optional[str] = None
    trend_htf_closes: deque = None
    trend_ma_series: deque = None
    pending_new_entry: Optional[float] = None
    tp_levels_done: List[bool] = None  # parallel to self.tp_scale_out_levels
    prev_close_vt: Optional[float] = None  # for vol-targeting return calc
    rets_short: deque = None  # rolling log returns (short window)
    rets_long: deque = None  # rolling log returns (baseline window)
    cycle_start_ts: Optional[int] = None  # unix seconds when cycle opened
    last_fill_ts: Optional[int] = None    # unix seconds of last entry/DCA fill

    def __post_init__(self):
        if self.lots is None: self.lots = []
        if self.trend_htf_closes is None: self.trend_htf_closes = deque()
        if self.trend_ma_series is None: self.trend_ma_series = deque()
        if self.tp_levels_done is None: self.tp_levels_done = []
        if self.rets_short is None: self.rets_short = deque()
        if self.rets_long is None: self.rets_long = deque()

class _PackAdaptiveBase:
    SIDE = 'LONG'
    def __init__(self, cfg: Dict[str, Any], params_key: str):
        self.cfg = cfg
        sp = cfg.get(params_key, {}) or {}
        # sizing
        self.first_qty_coin = float(sp.get('firstBuyQtyCoin' if self.SIDE=='LONG' else 'firstSellQtyCoin', 0.09))
        self.min_order_qty_coin = float(sp.get('minOrderQtyCoin', 0.09))
        self.min_order_usdt = float(sp.get('minOrderUSDT', 0.0))
        self.use_equity_pct_base = bool(sp.get('useEquityPctBase', True))
        self.base_order_pct_eq = float(sp.get('baseOrderPctEq', 1.0))
        self.equity_for_sizing = float(sp.get('equityForSizingUSDT', 300.0))
        # core
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
        # ladder
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
        # adaptive sizing
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
        # Regime entry filters (backward-compatible: threshold=0 = disabled)
        self.entry_block_counter_gain_abs = float(sp.get('entryBlockCounterTrendGainAbs', 0.0))
        self.entry_block_counter_dp6h_abs = float(sp.get('entryBlockCounterTrendDp6hAbs', 0.0))
        self.entry_block_vol_surge_max = float(sp.get('entryBlockVolSurgeMax', 0.0))
        self.entry_block_atr_ratio_max = float(sp.get('entryBlockAtrRatioMax', 0.0))
        # Sub-sell start: minimum num_fills before sub-sell exits begin. Default 5 (legacy).
        self.subsell_min_fills = int(sp.get('subSellMinFills', 5))
        # ATR-adaptive DCA step/TP (legacy, default off — wider step hurts DCA mechanics on ENA)
        self.atr_step_mult = float(sp.get('atrStepMult', 0.0))
        self.atr_tp_mult = float(sp.get('atrTpMult', 0.0))
        self._last_atr_ratio = 0.0
        # Hard HTF trend-strength gate for new cycle entry.
        # strength (0-100) is computed same as for trend-adaptive sizing. Block entry when
        # strength < entryTrendStrengthMin. 0 = disabled.
        self.entry_trend_strength_min = float(sp.get('entryTrendStrengthMin', 0.0))
        self._last_trend_strength = 100.0
        # DCA block on counter-trend: if mid-cycle and the row's gain_24h_before is strongly
        # against our side, skip *new DCA fills* this bar. Cycle stays open; existing fills hold.
        # 0 = disabled.
        self.dca_block_counter_gain_abs = float(sp.get('dcaBlockCounterTrendGainAbs', 0.0))
        # DCA block on vol surge: skip new fills when vol_surge > X.
        self.dca_block_vol_surge_max = float(sp.get('dcaBlockVolSurgeMax', 0.0))
        # DCA size dampening: scale qty_add by this factor when num_fills >= dcaDampStartFill.
        # E.g. dcaDampScale=0.5, dcaDampStartFill=3 means fills 4,5,6+ are halved.
        # Reduces tail risk on deep DCA without blocking entry. 1.0=disabled (default).
        self.dca_damp_scale = float(sp.get('dcaDampScale', 1.0))
        self.dca_damp_start_fill = int(sp.get('dcaDampStartFill', 999))
        # closeOnContraryGainPct: when an open cycle exists and |gain_24h_before| exceeds
        # this threshold in the counter-direction (LONG closes when gain<-X, SHORT closes
        # when gain>+X), the entire position is closed at market — accepts realized loss
        # to prevent huge unrealized accumulation in regime breaks (e.g. Jul-25 +113% pump).
        # 0 = disabled.
        self.close_on_contrary_gain = float(sp.get('closeOnContraryGainPct', 0.0))
        # staleCloseHours / staleCloseUnrealPct / staleCloseIdleHours: close stale dead cycles
        # that have been sitting idle and underwater. Frees stuck capital for fresh cycles.
        # ALL conditions must be met to fire close. 0 = disabled.
        # - staleCloseHours: cycle open >= X hours
        # - staleCloseUnrealPct: avg unrealized worse than -X% (positive number, e.g. 25.0)
        # - staleCloseIdleHours: no entry/DCA fill in last Y hours
        # - staleCloseMinFills: only stale-close if num_fills >= N (don't close fresh cycles)
        self.stale_close_hours = float(sp.get('staleCloseHours', 0.0))
        self.stale_close_unreal_pct = float(sp.get('staleCloseUnrealPct', 0.0))
        self.stale_close_idle_hours = float(sp.get('staleCloseIdleHours', 0.0))
        self.stale_close_min_fills = int(sp.get('staleCloseMinFills', 1))
        # tpScaleOutLevels: list of [price_pct_from_avg, qty_frac_of_current] pairs.
        # Example: [[0.20, 0.33], [0.30, 0.33], [0.40, 1.0]] — close 33% at +0.20%, 33% at +0.30%,
        # all remaining at +0.40% (with trailing). Each level fires at most once per cycle.
        # When set (non-empty), replaces the regular full-TP exit. Disabled if empty/None.
        raw_levels = sp.get('tpScaleOutLevels', None)
        self.tp_scale_out_levels: List[Tuple[float, float]] = []
        if raw_levels:
            for pair in raw_levels:
                self.tp_scale_out_levels.append((float(pair[0]), float(pair[1])))
        # Regime-adaptive equity budget: scale equity_for_sizing up/down based on gain_24h_before
        # so this side gets MORE budget when the trend agrees, LESS when it doesn't.
        # regimeBudgetEnable: master switch (False = use base equity, default).
        # regimeFavorableGainAbs: |gain_24h| threshold for "favorable" regime (LONG: +X, SHORT: -X).
        # regimeUnfavorableGainAbs: similar for "unfavorable" (LONG: -X, SHORT: +X).
        # regimeFavorableMult: equity_for_sizing multiplier when favorable (e.g. 1.5).
        # regimeUnfavorableMult: equity_for_sizing multiplier when unfavorable (e.g. 0.6).
        self.regime_budget_enable = bool(sp.get('regimeBudgetEnable', False))
        self.regime_favor_gain_abs = float(sp.get('regimeFavorableGainAbs', 0.05))
        self.regime_unfavor_gain_abs = float(sp.get('regimeUnfavorableGainAbs', 0.05))
        self.regime_favor_mult = float(sp.get('regimeFavorableMult', 1.5))
        self.regime_unfavor_mult = float(sp.get('regimeUnfavorableMult', 0.6))
        self._equity_base_value = self.equity_for_sizing  # snapshot
        # Carver-style volatility targeting: sizing ∝ baseline_vol / current_vol.
        # Compute realized vol on rolling SHORT window, normalize against rolling LONG (baseline) window.
        # Multiplier capped to [min, max]. Applied at new-cycle entry only.
        # 0/None disabled.
        self.vol_target_enable = bool(sp.get('volTargetEnable', False))
        self.vol_target_short_bars = int(sp.get('volTargetShortBars', 120))   # 60 min on 30s bars
        self.vol_target_long_bars = int(sp.get('volTargetLongBars', 5760))    # 2 days on 30s bars
        self.vol_target_min_mult = float(sp.get('volTargetMinMult', 0.5))
        self.vol_target_max_mult = float(sp.get('volTargetMaxMult', 2.0))
        self.timeframe = str(cfg.get('timeframe', '1m'))
        self._states: Dict[str, _State] = {}
        self._bar_key = None
        self._orders_this_bar = 0
        self._recent_bar_fills = deque()
        self._recent_history_fills = 0

    def universe(self, t, md_map): return list(md_map.keys())
    def rank(self, t, md_map, universe_syms): return list(universe_syms)
    def _get_state(self, sym):
        if sym not in self._states: self._states[sym] = _State()
        return self._states[sym]
    def _parse_time(self, t):
        if isinstance(t, _dt.datetime): return t
        return _dt.datetime.fromisoformat(str(t).replace('Z','+00:00'))
    def _tf_seconds(self, tf=None):
        s = str(tf or self.timeframe).strip().lower()
        if s.endswith('m'): return max(1,int(round(float(s[:-1])*60)))
        if s.endswith('h'): return max(1,int(round(float(s[:-1])*3600)))
        if s.endswith('d'): return max(1,int(round(float(s[:-1])*86400)))
        if s.endswith('w'): return 604800
        return 60
    def _allow_this_bar(self, t):
        if not self.use_even_bars: return True
        dt=self._parse_time(t); dt = dt.astimezone(_dt.timezone.utc) if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
        anchor=_dt.datetime(dt.year,1,1,tzinfo=_dt.timezone.utc)
        return int((dt-anchor).total_seconds()//self._tf_seconds()) % 2 == 0
    def _live_now(self, t):
        if not self.use_live_sync_start or self.live_start_time in (None,0,'0'): return True
        return self._parse_time(t) >= self._parse_time(self.live_start_time)
    def _roll_bar(self, t):
        dt=self._parse_time(t); key=int(dt.timestamp())
        if self._bar_key is None:
            self._bar_key=key; self._orders_this_bar=0; return
        if key != self._bar_key:
            hist_lim=max(0,int(math.ceil(180.0/self._tf_seconds()))-1)
            if hist_lim>0:
                self._recent_bar_fills.append(self._orders_this_bar)
                self._recent_history_fills += self._orders_this_bar
                while len(self._recent_bar_fills)>hist_lim:
                    self._recent_history_fills -= self._recent_bar_fills.popleft()
            else:
                self._recent_bar_fills.clear(); self._recent_history_fills=0
            self._orders_this_bar=0; self._bar_key=key
    def _can_place_order(self,t):
        self._roll_bar(t)
        return self._live_now(t) and self._allow_this_bar(t) and (self._recent_history_fills + self._orders_this_bar) < self.max_orders_per_3min
    def _register_order(self): self._orders_this_bar += 1
    def _get_step(self, num):
        nf=num+1
        base = self.step1 if nf==2 else self.step2 if nf==3 else self.step3 if nf==4 else self.step4 if nf==5 else self.step5 if nf==6 else self.linear_step_percent
        if self.atr_step_mult > 0.0:
            base = base * (1.0 + self.atr_step_mult * self._last_atr_ratio)
        return base
    def _get_tp_percent(self):
        base = self.tp_percent
        if self.atr_tp_mult > 0.0:
            base = base * (1.0 + self.atr_tp_mult * self._last_atr_ratio)
        return base
    def _get_mult(self, num):
        nf=num+1
        return self.mult2 if nf==2 else self.mult3 if nf==3 else self.mult4 if nf==4 else self.mult5 if nf==5 else 1.0
    def _next_level(self,last,num):
        return last*(1.0 - self._get_step(num)/100.0) if self.SIDE=='LONG' else last*(1.0 + self._get_step(num)/100.0)
    def _trigger_prices(self,row):
        close=float(row.get('close') or 0.0); op=float(row.get('open', close) or close); hi=float(row.get('high', close) or close); lo=float(row.get('low', close) or close)
        if self.use_high_low_touch: return hi, lo, close
        return max(op, close), min(op, close), close
    def _trend_bucket_id(self, dt):
        dt=dt.astimezone(_dt.timezone.utc) if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
        tf=self.trend_ma_tf.upper()
        if tf=='W':
            iso=dt.isocalendar(); return f'{iso[0]}-W{iso[1]:02d}'
        if tf=='D': return dt.strftime('%Y-%m-%d')
        secs=self._tf_seconds(tf.lower())
        return str(int(dt.timestamp())//secs)
    def _update_trend(self, st, t, close):
        dt=self._parse_time(t)
        bucket=self._trend_bucket_id(dt)
        if st.trend_bucket is None:
            st.trend_bucket=bucket; st.trend_htf_closes.append(close)
        elif bucket!=st.trend_bucket:
            st.trend_bucket=bucket; st.trend_htf_closes.append(close)
        else:
            if st.trend_htf_closes: st.trend_htf_closes[-1]=close
            else: st.trend_htf_closes.append(close)
        vals=list(st.trend_htf_closes)
        ma=sum(vals[-self.trend_ma_len:])/self.trend_ma_len if len(vals)>=self.trend_ma_len else None
        st.trend_ma_series.append(ma)
        while len(st.trend_ma_series) > max(self.trend_slope_bars+5,256): st.trend_ma_series.popleft()
        prev=list(st.trend_ma_series)[-1-self.trend_slope_bars] if len(st.trend_ma_series)>self.trend_slope_bars else None
        slope=((ma-prev)/prev)*100.0 if (ma is not None and prev not in (None,0)) else 0.0
        rng=max(abs(self.trend_slope_long_bound-self.trend_slope_short_bound),1e-6)
        if self.SIDE=='SHORT':
            strength=max(0.0,min(100.0,100.0*(self.trend_slope_long_bound-slope)/rng))
        else:
            strength=max(0.0,min(100.0,100.0*(slope-self.trend_slope_short_bound)/rng))
        self._last_trend_strength = strength
        score_rng=max(abs(self.trend_score_max-self.trend_score_min),1e-6)
        factor=max(0.0,min(1.0,(strength-self.trend_score_min)/score_rng))
        return self.min_invest_pct + (self.max_invest_pct-self.min_invest_pct)*factor
    def _calc_base_qty(self, close, target_invest_pct):
        close = float(close or 0.0)
        if close <= 0:
            return 0.0
        sizing_pct=target_invest_pct if self.use_trend_adaptive_sizing else self.base_order_pct_eq
        raw=((self.equity_for_sizing*sizing_pct/100.0)/close) if self.use_equity_pct_base else self.first_qty_coin
        min_usdt_qty = (self.min_order_usdt / close) if self.min_order_usdt > 0.0 else 0.0
        return max(raw, self.min_order_qty_coin, min_usdt_qty)
    def _apply_regime_budget(self, row):
        if not self.regime_budget_enable:
            return
        gain = float(row.get('gain_24h_before', 0.0) or 0.0)
        base = self._equity_base_value
        if self.SIDE == 'LONG':
            if gain >= self.regime_favor_gain_abs:
                self.equity_for_sizing = base * self.regime_favor_mult
            elif gain <= -self.regime_unfavor_gain_abs:
                self.equity_for_sizing = base * self.regime_unfavor_mult
            else:
                self.equity_for_sizing = base
        else:
            if gain <= -self.regime_favor_gain_abs:
                self.equity_for_sizing = base * self.regime_favor_mult
            elif gain >= self.regime_unfavor_gain_abs:
                self.equity_for_sizing = base * self.regime_unfavor_mult
            else:
                self.equity_for_sizing = base
    def _update_vol_state(self, st, close: float):
        """Append a log return into rolling deques; prune to sizes."""
        if st.prev_close_vt is not None and st.prev_close_vt > 0 and close > 0:
            r = math.log(close / st.prev_close_vt)
            st.rets_short.append(r)
            st.rets_long.append(r)
            while len(st.rets_short) > self.vol_target_short_bars:
                st.rets_short.popleft()
            while len(st.rets_long) > self.vol_target_long_bars:
                st.rets_long.popleft()
        st.prev_close_vt = close
    def _apply_vol_target(self, st):
        if not self.vol_target_enable:
            return
        # need enough data in both windows
        if len(st.rets_short) < max(20, self.vol_target_short_bars // 4):
            return
        if len(st.rets_long) < max(60, self.vol_target_long_bars // 4):
            return
        rs = list(st.rets_short)
        rl = list(st.rets_long)
        ms = sum(rs) / len(rs); ml = sum(rl) / len(rl)
        var_s = sum((x - ms) ** 2 for x in rs) / len(rs)
        var_l = sum((x - ml) ** 2 for x in rl) / len(rl)
        if var_s <= 0 or var_l <= 0:
            return
        sigma_s = var_s ** 0.5
        sigma_l = var_l ** 0.5
        # Carver formula: new_size = base * sigma_baseline / sigma_current
        ratio = sigma_l / sigma_s
        ratio = max(self.vol_target_min_mult, min(self.vol_target_max_mult, ratio))
        # multiply CURRENT equity_for_sizing (preserves regime if also active)
        self.equity_for_sizing = self.equity_for_sizing * ratio
    def _entry_tp(self, price):
        tp = self._get_tp_percent()
        return price*(1.0+tp/100.0) if self.SIDE=='LONG' else price*(1.0-tp/100.0)
    def _order_value_ok(self, price, qty):
        return (price * qty) >= self.min_order_usdt - 1e-12
    def export_state_snapshot(self, sym):
        return copy.deepcopy(self._states.get(sym))

    def restore_state_snapshot(self, sym, snapshot):
        if snapshot is None:
            self._states.pop(sym, None)
        else:
            self._states[sym] = copy.deepcopy(snapshot)

    def is_warm_ready(self, sym):
        st = self._get_state(sym)
        # Trend MA is usable only after enough higher-timeframe buckets.
        # If trend adaptive sizing is disabled, this is still harmless.
        return bool(len(st.trend_htf_closes) >= min(int(self.trend_ma_len), 20))

    def warmup_history(self, sym, rows, ctx=None):
        st = self._get_state(sym)
        n = 0
        for row in rows or []:
            try:
                t = row.get('datetime_utc')
                close = float(row.get('close'))
                if close <= 0:
                    continue
                self._update_vol_state(st, close)
                self._update_trend(st, t, close)
                n += 1
            except Exception:
                continue
        return {'bars_loaded': n, 'ready': self.is_warm_ready(sym)}

    def _recalc_state_from_lots(self, st):
        qty = sum(float(q) for q, _ in st.lots)
        value = sum(float(q) * float(px) for q, px in st.lots)
        st.pos_size = float(qty)
        st.pos_value_usdt = float(value)
        st.avg_price = (value / qty) if qty > 0 else None
        st.num_fills = len(st.lots)
        if st.lots:
            st.last_fill_price = float(st.lots[-1][1])
            st.next_level_price = self._next_level(st.last_fill_price, st.num_fills)
        else:
            st.last_fill_price = None
            st.next_level_price = None
            st.cycle_base_qty_coin = None
            st.trailing_active = False
            st.trailing_ref = None

    def sync_after_external_fill(self, sym, qty: float, entry: float, fill_price=None, delta_qty=None, event: str = ''):
        """Live fill sync for the rollback strategy.

        This keeps the LIFO lot stack consistent with actual exchange fills.
        It is intentionally minimal:
        - open: reset to one actual lot
        - dca/dca_limit_fill: rewrite the latest added lot to actual fill price/qty
        - partial/close/sync: trust strategy's LIFO pop/close logic, then align aggregate qty/entry
        """
        st = self._get_state(sym)
        q = float(qty or 0.0)
        px = float(fill_price or entry or 0.0)
        ev = str(event or '').lower()
        dq = None
        try:
            dq = float(delta_qty) if delta_qty is not None else None
        except Exception:
            dq = None

        if q <= 0 or ev in ('close', 'sync_absent'):
            st.lots = []
            self._recalc_state_from_lots(st)
            return

        if ev == 'open':
            st.lots = [(q, px)]
            st.cycle_base_qty_coin = q
            self._recalc_state_from_lots(st)
            return

        if ev in ('dca', 'dca_limit_fill'):
            # Strategy already appended the DCA lot at signal price. Replace that latest lot
            # by actual delta qty / fill price when possible.
            if dq is not None and dq > 0:
                if st.lots:
                    st.lots[-1] = (dq, px)
                else:
                    st.lots.append((dq, px))
            self._recalc_state_from_lots(st)
            # If exchange says remaining qty differs slightly, scale lots.
            if q > 0 and abs(st.pos_size - q) / max(q, 1e-12) > 1e-5 and st.pos_size > 0:
                ratio = q / st.pos_size
                st.lots = [(float(lq) * ratio, lp) for lq, lp in st.lots]
                self._recalc_state_from_lots(st)
            return

        # partial: strategy already popped exact LIFO lot before order submit.
        # Align aggregate to exchange without reconstructing old closed lot.
        if q > 0 and st.lots:
            if abs(st.pos_size - q) / max(q, 1e-12) > 1e-5 and st.pos_size > 0:
                ratio = q / st.pos_size
                st.lots = [(float(lq) * ratio, lp) for lq, lp in st.lots]
            self._recalc_state_from_lots(st)
            return

        st.pos_size = q
        st.avg_price = float(entry or px)
        st.pos_value_usdt = q * st.avg_price

    def entry_signal(self,is_opening,sym,row,ctx=None):
        t=row.get('datetime_utc')
        if not is_opening or not self._can_place_order(t): return None
        self._last_atr_ratio = float(row.get('atr_ratio', 0.0) or 0.0)
        self._apply_regime_budget(row)
        st=self._get_state(sym)
        _,_,close=self._trigger_prices(row)
        self._update_vol_state(st, close)
        self._apply_vol_target(st)
        target_pct=self._update_trend(st,t,close)
        # Regime entry filters — skip new cycle if market regime is hostile to this side.
        if self.entry_block_counter_gain_abs > 0.0:
            gain = float(row.get('gain_24h_before', 0.0) or 0.0)
            if self.SIDE == 'LONG' and gain < -self.entry_block_counter_gain_abs: return None
            if self.SIDE == 'SHORT' and gain > self.entry_block_counter_gain_abs: return None
        if self.entry_block_counter_dp6h_abs > 0.0:
            dp6 = float(row.get('dp6h', 0.0) or 0.0)
            if self.SIDE == 'LONG' and dp6 < -self.entry_block_counter_dp6h_abs: return None
            if self.SIDE == 'SHORT' and dp6 > self.entry_block_counter_dp6h_abs: return None
        if self.entry_block_vol_surge_max > 0.0:
            vs = float(row.get('vol_surge_mult', 0.0) or 0.0)
            if vs >= self.entry_block_vol_surge_max: return None
        if self.entry_block_atr_ratio_max > 0.0:
            ar = float(row.get('atr_ratio', 0.0) or 0.0)
            if ar >= self.entry_block_atr_ratio_max: return None
        if self.entry_trend_strength_min > 0.0 and self._last_trend_strength < self.entry_trend_strength_min:
            return None
        if st.reset_pending or (st.pos_size==0 and len(st.lots)==0):
            st.reset_pending=False; st.trailing_active=False; st.trailing_ref=None; st.pending_new_entry=None
            st.cycle_base_qty_coin=self._calc_base_qty(close,target_pct)
            st.tp_levels_done = [False] * len(self.tp_scale_out_levels)
            qty0=st.cycle_base_qty_coin; value=qty0*close
            if not self._order_value_ok(close, qty0):
                return None
            st.pos_value_usdt=value; st.pos_size=qty0; st.avg_price=close; st.num_fills=1; st.last_fill_price=close; st.next_level_price=self._next_level(close,1); st.lots=[(qty0,close)]
            try:
                _ts_now = int(self._parse_time(t).timestamp())
            except Exception:
                _ts_now = 0
            st.cycle_start_ts = _ts_now
            st.last_fill_ts = _ts_now
            self._register_order()
            return Sig(side=self.SIDE, tp=self._entry_tp(close), sl=None, reason='First', qty=qty0)
        return None
    def manage_position(self,sym,row,pos,ctx=None):
        t=row.get('datetime_utc'); self._roll_bar(t)
        if not self._live_now(t) or not self._allow_this_bar(t): return None
        self._last_atr_ratio = float(row.get('atr_ratio', 0.0) or 0.0)
        # Reset equity to base during manage so DCA uses base sizing (cycle_base_qty fixed at entry)
        if self.regime_budget_enable or self.vol_target_enable:
            self.equity_for_sizing = self._equity_base_value
        st=self._get_state(sym)
        # Update rolling vol windows from current close (for next-bar entry decisions)
        if self.vol_target_enable:
            _, _, _close_now = self._trigger_prices(row)
            self._update_vol_state(st, _close_now)
        hi,lo,close=self._trigger_prices(row); _=self._update_trend(st,t,close)
        if st.pos_size>0 and pos.qty is not None and pos.qty>0 and abs(pos.qty-st.pos_size)/max(st.pos_size,1e-12)>1e-6:
            ratio=pos.qty/st.pos_size; st.lots=[(q*ratio,p) for q,p in st.lots]; st.pos_size=float(pos.qty)
        if st.pending_new_entry is not None:
            pos.entry=st.pending_new_entry; st.avg_price=st.pending_new_entry; st.pending_new_entry=None
        if st.avg_price is None: st.avg_price=float(pos.entry)
        # Stale-bag close: close dead idle cycles that won't recover, freeing stuck capital
        # for fresh cycles at current prices. ALL conditions must be true to fire.
        if self.stale_close_hours > 0.0 and st.lots and st.cycle_start_ts is not None and self._can_place_order(t):
            try:
                _ts_now = int(self._parse_time(t).timestamp())
            except Exception:
                _ts_now = 0
            cycle_age_h = (_ts_now - (st.cycle_start_ts or _ts_now)) / 3600.0
            idle_h = (_ts_now - (st.last_fill_ts or st.cycle_start_ts or _ts_now)) / 3600.0
            unr_pct = 0.0
            if st.avg_price and st.avg_price > 0:
                if self.SIDE == 'LONG':
                    unr_pct = (close - st.avg_price) / st.avg_price * 100.0
                else:
                    unr_pct = (st.avg_price - close) / st.avg_price * 100.0
            if (cycle_age_h >= self.stale_close_hours
                and idle_h >= self.stale_close_idle_hours
                and unr_pct <= -self.stale_close_unreal_pct
                and st.num_fills >= self.stale_close_min_fills):
                st.reset_pending = True
                st.pos_value_usdt = 0.0; st.pos_size = 0.0; st.avg_price = None
                st.num_fills = 0; st.last_fill_price = None; st.next_level_price = None
                st.lots = []; st.trailing_active = False; st.trailing_ref = None
                st.cycle_start_ts = None; st.last_fill_ts = None
                self._register_order()
                return ExitSig(action='EXIT', exit_price=close, reason=f'Stale bag age={cycle_age_h:.0f}h idle={idle_h:.0f}h unr={unr_pct:.1f}%')
        # Emergency close on extreme contrary regime — accept realized loss to cap MTM bleed.
        if self.close_on_contrary_gain > 0.0 and st.lots and self._can_place_order(t):
            gain = float(row.get('gain_24h_before', 0.0) or 0.0)
            contrary = (self.SIDE == 'LONG' and gain < -self.close_on_contrary_gain) or \
                       (self.SIDE == 'SHORT' and gain > self.close_on_contrary_gain)
            if contrary:
                st.reset_pending=True
                st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None
                st.num_fills=0; st.last_fill_price=None; st.next_level_price=None
                st.lots=[]; st.trailing_active=False; st.trailing_ref=None
                self._register_order()
                return ExitSig(action='EXIT', exit_price=close, reason='Contrary regime')
        # DCA counter-trend / vol-surge guard: lifts dca_block_risk this bar only.
        dca_block_risk = False
        if self.dca_block_counter_gain_abs > 0.0:
            gain = float(row.get('gain_24h_before', 0.0) or 0.0)
            if self.SIDE == 'LONG' and gain < -self.dca_block_counter_gain_abs: dca_block_risk = True
            if self.SIDE == 'SHORT' and gain > self.dca_block_counter_gain_abs: dca_block_risk = True
        if not dca_block_risk and self.dca_block_vol_surge_max > 0.0:
            vs = float(row.get('vol_surge_mult', 0.0) or 0.0)
            if vs >= self.dca_block_vol_surge_max: dca_block_risk = True
        # Scale-out TP: fire each level once, partial close until last level closes all.
        if self.tp_scale_out_levels and st.lots and len(st.tp_levels_done) == len(self.tp_scale_out_levels):
            for i, (lvl_pct, lvl_frac) in enumerate(self.tp_scale_out_levels):
                if st.tp_levels_done[i]:
                    continue
                tgt = st.avg_price*(1.0+lvl_pct/100.0) if self.SIDE=='LONG' else st.avg_price*(1.0-lvl_pct/100.0)
                touched = (hi >= tgt) if self.SIDE=='LONG' else (lo <= tgt)
                close_ok = (close >= tgt) if self.SIDE=='LONG' else (close <= tgt)
                if touched and close_ok and self._can_place_order(t):
                    st.tp_levels_done[i] = True
                    is_last = (i == len(self.tp_scale_out_levels)-1) or lvl_frac >= 0.999
                    if is_last:
                        st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0
                        st.avg_price=None; st.num_fills=0; st.last_fill_price=None
                        st.next_level_price=None; st.lots=[]
                        st.trailing_active=False; st.trailing_ref=None
                        self._register_order()
                        return ExitSig(action='TP', exit_price=close, reason=f'Scale-out L{i+1} full')
                    qty_frac_close = max(0.0, min(1.0, lvl_frac))
                    self._register_order()
                    return ExitSig(action='TP_PARTIAL', exit_price=close, qty_frac=qty_frac_close, reason=f'Scale-out L{i+1}')
        max_budget=self.equity_for_sizing
        scale_out_active = bool(self.tp_scale_out_levels)
        tp_price=self._entry_tp(st.avg_price)
        tp_touch=(hi>=tp_price) if self.SIDE=='LONG' else (lo<=tp_price)
        tp_close_ok=((close>=tp_price) if self.SIDE=='LONG' else (close<=tp_price)) if self.require_close_beyond_full_tp else True
        tp_close_confirmed=tp_touch and tp_close_ok
        tp_blocks_dca = tp_touch if self.block_dca_on_tp_touch else tp_close_confirmed
        if tp_touch and not scale_out_active:
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
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None; self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full (Trailing)')
            else:
                if tp_close_confirmed and self._can_place_order(t):
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None; self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full')
        if not tp_touch: st.trailing_active=False; st.trailing_ref=None
        fills=0
        while (not tp_blocks_dca and not dca_block_risk and st.num_fills<self.margin_call_limit and fills<self.max_fills_per_bar and st.next_level_price is not None and ((lo<=st.next_level_price) if self.SIDE=='LONG' else (hi>=st.next_level_price)) and (((not self.require_close_beyond_dca) or (close<=st.next_level_price)) if self.SIDE=='LONG' else ((not self.require_close_beyond_dca) or (close>=st.next_level_price))) and self._can_place_order(t)):
            mult=self._get_mult(st.num_fills)
            if st.cycle_base_qty_coin is None: st.cycle_base_qty_coin=self._calc_base_qty(close,0.0)
            damp = self.dca_damp_scale if (self.dca_damp_scale < 1.0 and st.num_fills >= self.dca_damp_start_fill) else 1.0
            qty_add=max(st.cycle_base_qty_coin*mult*damp, self.min_order_usdt/max(close,1e-12) if self.min_order_usdt>0 else 0.0); value=qty_add*close
            if not self._order_value_ok(close, qty_add): break
            if (st.pos_value_usdt + value) > max_budget: break
            trigger_level=st.next_level_price
            new_value=float(pos.entry)*float(pos.qty) + value
            new_qty=float(pos.qty)+qty_add
            new_entry=new_value/max(new_qty,1e-12)
            pos.qty=new_qty; pos.entry=new_entry
            st.pos_value_usdt += value; st.pos_size=new_qty; st.avg_price=new_entry; st.lots.append((qty_add, close)); st.num_fills += 1; st.last_fill_price=trigger_level; st.next_level_price=self._next_level(trigger_level, st.num_fills); fills += 1; self._register_order(); st.trailing_active=False; st.trailing_ref=None
            try: st.last_fill_ts = int(self._parse_time(t).timestamp())
            except Exception: pass
        subs=0
        while (not tp_blocks_dca and st.num_fills>self.subsell_min_fills and st.lots and subs<self.max_subsells_per_bar and self._can_place_order(t)):
            qty_last, entry_last = st.lots[-1]
            exposure_pct=((st.pos_size*close)/self.equity_for_sizing*100.0) if self.equity_for_sizing>0 else 0.0
            force_be=exposure_pct > self.hard_breakeven_deleverage_pct
            last_tp = entry_last*(1.0 + self.subsell_tp_percent/100.0) if self.SIDE=='LONG' else entry_last*(1.0 - self.subsell_tp_percent/100.0)
            touch_level = entry_last if force_be else last_tp
            touch = (hi>=touch_level) if self.SIDE=='LONG' else (lo<=touch_level)
            if self.subsell_close_confirm_mode=='off': close_ok=True
            elif self.subsell_close_confirm_mode=='breakeven' or force_be: close_ok=(close>=entry_last) if self.SIDE=='LONG' else (close<=entry_last)
            else: close_ok=(close>=last_tp) if self.SIDE=='LONG' else (close<=last_tp)
            if not (touch and close_ok): break
            qty_total=float(pos.qty); qty_close=min(float(qty_last), qty_total)
            if qty_total<=0 or qty_close<=0: break
            qty_frac=max(0.0,min(1.0, qty_close/max(qty_total,1e-12)))
            total_cost=sum(q*p for q,p in st.lots)
            profit = qty_close*((close-entry_last) if self.SIDE=='LONG' else (entry_last-close))
            remaining_qty=qty_total-qty_close
            remaining_cost=total_cost - qty_close*entry_last
            if self.auto_merge and remaining_qty>0: remaining_cost -= profit
            if remaining_qty>0:
                st.pending_new_entry = remaining_cost/max(remaining_qty,1e-12); st.avg_price=st.pending_new_entry
            pos.entry=float(entry_last)
            st.lots.pop(); st.num_fills=max(st.num_fills-1,0); st.pos_size=max(0.0, qty_total-qty_close)
            if st.lots:
                st.last_fill_price=st.lots[-1][1]; st.next_level_price=self._next_level(st.last_fill_price, st.num_fills)
            else:
                st.last_fill_price=None; st.next_level_price=None
            self._register_order(); return ExitSig(action='TP_PARTIAL', exit_price=close, qty_frac=qty_frac, reason='Sub-sell last lot' if self.SIDE=='LONG' else 'Sub-cover last lot')
        return None

class CryptomineLongPackAdaptiveEven(_PackAdaptiveBase):
    SIDE='LONG'
    def __init__(self,cfg): super().__init__(cfg,'strategy_params_long')

class CryptomineShortPackAdaptiveEven(_PackAdaptiveBase):
    SIDE='SHORT'
    def __init__(self,cfg): super().__init__(cfg,'strategy_params_short')
