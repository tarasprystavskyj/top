# -*- coding: utf-8 -*-
"""Cryptomine (Royal Q) — TOR v5 'C - limit 14 (robust)' for backtester_core_speed3_veto_universe_2.py

NOTE (важливо): backtester_core_speed3_veto_universe_2.py підтримує лише 1 активну позицію на символ.
Оригінальний PineScript тримає "склад" із багатьох лотів (DCA) та робить LIFO sub-sells.
Тут ми емулюємо це в межах ОДНІЄЇ позиції через:
- додавання (DCA) всередині manage_position(): збільшуємо pos.qty і оновлюємо pos.entry на новий VWAP
- sub-sell як TP_PARTIAL (1 частковий продаж на бар)
- опційний autoMerge як зсув бази собівартості після часткового продажу (застосовується на наступному барі)

Також додано "alert-safe" throttle: максимум N сигналів за M барів (за замовчуванням 14 за 6 барів).

PATCH (17 Dec 2025):
1) bt2 інколи передає row без timestamp-поля -> manage_position має використовувати _get_bar_time(row).
2) bt2 вимагає numeric take_profit/stop_price на вході -> entry_signal завжди повертає Sig(tp=..., sl=...).
3) ВАЖЛИВЕ: realized PnL від TP_PARTIAL у bt2 рахується від pos.entry.
   Якщо pos.entry = VWAP (середня), то LIFO sub-sell може давати "дивний" PnL (аж до від’ємного),
   хоча останній лот продаємо в плюс.
   ФІКС: перед поверненням ExitSig(TP_PARTIAL) тимчасово ставимо pos.entry = entry_last (ціна входу останнього лота),
         а на наступному барі відновлюємо pos.entry до нового середнього (pending_new_entry).
"""

from __future__ import annotations

from dataclasses import dataclass
from collections import deque
from typing import Any, Dict, List, Optional, Tuple


# ---------------------
# Backtester контракт
# ---------------------
@dataclass
class Sig:
    side: str  # 'LONG' | 'SHORT'
    tp: Optional[float] = None
    sl: Optional[float] = None
    reason: str = ""


@dataclass
class ExitSig:
    action: str  # 'TP' | 'SL' | 'EXIT' | 'TP_PARTIAL'
    exit_price: float
    qty_frac: float = 1.0
    reason: str = ""


@dataclass
class _SymState:
    # warehouse stats (Pine-like)
    pos_cost_usdt: float = 0.0
    pos_size: float = 0.0
    avg_price: Optional[float] = None
    num_buys: int = 0
    last_fill_price: Optional[float] = None
    next_level_price: Optional[float] = None

    # LIFO lots (qty, entry_price)
    lots: List[Tuple[float, float]] = None  # type: ignore

    # trailing for full TP
    trailing_active: bool = False
    trailing_max: Optional[float] = None

    # cycle control
    reset_pending: bool = False  # after full close, we want restart

    # pending entry shift after TP_PARTIAL (autoMerge approximation)
    pending_new_entry: Optional[float] = None

    def __post_init__(self):
        if self.lots is None:
            self.lots = []


