# -*- coding: utf-8 -*-
from __future__ import annotations
from dataclasses import dataclass, field
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
    pending_new_entry: Optional[float] = None
    pending_comp_usdt: float = 0.0
    pending_exit_meta: Optional[Dict[str, Any]] = None

class _PackAdaptiveBase:
    SIDE = 'LONG'
    def __init__(self, cfg: Dict[str, Any], params_key: str):
        self.cfg = cfg
        sp = cfg.get(params_key, {}) or {}
        self.first_qty_coin = float(sp.get('firstBuyQtyCoin' if self.SIDE=='LONG' else 'firstSellQtyCoin', 0.09))
        self.min_order_qty_coin = float(sp.get('minOrderQtyCoin', 0.09))
        self.min_order_usdt = float(sp.get('minOrderUSDT', 0.0))
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
        # new: carry forward USDT deficits caused by live slippage on closes
        self.enable_slip_loss_comp = bool(sp.get('enableSlipLossCompensation', True))
        self.max_comp_step_pct = float(sp.get('maxCompensationStepPct', 3.0))
        self.comp_roundtrip_fee_rate = float((cfg.get('portfolio') or {}).get('fee_rate', 0.001))
        self.close_fee_rate = float((cfg.get('portfolio') or {}).get('fee_rate', self.comp_roundtrip_fee_rate))
        self.timeframe = str(cfg.get('timeframe', '1m'))
        self._states: Dict[str, _State] = {}
        self._bar_key = None
        self._orders_this_bar = 0
        self._recent_bar_fills = deque()
        self._recent_history_fills = 0

    # --------- state / runner hooks ----------
    def export_state_snapshot(self, sym: str):
        st = self._states.get(sym)
        return copy.deepcopy(st)

    def restore_state_snapshot(self, sym: str, snapshot):
        if snapshot is None:
            self._states.pop(sym, None)
        else:
            self._states[sym] = copy.deepcopy(snapshot)

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
            iso=dt.isocalendar()
            year = getattr(iso, 'year', iso[0])
            week = getattr(iso, 'week', iso[1])
            return f'{year}-W{int(week):02d}'
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
    def _calc_base_qty(self, close, target_invest_pct):
        sizing_pct=target_invest_pct if self.use_trend_adaptive_sizing else self.base_order_pct_eq
        raw=((self.equity_for_sizing*sizing_pct/100.0)/close) if self.use_equity_pct_base else self.first_qty_coin
        return max(raw, self.min_order_qty_coin)
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

    def _record_pending_exit(self, st, *, qty_close, basis_price, baseline_ref_price, signal_ref_price, debug_meta: Optional[Dict[str, Any]] = None):
        if not self.enable_slip_loss_comp:
            st.pending_exit_meta = None
            return
        meta = {
            'qty_close': float(qty_close),
            'basis_price': float(basis_price),
            'baseline_ref_price': float(baseline_ref_price),
            'signal_ref_price': float(signal_ref_price),
            'pending_comp_before': float(st.pending_comp_usdt or 0.0),
        }
        if debug_meta:
            meta.update(dict(debug_meta))
        st.pending_exit_meta = meta

    def _fmt_partial_lot_reason(self, *, lot_id, entry, touch_level, confirm_level, mode, min_allowed_price):
        try:
            return (
                f"lot_id={int(lot_id)} "
                f"entry={float(entry):.10g} "
                f"touch_level={float(touch_level):.10g} "
                f"confirm_level={float(confirm_level):.10g} "
                f"mode={str(mode)} "
                f"min_allowed_price={float(min_allowed_price):.10g}"
            )
        except Exception:
            return f"lot_id={lot_id} mode={mode}"

    def _lot_min_close_price(self, entry_price: float, *, tp_percent: float = 0.0) -> float:
        entry_price = float(entry_price or 0.0)
        fee_rate = float(self.close_fee_rate or 0.0)
        tp_frac = float(tp_percent or 0.0) / 100.0
        if self.SIDE == 'LONG':
            return entry_price * (1.0 + fee_rate + tp_frac)
        return entry_price * (1.0 - fee_rate - tp_frac)

    def _lot_breakeven_price(self, entry_price: float) -> float:
        return self._lot_min_close_price(entry_price, tp_percent=0.0)

    def entry_signal(self,is_opening,sym,row,ctx=None):
        t=row.get('datetime_utc')
        if not is_opening or not self._can_place_order(t): return None
        st=self._get_state(sym)
        _,_,close=self._trigger_prices(row)
        target_pct=self._cached_target_pct(row)
        if target_pct is None:
            target_pct = self._update_trend(st,t,close) if self.use_trend_adaptive_sizing else self.base_order_pct_eq
        if st.reset_pending or (st.pos_size==0 and len(st.lots)==0):
            st.reset_pending=False; st.trailing_active=False; st.trailing_ref=None; st.pending_new_entry=None; st.pending_exit_meta=None
            st.cycle_base_qty_coin=self._calc_base_qty(close,target_pct)
            qty0=st.cycle_base_qty_coin; value=qty0*close
            if not self._order_value_ok(close, qty0):
                return None
            st.pos_value_usdt=value; st.pos_size=qty0; st.avg_price=close; st.num_fills=1; st.last_fill_price=close; st.next_level_price=self._next_level(close,1); st.lots=[(qty0,close)]
            self._register_order()
            return Sig(side=self.SIDE, tp=self._entry_tp(close), sl=None, reason='First', qty=qty0)
        return None

    def manage_position(self,sym,row,pos,ctx=None):
        t=row.get('datetime_utc'); self._roll_bar(t)
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
                    self._record_pending_exit(st, qty_close=qty_close, basis_price=basis, baseline_ref_price=base_tp_price, signal_ref_price=close)
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None
                    self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full (Trailing)')
            else:
                if tp_close_confirmed and self._can_place_order(t):
                    basis=float(st.avg_price or pos.entry or close); qty_close=float(st.pos_size or pos.qty or 0.0)
                    self._record_pending_exit(st, qty_close=qty_close, basis_price=basis, baseline_ref_price=base_tp_price, signal_ref_price=close)
                    st.reset_pending=True; st.pos_value_usdt=0.0; st.pos_size=0.0; st.avg_price=None; st.num_fills=0; st.last_fill_price=None; st.next_level_price=None; st.lots=[]; st.trailing_active=False; st.trailing_ref=None
                    self._register_order(); return ExitSig(action='TP', exit_price=close, reason='TP Full')
        if not tp_touch: st.trailing_active=False; st.trailing_ref=None
        fills=0
        while (not tp_blocks_dca and st.num_fills<self.margin_call_limit and fills<self.max_fills_per_bar and st.next_level_price is not None and ((lo<=st.next_level_price) if self.SIDE=='LONG' else (hi>=st.next_level_price)) and (((not self.require_close_beyond_dca) or (close<=st.next_level_price)) if self.SIDE=='LONG' else ((not self.require_close_beyond_dca) or (close>=st.next_level_price))) and self._can_place_order(t)):
            mult=self._get_mult(st.num_fills)
            if st.cycle_base_qty_coin is None: st.cycle_base_qty_coin=self._calc_base_qty(close,0.0)
            qty_add=st.cycle_base_qty_coin*mult; value=qty_add*close
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
            lot_id = len(st.lots)
            qty_total=float(pos.qty)
            if qty_total<=0: break
            exposure_pct=((st.pos_size*close)/self.equity_for_sizing*100.0) if self.equity_for_sizing>0 else 0.0
            force_be=exposure_pct > self.hard_breakeven_deleverage_pct

            normal_tp_floor = self._lot_min_close_price(float(entry_last), tp_percent=self.subsell_tp_percent)
            breakeven_floor = self._lot_breakeven_price(float(entry_last))
            extra_px_sub = 0.0 if force_be else self._comp_extra_px(st, float(qty_last), float(st.pos_size or qty_total), float(entry_last))
            mode = 'deleverage_breakeven' if force_be else 'lot_tp_floor_fee_aware'

            if self.SIDE=='LONG':
                touch_level = breakeven_floor if force_be else (normal_tp_floor + extra_px_sub)
            else:
                touch_level = breakeven_floor if force_be else (normal_tp_floor - extra_px_sub)

            touch = (hi>=touch_level) if self.SIDE=='LONG' else (lo<=touch_level)

            # Strict rule:
            # - normal mode: close must stay at/through exact-lot TP floor
            # - deleverage mode only: breakeven close is allowed
            confirm_level = touch_level
            min_allowed_price = confirm_level
            close_ok=(close>=confirm_level) if self.SIDE=='LONG' else (close<=confirm_level)
            if not (touch and close_ok): break

            qty_close=min(float(qty_last), qty_total)
            if qty_close<=0: break
            qty_frac=max(0.0,min(1.0, qty_close/max(qty_total,1e-12)))
            total_cost=sum(q*p for q,p in st.lots)
            profit = qty_close*((close-entry_last) if self.SIDE=='LONG' else (entry_last-close))
            remaining_qty=qty_total-qty_close
            remaining_cost=total_cost - qty_close*entry_last
            if self.auto_merge and remaining_qty>0: remaining_cost -= profit
            debug_meta = {
                'lot_id': int(lot_id),
                'entry': float(entry_last),
                'touch_level': float(touch_level),
                'confirm_level': float(confirm_level),
                'mode': mode,
                'min_allowed_price': float(min_allowed_price),
                'normal_tp_floor': float(normal_tp_floor),
                'breakeven_floor': float(breakeven_floor),
                'extra_px_sub': float(extra_px_sub),
                'exposure_pct': float(exposure_pct),
            }
            self._record_pending_exit(st, qty_close=qty_close, basis_price=float(entry_last), baseline_ref_price=confirm_level, signal_ref_price=close, debug_meta=debug_meta)
            if remaining_qty>0:
                st.pending_new_entry = remaining_cost/max(remaining_qty,1e-12); st.avg_price=st.pending_new_entry
            pos.entry=float(entry_last)
            st.lots.pop(); st.num_fills=max(st.num_fills-1,0); st.pos_size=max(0.0, qty_total-qty_close)
            if st.lots:
                st.last_fill_price=st.lots[-1][1]; st.next_level_price=self._next_level(st.last_fill_price, st.num_fills)
            else:
                st.last_fill_price=None; st.next_level_price=None
            reason_base = 'Sub-sell last lot' if self.SIDE=='LONG' else 'Sub-cover last lot'
            reason = f"{reason_base} [{self._fmt_partial_lot_reason(lot_id=lot_id, entry=entry_last, touch_level=touch_level, confirm_level=confirm_level, mode=mode, min_allowed_price=min_allowed_price)}]"
            self._register_order(); return ExitSig(action='TP_PARTIAL', exit_price=close, qty_frac=qty_frac, reason=reason)
        return None

class CryptomineLongPackAdaptiveEven(_PackAdaptiveBase):
    SIDE='LONG'
    def __init__(self,cfg): super().__init__(cfg,'strategy_params_long')

class CryptomineShortPackAdaptiveEven(_PackAdaptiveBase):
    SIDE='SHORT'
    def __init__(self,cfg): super().__init__(cfg,'strategy_params_short')
