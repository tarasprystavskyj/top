# -*- coding: utf-8 -*-
"""Even-bar long/short cryptomine strategies ported from PineScript.

Backtester assumptions kept close to existing robust sample:
- one active position per symbol per strategy instance
- DCA and LIFO sub-sells/sub-covers are emulated inside manage_position()
- fill model is close-based (same simplification as existing Python sample)
- even-bar gating is enforced for ALL actions, mirroring f_canSignal() in Pine
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import datetime as _dt


@dataclass
class Sig:
    side: str
    tp: Optional[float] = None
    sl: Optional[float] = None
    reason: str = ""


@dataclass
class ExitSig:
    action: str
    exit_price: float
    qty_frac: float = 1.0
    reason: str = ""


@dataclass
class _SymState:
    pos_cost_usdt: float = 0.0
    pos_size: float = 0.0
    avg_price: Optional[float] = None
    num_fills: int = 0
    last_fill_price: Optional[float] = None
    next_level_price: Optional[float] = None
    lots: List[Tuple[float, float]] = None  # qty, entry_price
    trailing_active: bool = False
    trailing_ref: Optional[float] = None
    reset_pending: bool = False
    pending_new_entry: Optional[float] = None

    def __post_init__(self):
        if self.lots is None:
            self.lots = []


class _CryptomineEvenBase:
    SIDE = "LONG"  # override in child

    def __init__(self, cfg: Dict[str, Any], params_key: str = "strategy_params"):
        self.cfg = cfg
        sp = cfg.get(params_key, {}) or {}

        # shared inputs
        qty_key = "firstBuyQty" if self.SIDE == "LONG" else "firstSellQty"
        legacy_key = "firstBuyUSDT" if self.SIDE == "LONG" else "firstSellUSDT"
        self.first_qty = float(sp.get(qty_key, sp.get(legacy_key, 5.0)))
        # backward-compatible alias; name is legacy, semantics are now BASE-ASSET quantity
        self.first_usdt = self.first_qty
        self.tp_percent = float(sp.get("tpPercent", 1.1))
        self.callback_percent = float(sp.get("callbackPercent", 0.2))
        self.margin_call_limit = int(sp.get("marginCallLimit", 244))
        self.linear_step_percent = float(sp.get("linearDropPercent" if self.SIDE == "LONG" else "linearRisePercent", 0.5))
        self.auto_merge = bool(sp.get("autoMerge", True))
        self.subsell_tp_percent = float(sp.get("subSellTPPercent", 1.3))
        self.stop_percent = float(sp.get("stopPercent", 99.0))

        self.step1 = float(sp.get("drop1" if self.SIDE == "LONG" else "rise1", 0.3))
        self.step2 = float(sp.get("drop2" if self.SIDE == "LONG" else "rise2", 0.4))
        self.step3 = float(sp.get("drop3" if self.SIDE == "LONG" else "rise3", 0.6))
        self.step4 = float(sp.get("drop4" if self.SIDE == "LONG" else "rise4", 0.8))
        self.step5 = float(sp.get("drop5" if self.SIDE == "LONG" else "rise5", 0.8))

        self.mult2 = float(sp.get("mult2", 1.5))
        self.mult3 = float(sp.get("mult3", 1.0))
        self.mult4 = float(sp.get("mult4", 2.0))
        self.mult5 = float(sp.get("mult5", 3.5))
        self.max_fills_per_bar = int(sp.get("maxFillsPerBar", 6))
        self.max_subsells_per_bar = int(sp.get("maxSubSellsPerBar", 10))

        # In Pine the throttle is effectively one alert per active bar; for backtest fills it is non-binding.
        # We keep a switch but default it off.
        self.signals_throttle_enabled = bool(sp.get("signalsThrottleEnabled", False))
        self.max_signals_window = int(sp.get("maxSignalsWindow", 14))
        self.window_bars = int(sp.get("windowBars", 6))

        self.sl_enabled = bool(sp.get("slEnabled", False))
        self.max_budget_frac = float(sp.get("maxBudgetFrac", 0.60))
        self.initial_capital = float(cfg.get("initial_capital", cfg.get("initial_equity", 100.0)))
        self.timeframe = str(cfg.get("timeframe", "1m"))
        self.use_even_bars = bool(sp.get("useEvenBars", True))
        self.use_live_sync_start = bool(sp.get("useLiveSyncStart", False))
        self.live_start_time = sp.get("liveStartTime", None)

        self._states: Dict[str, _SymState] = {}

    def universe(self, t, md_map):
        return list(md_map.keys())

    def rank(self, t, md_map, universe_syms):
        return list(universe_syms)

    def _get_state(self, sym: str) -> _SymState:
        st = self._states.get(sym)
        if st is None:
            st = _SymState()
            self._states[sym] = st
        return st

    def _parse_time(self, t) -> Optional[_dt.datetime]:
        if t is None:
            return None
        if isinstance(t, _dt.datetime):
            return t
        try:
            return _dt.datetime.fromisoformat(str(t).replace("Z", "+00:00"))
        except Exception:
            return None

    def _tf_minutes(self) -> float:
        s = self.timeframe.strip().lower()
        if s.endswith("m"):
            return float(s[:-1])
        if s.endswith("h"):
            return float(s[:-1]) * 60.0
        if s.endswith("d"):
            return float(s[:-1]) * 1440.0
        return float(s)

    def _allow_this_bar(self, t) -> bool:
        if not self.use_even_bars:
            return True
        dt = self._parse_time(t)
        if dt is None:
            return True
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_dt.timezone.utc)
        else:
            dt = dt.astimezone(_dt.timezone.utc)
        anchor = _dt.datetime(dt.year, 1, 1, 0, 0, 0, tzinfo=_dt.timezone.utc)
        tf_sec = max(1, int(round(self._tf_minutes() * 60.0)))
        bars_from_anchor = int((dt - anchor).total_seconds() // tf_sec)
        return bars_from_anchor % 2 == 0

    def _live_now(self, t) -> bool:
        if not self.use_live_sync_start or self.live_start_time in (None, 0, "0"):
            return True
        dt = self._parse_time(t)
        start = self._parse_time(self.live_start_time)
        if dt is None or start is None:
            return True
        return dt >= start

    def _can_act(self, t) -> bool:
        return self._live_now(t) and self._allow_this_bar(t)

    def _get_step_for_next_level(self, num_fills: int) -> float:
        nf = num_fills + 1
        if nf == 2:
            return self.step1
        if nf == 3:
            return self.step2
        if nf == 4:
            return self.step3
        if nf == 5:
            return self.step4
        if nf == 6:
            return self.step5
        return self.linear_step_percent

    def _get_mult_for_next_level(self, num_fills: int) -> float:
        nf = num_fills + 1
        if nf == 2:
            return self.mult2
        if nf == 3:
            return self.mult3
        if nf == 4:
            return self.mult4
        if nf == 5:
            return self.mult5
        return 1.0

    def _next_level(self, last_fill_price: float, num_fills: int) -> float:
        step = self._get_step_for_next_level(num_fills)
        if self.SIDE == "LONG":
            return last_fill_price * (1.0 - step / 100.0)
        return last_fill_price * (1.0 + step / 100.0)

    def _entry_tp_sl(self, entry_price: float) -> Tuple[float, Optional[float]]:
        entry_price = float(entry_price)
        if self.SIDE == "LONG":
            tp = entry_price * (1.0 + self.tp_percent / 100.0)
            sl = entry_price * (1.0 - max(0.0, min(99.9, self.stop_percent)) / 100.0)
        else:
            tp = entry_price * (1.0 - self.tp_percent / 100.0)
            sl = entry_price * (1.0 + max(0.0, min(99.9, self.stop_percent)) / 100.0)
        if not self.sl_enabled:
            return float(tp), None
        if not (sl > 0.0):
            sl = entry_price * 0.0001
        return float(tp), float(sl)

    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx=None):
        t = row.get("datetime_utc") if hasattr(row, "get") else None
        if not is_opening or not self._can_act(t):
            return None
        st = self._get_state(sym)
        close = float(row["close"])

        if (st.reset_pending or (st.pos_size == 0 and len(st.lots) == 0)):
            st.reset_pending = False
            st.trailing_active = False
            st.trailing_ref = None
            st.pending_new_entry = None

            qty0 = self.first_qty
            st.pos_cost_usdt = qty0 * close
            st.pos_size = qty0
            st.avg_price = close
            st.num_fills = 1
            st.last_fill_price = close
            st.next_level_price = self._next_level(st.last_fill_price, st.num_fills)
            st.lots = [(qty0, close)]
            tp, sl = self._entry_tp_sl(close)
            return Sig(side=self.SIDE, tp=tp, sl=sl, reason="First")
        return None

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx=None):
        t = row.get("datetime_utc") if hasattr(row, "get") else None
        if not self._can_act(t):
            return None
        st = self._get_state(sym)
        close = float(row["close"])

        # sync qty after partials
        if st.pos_size > 0 and pos.qty is not None and pos.qty > 0 and abs(pos.qty - st.pos_size) / max(st.pos_size, 1e-12) > 1e-6:
            ratio = pos.qty / st.pos_size
            st.lots = [(q * ratio, p) for (q, p) in st.lots]
            st.pos_size = float(pos.qty)

        if st.pending_new_entry is not None:
            pos.entry = st.pending_new_entry
            st.avg_price = st.pending_new_entry
            st.pending_new_entry = None

        if st.avg_price is None:
            st.avg_price = float(pos.entry)
        if st.pos_size <= 0:
            st.pos_size = float(pos.qty)

        max_budget = self.initial_capital * self.max_budget_frac

        # Full TP with optional trailing
        if self.SIDE == "LONG":
            tp_price = st.avg_price * (1.0 + self.tp_percent / 100.0)
            tp_hit = close >= tp_price
        else:
            tp_price = st.avg_price * (1.0 - self.tp_percent / 100.0)
            tp_hit = close <= tp_price

        if tp_hit:
            if self.callback_percent and self.callback_percent > 0:
                st.trailing_active = True
                if self.SIDE == "LONG":
                    st.trailing_ref = close if st.trailing_ref is None else max(st.trailing_ref, close)
                    trail_stop = st.trailing_ref * (1.0 - self.callback_percent / 100.0)
                    fire = close <= trail_stop
                else:
                    st.trailing_ref = close if st.trailing_ref is None else min(st.trailing_ref, close)
                    trail_stop = st.trailing_ref * (1.0 + self.callback_percent / 100.0)
                    fire = close >= trail_stop
                if fire:
                    st.reset_pending = True
                    st.pos_cost_usdt = 0.0
                    st.pos_size = 0.0
                    st.avg_price = None
                    st.num_fills = 0
                    st.last_fill_price = None
                    st.next_level_price = None
                    st.lots = []
                    st.trailing_active = False
                    st.trailing_ref = None
                    return ExitSig(action="TP", exit_price=close, qty_frac=1.0, reason="TP Full (Trailing)")
            else:
                st.reset_pending = True
                st.pos_cost_usdt = 0.0
                st.pos_size = 0.0
                st.avg_price = None
                st.num_fills = 0
                st.last_fill_price = None
                st.next_level_price = None
                st.lots = []
                st.trailing_active = False
                st.trailing_ref = None
                return ExitSig(action="TP", exit_price=close, qty_frac=1.0, reason="TP Full")

        if not tp_hit:
            st.trailing_active = False
            st.trailing_ref = None

        # DCA fills (close-based approximation)
        fills = 0
        level_cross = (lambda c, lvl: c <= lvl) if self.SIDE == "LONG" else (lambda c, lvl: c >= lvl)
        while (
            st.num_fills < self.margin_call_limit
            and fills < self.max_fills_per_bar
            and st.next_level_price is not None
            and level_cross(close, st.next_level_price)
        ):
            mult = self._get_mult_for_next_level(st.num_fills)
            qty_add = self.first_qty * mult
            fill_price = close
            add_notional = qty_add * fill_price
            if self.max_budget_frac < 0.999999 and (st.pos_cost_usdt + add_notional) > max_budget:
                break

            new_cost = float(pos.entry) * float(pos.qty) + add_notional
            new_qty = float(pos.qty) + qty_add
            new_entry = new_cost / max(new_qty, 1e-12)
            pos.qty = new_qty
            pos.entry = new_entry

            st.pos_cost_usdt += add_notional
            st.pos_size = new_qty
            st.avg_price = new_entry
            st.lots.append((qty_add, fill_price))
            st.num_fills += 1
            st.last_fill_price = fill_price
            st.next_level_price = self._next_level(st.last_fill_price, st.num_fills)
            fills += 1

        # LIFO partial exits: only after >5 fills, up to configured per bar
        subsells = 0
        while st.num_fills > 5 and len(st.lots) > 0 and subsells < self.max_subsells_per_bar:
            qty_last, entry_last = st.lots[-1]
            if self.SIDE == "LONG":
                last_lot_tp = entry_last * (1.0 + self.subsell_tp_percent / 100.0)
                hit = close >= last_lot_tp
            else:
                last_lot_tp = entry_last * (1.0 - self.subsell_tp_percent / 100.0)
                hit = close <= last_lot_tp
            if not hit:
                break

            qty_total = float(pos.qty)
            qty_close = min(float(qty_last), qty_total)
            if qty_total <= 0 or qty_close <= 0:
                break
            qty_frac = max(0.0, min(1.0, qty_close / max(qty_total, 1e-12)))

            total_cost = sum(q * p for (q, p) in st.lots)
            profit = qty_close * ((close - float(entry_last)) if self.SIDE == "LONG" else (float(entry_last) - close))
            remaining_qty = qty_total - qty_close
            remaining_cost = total_cost - qty_close * float(entry_last)
            if self.auto_merge and remaining_qty > 0:
                remaining_cost -= profit
            if remaining_qty > 0:
                st.pending_new_entry = remaining_cost / max(remaining_qty, 1e-12)
                st.avg_price = st.pending_new_entry

            pos.entry = float(entry_last)  # correct realized pnl basis for bt core
            st.lots.pop()
            st.num_fills = max(st.num_fills - 1, 0)
            st.pos_size = max(0.0, qty_total - qty_close)
            if len(st.lots) > 0:
                st.last_fill_price = st.lots[-1][1]
                st.next_level_price = self._next_level(st.last_fill_price, st.num_fills)
            else:
                st.last_fill_price = None
                st.next_level_price = None
            subsells += 1
            return ExitSig(
                action="TP_PARTIAL",
                exit_price=close,
                qty_frac=qty_frac,
                reason="Sub-sell last lot" if self.SIDE == "LONG" else "Sub-cover last lot",
            )

        return None


class CryptomineCLimit14LongEven(_CryptomineEvenBase):
    SIDE = "LONG"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg, params_key="strategy_params_long")


class CryptomineCLimit14ShortEven(_CryptomineEvenBase):
    SIDE = "SHORT"

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg, params_key="strategy_params_short")
