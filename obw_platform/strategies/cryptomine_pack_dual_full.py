# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass
from collections import deque
import datetime as _dt
import math
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

    def __post_init__(self):
        if self.lots is None: self.lots = []
        if self.trend_htf_closes is None: self.trend_htf_closes = deque()
        if self.trend_ma_series is None: self.trend_ma_series = deque()

class _PackAdaptiveBase:
    SIDE = 'LONG'
    def __init__(self, cfg: Dict[str, Any], params_key: str):
        self.cfg = cfg
        sp = cfg.get(params_key, {}) or {}
        # sizing
        self.first_qty_coin = float(sp.get('firstBuyQtyCoin' if self.SIDE=='LONG' else 'firstSellQtyCoin', 0.09))
        self.min_order_qty_coin = float(sp.get('minOrderQtyCoin', 0.09))
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
        score_rng=max(abs(self.trend_score_max-self.trend_score_min),1e-6)
        factor=max(0.0,min(1.0,(strength-self.trend_score_min)/score_rng))
        return self.min_invest_pct + (self.max_invest_pct-self.min_invest_pct)*factor
    def _calc_base_qty(self, close, target_invest_pct):
        sizing_pct=target_invest_pct if self.use_trend_adaptive_sizing else self.base_order_pct_eq
        raw=((self.equity_for_sizing*sizing_pct/100.0)/close) if self.use_equity_pct_base else self.first_qty_coin
        return max(raw, self.min_order_qty_coin)
    def _entry_tp(self, price):
        return price*(1.0+self.tp_percent/100.0) if self.SIDE=='LONG' else price*(1.0-self.tp_percent/100.0)
    def entry_signal(self,is_opening,sym,row,ctx=None):
        t=row.get('datetime_utc')
        if not is_opening or not self._can_place_order(t): return None
        st=self._get_state(sym)
        _,_,close=self._trigger_prices(row)
        target_pct=self._update_trend(st,t,close)
        if st.reset_pending or (st.pos_size==0 and len(st.lots)==0):
            st.reset_pending=False; st.trailing_active=False; st.trailing_ref=None; st.pending_new_entry=None
            st.cycle_base_qty_coin=self._calc_base_qty(close,target_pct)
            qty0=st.cycle_base_qty_coin; value=qty0*close
            st.pos_value_usdt=value; st.pos_size=qty0; st.avg_price=close; st.num_fills=1; st.last_fill_price=close; st.next_level_price=self._next_level(close,1); st.lots=[(qty0,close)]
            self._register_order()
            return Sig(side=self.SIDE, tp=self._entry_tp(close), sl=None, reason='First', qty=qty0)
        return None
    def manage_position(self,sym,row,pos,ctx=None):
        t=row.get('datetime_utc'); self._roll_bar(t)
        if not self._live_now(t) or not self._allow_this_bar(t): return None
        st=self._get_state(sym)
        hi,lo,close=self._trigger_prices(row); _=self._update_trend(st,t,close)
        if st.pos_size>0 and pos.qty is not None and pos.qty>0 and abs(pos.qty-st.pos_size)/max(st.pos_size,1e-12)>1e-6:
            ratio=pos.qty/st.pos_size; st.lots=[(q*ratio,p) for q,p in st.lots]; st.pos_size=float(pos.qty)
        if st.pending_new_entry is not None:
            pos.entry=st.pending_new_entry; st.avg_price=st.pending_new_entry; st.pending_new_entry=None
        if st.avg_price is None: st.avg_price=float(pos.entry)
        max_budget=self.equity_for_sizing
        tp_price=self._entry_tp(st.avg_price)
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
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None; self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full (Trailing)')
            else:
                if tp_close_confirmed and self._can_place_order(t):
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None; self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full')
        if not tp_touch: st.trailing_active=False; st.trailing_ref=None
        fills=0
        while (not tp_blocks_dca and st.num_fills<self.margin_call_limit and fills<self.max_fills_per_bar and st.next_level_price is not None and ((lo<=st.next_level_price) if self.SIDE=='LONG' else (hi>=st.next_level_price)) and ((close<=st.next_level_price) if self.SIDE=='LONG' else (close>=st.next_level_price) or not self.require_close_beyond_dca) and self._can_place_order(t)):
            mult=self._get_mult(st.num_fills)
            if st.cycle_base_qty_coin is None: st.cycle_base_qty_coin=self._calc_base_qty(close,0.0)
            qty_add=st.cycle_base_qty_coin*mult; value=qty_add*close
            if (st.pos_value_usdt + value) > max_budget: break
            trigger_level=st.next_level_price
            new_value=float(pos.entry)*float(pos.qty) + value
            new_qty=float(pos.qty)+qty_add
            new_entry=new_value/max(new_qty,1e-12)
            pos.qty=new_qty; pos.entry=new_entry
            st.pos_value_usdt += value; st.pos_size=new_qty; st.avg_price=new_entry; st.lots.append((qty_add, close)); st.num_fills += 1; st.last_fill_price=trigger_level; st.next_level_price=self._next_level(trigger_level, st.num_fills); fills += 1; self._register_order(); st.trailing_active=False; st.trailing_ref=None
        subs=0
        while (not tp_blocks_dca and st.num_fills>5 and st.lots and subs<self.max_subsells_per_bar and self._can_place_order(t)):
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