class CryptomineCLimit14Robust:
    """Python port (approximation) of PineScript "C - limit 14 (robust)"."""

    def __init__(self, cfg: Dict[str, Any]):
        sp = cfg.get("strategy_params", {}) or {}

        # --- TOR inputs ---
        self.first_buy_usdt      = float(sp.get("firstBuyUSDT", 5.0))
        self.tp_percent          = float(sp.get("tpPercent", 1.1))
        self.callback_percent    = float(sp.get("callbackPercent", 0.2))
        self.margin_call_limit   = int(sp.get("marginCallLimit", 244))
        self.linear_drop_percent = float(sp.get("linearDropPercent", 0.5))

        self.auto_merge          = bool(sp.get("autoMerge", True))
        self.sub_sell_tp_percent = float(sp.get("subSellTPPercent", 1.3))

        # bt2 requires numeric stop price on entry; default is very wide "almost never hit"
        self.stop_percent        = float(sp.get("stopPercent", 99.0))

        # Nonlinear initial drops (lvl2..lvl6)
        self.drop1 = float(sp.get("drop1", 0.3))
        self.drop2 = float(sp.get("drop2", 0.4))
        self.drop3 = float(sp.get("drop3", 0.6))
        self.drop4 = float(sp.get("drop4", 0.8))
        self.drop5 = float(sp.get("drop5", 0.8))

        # Multipliers (lvl2..lvl5), after lvl5 -> 1x
        self.mult2 = float(sp.get("mult2", 1.5))
        self.mult3 = float(sp.get("mult3", 1.0))
        self.mult4 = float(sp.get("mult4", 2.0))
        self.mult5 = float(sp.get("mult5", 3.5))

        # In Pine there are while-loops per bar.
        # Here: 1) DCA can do several adds in one bar if close gaps below multiple levels
        #       2) SUB-sell: only ONE partial sell per bar (backtester limitation)
        self.max_fills_per_bar = int(sp.get("maxFillsPerBar", 6))

        # --- Alert-safe throttle ---
        self.max_signals_window = int(sp.get("maxSignalsWindow", 14))
        self.window_bars        = int(sp.get("windowBars", 6))

        # --- Switches (yaml) ---
        # Якщо signalsThrottleEnabled=false → ліміт сигналів вимикається повністю.
        self.signals_throttle_enabled = bool(sp.get("signalsThrottleEnabled", True))

        # Якщо slEnabled=false → стратегія НЕ ставить SL на вході (і відповідно НЕ рухає його в BE).
        self.sl_enabled = bool(sp.get("slEnabled", True))

        # Optional budget cap (to avoid "infinite" DCA in backtest)
        self.max_budget_frac = float(sp.get("maxBudgetFrac", 1.0))  # 1.0 = no cap
        self.initial_capital = float(cfg.get("initial_capital", cfg.get("initial_equity", 10000.0)))

        # --- runtime state ---
        self._states: Dict[str, _SymState] = {}

        # rolling signals counter (script-wide, not per symbol)
        self._sig_window = deque([0] * self.window_bars, maxlen=self.window_bars)
        self._sig_sum = 0
        self._last_t = None
        self._bar_seq = 0

    # ---------------------
    # Universe / ranking
    # ---------------------
    def universe(self, t, md_map):
        # this strategy is typically single-symbol; keep universe as provided
        return list(md_map.keys())

    def rank(self, t, md_map, universe_syms):
        # no cross-sectional ranking: keep order
        return list(universe_syms)

    # ---------------------
    # Helpers
    # ---------------------
    def _next_bar_time(self):
        t = self._bar_seq
        self._bar_seq += 1
        return t

    def _bar_roll(self, t):
        if self._last_t is None or t != self._last_t:
            # new bar
            dropped = self._sig_window[0]
            self._sig_sum -= dropped
            self._sig_window.append(0)
            self._last_t = t

    def _can_signal(self) -> bool:
        if not self.signals_throttle_enabled:
            return True
        return self._sig_sum + self._sig_window[-1] < self.max_signals_window

    def _register_signal(self, n: int = 1):
        # caller must ensure _can_signal() for each increment (or call with small n)
        if not self.signals_throttle_enabled:
            return
        self._sig_window[-1] += n
        self._sig_sum += n

    def _get_state(self, sym: str) -> _SymState:
        st = self._states.get(sym)
        if st is None:
            st = _SymState()
            self._states[sym] = st
        return st

    def _get_drop_for_next_level(self, num_buys: int) -> float:
        nb = num_buys + 1
        if nb == 2:
            return self.drop1
        if nb == 3:
            return self.drop2
        if nb == 4:
            return self.drop3
        if nb == 5:
            return self.drop4
        if nb == 6:
            return self.drop5
        return self.linear_drop_percent

    def _get_mult_for_next_level(self, num_buys: int) -> float:
        nb = num_buys + 1
        if nb == 2:
            return self.mult2
        if nb == 3:
            return self.mult3
        if nb == 4:
            return self.mult4
        if nb == 5:
            return self.mult5
        return 1.0

    def _next_level(self, last_fill_price: float, num_buys: int) -> float:
        d = self._get_drop_for_next_level(num_buys)
        return last_fill_price * (1.0 - d / 100.0)

    def _get_bar_time(self, row):
        candidates = (
            "t", "ts", "time", "timestamp",
            "open_time", "open_ts",
            "datetime", "date",
            "datetime_utc",
        )

        # dict-like / pandas.Series
        for k in candidates:
            try:
                if hasattr(row, "get"):
                    v = row.get(k, None)
                    if v is not None:
                        return v
            except Exception:
                pass
            try:
                if hasattr(row, "index") and k in row.index:
                    return row[k]
            except Exception:
                pass
            try:
                if isinstance(row, dict) and k in row:
                    return row[k]
            except Exception:
                pass

        # інколи timestamp лежить в name (якщо це pandas.Series з індексом-часом)
        try:
            if hasattr(row, "name") and row.name is not None:
                return row.name
        except Exception:
            pass

        # ФОЛБЕК: часу нема в row — використовуємо синтетичний “час”
        return self._next_bar_time()

    def _entry_tp_sl(self, entry_price: float) -> Tuple[float, Optional[float]]:
        entry_price = float(entry_price)
        tp = entry_price * (1.0 + self.tp_percent / 100.0)

        if not self.sl_enabled:
            return float(tp), None

        sp = max(0.0, min(99.9, float(self.stop_percent)))
        sl = entry_price * (1.0 - sp / 100.0)
        if not (sl > 0.0):
            sl = entry_price * 0.0001
        return float(tp), float(sl)

    # ---------------------
    # Entry
    # ---------------------
    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx=None):
        self._bar_roll(self._get_bar_time(row))
        if not is_opening:
            return None

        st = self._get_state(sym)
        close = float(row["close"])

        # If we just closed the whole warehouse, restart immediately (next open step in same bar is OK)
        # But still respect throttle.
        if (st.reset_pending or (st.pos_size == 0 and len(st.lots) == 0)) and self._can_signal():
            # (re)start new cycle
            st.reset_pending = False
            st.trailing_active = False
            st.trailing_max = None
            st.pending_new_entry = None

            buy_usdt = self.first_buy_usdt
            qty0 = buy_usdt / close

            st.pos_cost_usdt = buy_usdt
            st.pos_size = qty0
            st.avg_price = close
            st.num_buys = 1
            st.last_fill_price = close
            st.next_level_price = self._next_level(st.last_fill_price, st.num_buys)
            st.lots = [(qty0, close)]

            tp, sl = self._entry_tp_sl(close)

            self._register_signal(1)
            return Sig(side="LONG", tp=tp, sl=sl, reason="First Buy_0")

        return None

    # ---------------------
    # Position management
    # ---------------------
    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx=None):
        # FIX: row може не мати 't'
        self._bar_roll(self._get_bar_time(row))
        st = self._get_state(sym)
        close = float(row["close"])

        # Sync: after partial closes the backtester changes pos.qty, so rescale lots.
        if st.pos_size > 0 and pos.qty is not None and pos.qty > 0 and abs(pos.qty - st.pos_size) / st.pos_size > 1e-6:
            ratio = pos.qty / st.pos_size
            st.lots = [(q * ratio, p) for (q, p) in st.lots]
            st.pos_size = float(pos.qty)
            # st.pos_cost_usdt isn't tracked perfectly after partials in this approximation.

        # Apply pending entry shift (autoMerge approximation) AFTER the partial close has been processed by backtester.
        if st.pending_new_entry is not None:
            pos.entry = st.pending_new_entry
            st.avg_price = st.pending_new_entry
            st.pending_new_entry = None

        # Ensure avg price is aligned with backtester position
        if st.avg_price is None:
            st.avg_price = float(pos.entry)
        if st.pos_size <= 0:
            st.pos_size = float(pos.qty)

        # Budget cap (optional)
        max_budget = self.initial_capital * self.max_budget_frac

        # 1) FULL TP (priority)
        tp_price = st.avg_price * (1.0 + self.tp_percent / 100.0)
        tp_hit = close >= tp_price

        if tp_hit:
            if self.callback_percent and self.callback_percent > 0:
                st.trailing_active = True
                st.trailing_max = close if st.trailing_max is None else max(st.trailing_max, close)
                trail_stop = st.trailing_max * (1.0 - self.callback_percent / 100.0)

                if close <= trail_stop and self._can_signal():
                    self._register_signal(1)
                    st.reset_pending = True
                    # clear internal warehouse (we'll rebuild on next entry)
                    st.pos_cost_usdt = 0.0
                    st.pos_size = 0.0
                    st.avg_price = None
                    st.num_buys = 0
                    st.last_fill_price = None
                    st.next_level_price = None
                    st.lots = []
                    st.trailing_active = False
                    st.trailing_max = None
                    return ExitSig(action="TP", exit_price=close, qty_frac=1.0, reason="TP Full (Trailing)")
            else:
                if self._can_signal():
                    self._register_signal(1)
                    st.reset_pending = True
                    st.pos_cost_usdt = 0.0
                    st.pos_size = 0.0
                    st.avg_price = None
                    st.num_buys = 0
                    st.last_fill_price = None
                    st.next_level_price = None
                    st.lots = []
                    st.trailing_active = False
                    st.trailing_max = None
                    return ExitSig(action="TP", exit_price=close, qty_frac=1.0, reason="TP Full")

        # If TP not hit, reset trailing
        if not tp_hit:
            st.trailing_active = False
            st.trailing_max = None

        # 2) DCA buys (close-based approximation)
        fills = 0
        while (
            st.num_buys < self.margin_call_limit
            and fills < self.max_fills_per_bar
            and st.next_level_price is not None
            and close <= st.next_level_price
            and self._can_signal()
        ):
            mult = self._get_mult_for_next_level(st.num_buys)
            buy_usdt = self.first_buy_usdt * mult

            # optional budget cap
            if self.max_budget_frac < 0.999999 and (st.pos_cost_usdt + buy_usdt) > max_budget:
                break

            fill_price = st.next_level_price
            qty_add = buy_usdt / fill_price

            # Update backtester position (VWAP)
            new_cost = float(pos.entry) * float(pos.qty) + buy_usdt
            new_qty = float(pos.qty) + qty_add
            new_entry = new_cost / new_qty

            pos.qty = new_qty
            pos.entry = new_entry

            # Update internal warehouse
            st.pos_cost_usdt += buy_usdt
            st.pos_size = new_qty
            st.avg_price = new_entry
            st.lots.append((qty_add, fill_price))

            st.num_buys += 1
            st.last_fill_price = fill_price
            st.next_level_price = self._next_level(st.last_fill_price, st.num_buys)

            fills += 1
            self._register_signal(1)

        # 3) SUB-SELL (LIFO) — 1 partial per bar
        if st.num_buys > 5 and len(st.lots) > 0 and self._can_signal():
            qty_last, entry_last = st.lots[-1]
            last_lot_tp = entry_last * (1.0 + self.sub_sell_tp_percent / 100.0)

            if close >= last_lot_tp:
                qty_total = float(pos.qty)
                qty_close = min(float(qty_last), qty_total)

                if qty_total > 0 and qty_close > 0:
                    qty_frac = max(0.0, min(1.0, qty_close / qty_total))

                    # ---- compute new avg from LIFO lots (NOT from pos.entry) ----
                    total_cost = sum(q * p for (q, p) in st.lots)
                    profit = qty_close * (close - float(entry_last))

                    remaining_qty = qty_total - qty_close
                    remaining_cost = total_cost - qty_close * float(entry_last)

                    if self.auto_merge and remaining_qty > 0:
                        remaining_cost -= profit

                    if remaining_qty > 0:
                        st.pending_new_entry = remaining_cost / remaining_qty
                        # keep internal avg in sync with what we will apply next bar
                        st.avg_price = st.pending_new_entry

                    # ---- KEY FIX: make realized PnL of TP_PARTIAL correct in bt2 ----
                    # bt2 uses pos.entry as cost basis. For a LIFO lot sale, cost basis must be entry_last.
                    pos.entry = float(entry_last)

                    # Update internal warehouse (bt will reduce pos.qty after this returns)
                    st.lots.pop()
                    st.num_buys = max(st.num_buys - 1, 0)
                    st.pos_size = max(0.0, qty_total - qty_close)

                    # Re-anchor grid to new last lot (if any)
                    if len(st.lots) > 0:
                        st.last_fill_price = st.lots[-1][1]
                        st.next_level_price = self._next_level(st.last_fill_price, st.num_buys)
                    else:
                        st.last_fill_price = None
                        st.next_level_price = None

                    self._register_signal(1)
                    return ExitSig(action="TP_PARTIAL", exit_price=close, qty_frac=qty_frac, reason="Sub-sell last lot")

        return None
