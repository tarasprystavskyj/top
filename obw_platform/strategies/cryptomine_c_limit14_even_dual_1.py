# -*- coding: utf-8 -*-
"""Dual even-bar Cryptomine strategy port with stricter close-confirmation logic.

Notes:
- Keeps firstBuyUSDT / firstSellUSDT semantics in USDT (per latest user requirement).
- Close-only execution model: all fills happen at current bar close.
- Trigger levels (next DCA / TP) are conditions only; optional high/low touch is used
  when OHLC is available, otherwise close is used.
- Mirrors the newer Pine short logic and applies a symmetric long variant.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
import datetime as _dt
from collections import deque
import math


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
    pos_cost_usdt: float = 0.0        # long: cost; short: proceeds
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
    SIDE = "LONG"

    def __init__(self, cfg: Dict[str, Any], params_key: str = "strategy_params"):
        self.cfg = cfg
        sp = cfg.get(params_key, {}) or {}

        # Sizing (kept in USDT by requirement)
        self.first_usdt = float(sp.get("firstBuyUSDT" if self.SIDE == "LONG" else "firstSellUSDT", 5.0))
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

        # New stricter logic options
        if self.SIDE == "SHORT":
            self.require_close_beyond_full_tp = bool(sp.get("requireCloseBelowFullTP", True))
            self.subsell_close_confirm_mode = str(sp.get("subCoverCloseConfirmMode", "breakeven"))
            self.require_close_beyond_dca = bool(sp.get("requireCloseAboveDcaLevel", True))
        else:
            self.require_close_beyond_full_tp = bool(sp.get("requireCloseAboveFullTP", True))
            self.subsell_close_confirm_mode = str(sp.get("subSellCloseConfirmMode", "breakeven"))
            self.require_close_beyond_dca = bool(sp.get("requireCloseBelowDcaLevel", True))
        self.block_dca_on_tp_touch = bool(sp.get("blockDcaOnTpTouch", False))
        self.use_high_low_touch = bool(sp.get("useHighLowTouch", True))
        self.max_orders_per_3min = int(sp.get("maxOrdersPer3Min", 14))

        self.sl_enabled = bool(sp.get("slEnabled", False))
        self.max_budget_frac = float(sp.get("maxBudgetFrac", 0.60))
        self.initial_capital = float((cfg.get("portfolio", {}) or {}).get("initial_equity_total", cfg.get("initial_capital", cfg.get("initial_equity", 100.0))))
        self.timeframe = str(cfg.get("timeframe", "1m"))
        self.use_even_bars = bool(sp.get("useEvenBars", True))
        self.use_live_sync_start = bool(sp.get("useLiveSyncStart", False))
        self.live_start_time = sp.get("liveStartTime", None)

        self._states: Dict[str, _SymState] = {}

        # Rolling 3-minute fill throttle
        self._bar_key = None
        self._orders_this_bar = 0
        self._recent_bar_fills = deque()
        self._recent_history_fills = 0

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

    def _tf_seconds(self) -> int:
        s = self.timeframe.strip().lower()
        if s.endswith("m"):
            return max(1, int(round(float(s[:-1]) * 60.0)))
        if s.endswith("h"):
            return max(1, int(round(float(s[:-1]) * 3600.0)))
        if s.endswith("d"):
            return max(1, int(round(float(s[:-1]) * 86400.0)))
        return max(1, int(round(float(s) * 60.0)))

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
        tf_sec = self._tf_seconds()
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

    def _roll_bar(self, t):
        dt = self._parse_time(t)
        bar_key = int(dt.timestamp()) if dt is not None else t
        if self._bar_key is None:
            self._bar_key = bar_key
            self._orders_this_bar = 0
            return
        if bar_key != self._bar_key:
            history_limit = max(0, int(math.ceil(180.0 / self._tf_seconds())) - 1)
            if history_limit > 0:
                self._recent_bar_fills.append(self._orders_this_bar)
                self._recent_history_fills += self._orders_this_bar
                while len(self._recent_bar_fills) > history_limit:
                    self._recent_history_fills -= self._recent_bar_fills.popleft()
            else:
                self._recent_history_fills = 0
                self._recent_bar_fills.clear()
            self._orders_this_bar = 0
            self._bar_key = bar_key

    def _recent_orders_in_3min(self) -> int:
        return self._recent_history_fills + self._orders_this_bar

    def _can_place_order(self, t) -> bool:
        self._roll_bar(t)
        return self._can_act(t) and self._recent_orders_in_3min() < self.max_orders_per_3min

    def _register_order(self):
        self._orders_this_bar += 1

    def _trigger_prices(self, row: Dict[str, Any]):
        close = float(row.get("close") or 0.0)
        op = float(row.get("open", close) or close)
        hi = float(row.get("high", close) or close)
        lo = float(row.get("low", close) or close)
        if self.use_high_low_touch:
            return hi, lo, close
        return max(op, close), min(op, close), close

    def _get_step_for_next_level(self, num_fills: int) -> float:
        nf = num_fills + 1
        if nf == 2: return self.step1
        if nf == 3: return self.step2
        if nf == 4: return self.step3
        if nf == 5: return self.step4
        if nf == 6: return self.step5
        return self.linear_step_percent

    def _get_mult_for_next_level(self, num_fills: int) -> float:
        nf = num_fills + 1
        if nf == 2: return self.mult2
        if nf == 3: return self.mult3
        if nf == 4: return self.mult4
        if nf == 5: return self.mult5
        return 1.0

    def _next_level(self, last_fill_price: float, num_fills: int) -> float:
        step = self._get_step_for_next_level(num_fills)
        return last_fill_price * (1.0 - step / 100.0) if self.SIDE == "LONG" else last_fill_price * (1.0 + step / 100.0)

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
        if not is_opening or not self._can_place_order(t):
            return None
        st = self._get_state(sym)
        close = float(row["close"])
        if st.reset_pending or (st.pos_size == 0 and len(st.lots) == 0):
            st.reset_pending = False
            st.trailing_active = False
            st.trailing_ref = None
            st.pending_new_entry = None

            qty0 = self.first_usdt / max(close, 1e-12)
            st.pos_cost_usdt = self.first_usdt
            st.pos_size = qty0
            st.avg_price = close
            st.num_fills = 1
            st.last_fill_price = close
            st.next_level_price = self._next_level(st.last_fill_price, st.num_fills)
            st.lots = [(qty0, close)]
            tp, sl = self._entry_tp_sl(close)
            self._register_order()
            return Sig(side=self.SIDE, tp=tp, sl=sl, reason="First")
        return None

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx=None):
        t = row.get("datetime_utc") if hasattr(row, "get") else None
        self._roll_bar(t)
        if not self._can_act(t):
            return None
        st = self._get_state(sym)
        trigger_high, trigger_low, close = self._trigger_prices(row)

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

        if self.SIDE == "LONG":
            tp_price = st.avg_price * (1.0 + self.tp_percent / 100.0)
            tp_touch = trigger_high >= tp_price
            tp_close_ok = (not self.require_close_beyond_full_tp) or (close >= tp_price)
        else:
            tp_price = st.avg_price * (1.0 - self.tp_percent / 100.0)
            tp_touch = trigger_low <= tp_price
            tp_close_ok = (not self.require_close_beyond_full_tp) or (close <= tp_price)
        tp_close_confirmed = tp_touch and tp_close_ok
        tp_blocks_dca = tp_touch if self.block_dca_on_tp_touch else tp_close_confirmed

        # FULL TP priority
        if tp_touch:
            if self.callback_percent and self.callback_percent > 0:
                st.trailing_active = True
                if self.SIDE == "LONG":
                    st.trailing_ref = trigger_high if st.trailing_ref is None else max(st.trailing_ref, trigger_high)
                    trail_stop = st.trailing_ref * (1.0 - self.callback_percent / 100.0)
                    fire = tp_close_confirmed and close <= trail_stop
                else:
                    st.trailing_ref = trigger_low if st.trailing_ref is None else min(st.trailing_ref, trigger_low)
                    trail_stop = st.trailing_ref * (1.0 + self.callback_percent / 100.0)
                    fire = tp_close_confirmed and close >= trail_stop
                if fire and self._can_place_order(t):
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
                    self._register_order()
                    return ExitSig(action="TP", exit_price=close, qty_frac=1.0, reason="TP Full (Trailing)")
            else:
                if tp_close_confirmed and self._can_place_order(t):
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
                    self._register_order()
                    return ExitSig(action="TP", exit_price=close, qty_frac=1.0, reason="TP Full")

        if not tp_touch:
            st.trailing_active = False
            st.trailing_ref = None

        # DCA fills
        fills = 0
        level_touch = (lambda hi, lo, lvl: lo <= lvl) if self.SIDE == "LONG" else (lambda hi, lo, lvl: hi >= lvl)
        close_confirm_ok = (lambda c, lvl: c <= lvl) if self.SIDE == "LONG" else (lambda c, lvl: c >= lvl)
        while (
            not tp_blocks_dca
            and st.num_fills < self.margin_call_limit
            and fills < self.max_fills_per_bar
            and st.next_level_price is not None
            and level_touch(trigger_high, trigger_low, st.next_level_price)
            and ((not self.require_close_beyond_dca) or close_confirm_ok(close, st.next_level_price))
            and self._can_place_order(t)
        ):
            mult = self._get_mult_for_next_level(st.num_fills)
            add_usdt = self.first_usdt * mult
            if self.max_budget_frac < 0.999999 and (st.pos_cost_usdt + add_usdt) > max_budget:
                break
            trigger_level = st.next_level_price
            fill_price = close
            qty_add = add_usdt / max(fill_price, 1e-12)

            new_cost = float(pos.entry) * float(pos.qty) + add_usdt
            new_qty = float(pos.qty) + qty_add
            new_entry = new_cost / max(new_qty, 1e-12)
            pos.qty = new_qty
            pos.entry = new_entry

            st.pos_cost_usdt += add_usdt
            st.pos_size = new_qty
            st.avg_price = new_entry
            st.lots.append((qty_add, fill_price))
            st.num_fills += 1
            st.last_fill_price = trigger_level
            st.next_level_price = self._next_level(st.last_fill_price, st.num_fills)
            fills += 1
            self._register_order()

        # LIFO partial exits with stricter close confirmation
        subsells = 0
        while (
            not tp_blocks_dca
            and st.num_fills > 5
            and len(st.lots) > 0
            and subsells < self.max_subsells_per_bar
            and self._can_place_order(t)
        ):
            qty_last, entry_last = st.lots[-1]
            if self.SIDE == "LONG":
                last_lot_tp = entry_last * (1.0 + self.subsell_tp_percent / 100.0)
                touch = trigger_high >= last_lot_tp
                close_ok = {
                    "off": True,
                    "breakeven": close >= entry_last,
                    "subsell_tp": close >= last_lot_tp,
                }.get(self.subsell_close_confirm_mode, close >= entry_last)
            else:
                last_lot_tp = entry_last * (1.0 - self.subsell_tp_percent / 100.0)
                touch = trigger_low <= last_lot_tp
                close_ok = {
                    "off": True,
                    "breakeven": close <= entry_last,
                    "subcover_tp": close <= last_lot_tp,
                }.get(self.subsell_close_confirm_mode, close <= entry_last)
            if not (touch and close_ok):
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

            pos.entry = float(entry_last)  # make partial realized PnL use LIFO lot cost basis
            st.lots.pop()
            st.num_fills = max(st.num_fills - 1, 0)
            st.pos_size = max(0.0, qty_total - qty_close)
            if st.lots:
                st.last_fill_price = st.lots[-1][1]
                st.next_level_price = self._next_level(st.last_fill_price, st.num_fills)
            else:
                st.last_fill_price = None
                st.next_level_price = None
            subsells += 1
            self._register_order()
            return ExitSig(action="TP_PARTIAL", exit_price=close, qty_frac=qty_frac, reason="Sub-sell last lot" if self.SIDE == "LONG" else "Sub-cover last lot")

        return None


class CryptomineCLimit14LongEven(_CryptomineEvenBase):
    SIDE = "LONG"
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg, params_key="strategy_params_long")


class CryptomineCLimit14ShortEven(_CryptomineEvenBase):
    SIDE = "SHORT"
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg, params_key="strategy_params_short")
