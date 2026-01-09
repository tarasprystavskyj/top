# -*- coding: utf-8 -*-
"""cryptomine_c_limit14_fib_hybrid.py

Hybrid strategy:

1) Mechanics = CryptomineCLimit14Robust (grid steps + LIFO sub-sell).
2) Buy sizing is replaced with a Fibonacci/Martingale-inspired curve so that
   the *relative* buy size increases as price approaches a fib target level.

Important note about equity:
----------------------------
The backtester_core_speed3_veto_universe_4_mtm_unrealized.py (in your repo zip)
does not pass an "equity_mtm" context into strategies (ctx=None).
So here we use initial_equity (from YAML) as the sizing reference.

If later you update the backtester to pass ctx['equity_mtm'], we can switch
the reference easily.

Cached columns (from fetch_build_cache_v16/fibcache):
  - pmin_roll_<pminLookbackBars>
  - atr_htf_pct_<atrHTFBars>_<atrLen>
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from strategies.cryptomine_c_limit14_robust import CryptomineCLimit14Robust, ExitSig


@dataclass
class _FibPlan:
    # cycle anchors
    p0: float
    pmin: float
    pn: float
    fib_eff: float
    # sizing
    km: float
    km_max: float
    mults: List[float]          # per buy number (index 0 => buy#1)
    prices: List[float]         # planned level prices (index 0 => buy#1)
    k_fib: int                  # number of buys that lie ABOVE/AT pn (budget targeting region)


class CryptomineCLimit14FibHybrid(CryptomineCLimit14Robust):
    """DCA grid + sub-sell from Robust, but fib-driven sizing."""

    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        sp = cfg.get("strategy_params", {}) or {}

        # ---- Fib sizing params ----
        self.base_fib = float(sp.get("fibLevel", 0.618))

        # ATR/Confidence (optional)
        self.htf_bars = int(sp.get("atrHTFBars", 60))
        self.atr_len = int(sp.get("atrLen", 14))
        self.atr_pct_ref = float(sp.get("atrPctRef", 0.02))
        self.conf_min = float(sp.get("confMin", 0.35))
        self.conf_max = float(sp.get("confMax", 1.0))
        self.fib_min = float(sp.get("fibMin", 0.20))
        self.fib_max = float(sp.get("fibMax", 0.618))

        # rolling pmin lookback (0 => fallback to row['low'] rolling since cycle start)
        self.pmin_lookback_bars = int(sp.get("pminLookbackBars", 43200))

        # target budget fraction to be "planned" until pn
        self.budget_frac = float(sp.get("budgetFrac", 0.90))
        if self.budget_frac <= 0:
            self.budget_frac = 0.90
        if self.budget_frac > 1.0:
            self.budget_frac = 1.0

        # hard cap (fraction of initial_equity)
        self.hard_cap_frac = float(sp.get("hardCapFrac", 0.99))
        if self.hard_cap_frac <= 0:
            self.hard_cap_frac = 0.99

        # tail risk cap: max multiplier at pn
        self.max_buy_mult = float(sp.get("maxBuyMult", 8.0))
        if self.max_buy_mult < 1.0:
            self.max_buy_mult = 1.0
        self.km_max = float(sp.get("kmMax", 0.0) or 0.0)
        if self.km_max <= 0:
            self.km_max = math.log(self.max_buy_mult)

        # for very small targets you may want to scale down ALL buys (including first);
        # default False to keep firstBuyUSDT unchanged.
        self.scale_to_budget = bool(sp.get("scaleToBudget", False))

        # cache keys
        self._pmin_key = f"pmin_roll_{self.pmin_lookback_bars}" if self.pmin_lookback_bars > 0 else None
        self._atr_key = f"atr_htf_pct_{self.htf_bars}_{self.atr_len}" if self.htf_bars > 0 else None

        # per-symbol plan
        self._plan: Dict[str, _FibPlan] = {}
        self._pmin_fallback: Dict[str, float] = {}

        # initial equity reference (the backtester yaml uses initial_equity)
        self.initial_equity = float(cfg.get("initial_equity", cfg.get("initial_capital", 100.0)))

    # ---------------- utils ----------------
    @staticmethod
    def _clip(x: float, lo: float, hi: float) -> float:
        return lo if x < lo else hi if x > hi else x

    def _get_cached_pmin(self, sym: str, row: Dict[str, Any], low: float) -> float:
        if self._pmin_key:
            v = row.get(self._pmin_key, None)
            if v is not None and v == v:
                try:
                    return float(v)
                except Exception:
                    pass
        # fallback rolling min since cycle start
        pm = self._pmin_fallback.get(sym)
        if pm is None:
            pm = low
        else:
            pm = low if low < pm else pm
        self._pmin_fallback[sym] = pm
        return float(pm)

    def _get_cached_atr_pct(self, row: Dict[str, Any]) -> Optional[float]:
        if not self._atr_key:
            return None
        v = row.get(self._atr_key, None)
        if v is None:
            return None
        try:
            v = float(v)
        except Exception:
            return None
        if v != v:  # NaN
            return None
        return v

    def _confidence(self, atr_pct: Optional[float]) -> float:
        if atr_pct is None or self.atr_pct_ref <= 0:
            return self.conf_max
        raw = atr_pct / self.atr_pct_ref
        return self._clip(raw, self.conf_min, self.conf_max)

    def _fib_eff(self, atr_pct: Optional[float]) -> float:
        conf = self._confidence(atr_pct)
        fib = self.base_fib * conf
        return self._clip(fib, self.fib_min, self.fib_max)

    # ---------------- plan building ----------------
    def _simulate_level_prices(self, p0: float) -> List[float]:
        """Simulate planned fill prices using the SAME step-down logic as Robust."""
        prices = [float(p0)]
        last = float(p0)
        num_buys = 1
        while num_buys < self.margin_call_limit:
            nxt = self._next_level(last, num_buys)
            prices.append(float(nxt))
            last = float(nxt)
            num_buys += 1
        return prices

    def _build_plan(self, sym: str, row: Dict[str, Any], p0: float) -> _FibPlan:
        low = float(row.get("low", p0))
        pmin = self._get_cached_pmin(sym, row, low)
        atr_pct = self._get_cached_atr_pct(row)
        fib_eff = self._fib_eff(atr_pct)

        # fib target level pn (measured from bottom): pn = pmin + fib*(p0 - pmin)
        pn = pmin + fib_eff * (p0 - pmin)
        if pn > p0:
            pn = p0
        if pn < pmin:
            pn = pmin

        # planned prices (buy#1..buy#margin_call_limit)
        prices = self._simulate_level_prices(p0)

        # count K buys until we reach pn (inclusive)
        k_fib = 1
        for i in range(1, len(prices)):
            if prices[i] <= pn:
                k_fib = i + 1
                break
            k_fib = i + 1

        # normalized depths for the first k_fib buys
        den = (p0 - pn)
        if den <= 0:
            depths = [0.0 for _ in range(k_fib)]
        else:
            depths = [self._clip((p0 - prices[i]) / den, 0.0, 1.0) for i in range(k_fib)]

        # budget target for those buys
        target = self.budget_frac * self.initial_equity
        b0 = float(self.first_buy_usdt)

        def sum_budget(km: float) -> float:
            s = 0.0
            for d in depths:
                s += math.exp(km * d)
            return b0 * s

        lo, hi = 0.0, float(self.km_max)
        s_lo = sum_budget(lo)
        s_hi = sum_budget(hi)

        if target <= s_lo:
            km = 0.0
        elif target >= s_hi:
            km = hi
        else:
            km = 0.5 * (lo + hi)
            for _ in range(28):
                s_mid = sum_budget(km)
                if s_mid < target:
                    lo = km
                else:
                    hi = km
                km = 0.5 * (lo + hi)

        # build multipliers for all buys
        mults: List[float] = []
        scale = 1.0
        if self.scale_to_budget:
            denom = sum_budget(km)
            if denom > 0:
                scale = target / denom

        # first k_fib buys follow exp curve; deeper buys revert to 1x
        for i in range(len(prices)):
            if i < k_fib:
                d = depths[i]
                m = math.exp(km * d)
            else:
                m = 1.0
            if m > self.max_buy_mult:
                m = self.max_buy_mult
            mults.append(float(m) * scale)

        return _FibPlan(
            p0=float(p0),
            pmin=float(pmin),
            pn=float(pn),
            fib_eff=float(fib_eff),
            km=float(km),
            km_max=float(self.km_max),
            mults=mults,
            prices=prices,
            k_fib=int(k_fib),
        )

    # ---------------- overrides ----------------
    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx=None):
        sig = super().entry_signal(is_opening, sym, row, ctx=ctx)
        if sig is None:
            return None

        # super() already executed the first buy; create a sizing plan for this cycle
        try:
            p0 = float(row["close"])
        except Exception:
            return sig

        self._plan[sym] = self._build_plan(sym, row, p0)
        return sig

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx=None):
        self._bar_roll(self._get_bar_time(row))
        st = self._get_state(sym)
        close = float(row["close"])

        # ensure plan exists
        if sym not in self._plan and st.num_buys > 0:
            self._plan[sym] = self._build_plan(sym, row, float(st.last_fill_price or close))

        # Sync lots after partial closes
        if st.pos_size > 0 and pos.qty is not None and pos.qty > 0 and abs(pos.qty - st.pos_size) / st.pos_size > 1e-6:
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

        # hard cap on total deployed cost (reference = initial_equity, since ctx is not provided)
        hard_cap = self.hard_cap_frac * self.initial_equity

        # 1) FULL TP (priority) — unchanged
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
                    st.pos_cost_usdt = 0.0
                    st.pos_size = 0.0
                    st.avg_price = None
                    st.num_buys = 0
                    st.last_fill_price = None
                    st.next_level_price = None
                    st.lots = []
                    st.trailing_active = False
                    st.trailing_max = None
                    self._plan.pop(sym, None)
                    self._pmin_fallback.pop(sym, None)
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
                    self._plan.pop(sym, None)
                    self._pmin_fallback.pop(sym, None)
                    return ExitSig(action="TP", exit_price=close, qty_frac=1.0, reason="TP Full")

        if not tp_hit:
            st.trailing_active = False
            st.trailing_max = None

        # 2) DCA buys — ONLY sizing is changed
        fills = 0
        plan = self._plan.get(sym, None)
        while (
            st.num_buys < self.margin_call_limit
            and fills < self.max_fills_per_bar
            and st.next_level_price is not None
            and close <= st.next_level_price
            and self._can_signal()
        ):
            nb = st.num_buys + 1  # next buy number (2..)
            if plan and (nb - 1) < len(plan.mults):
                mult = float(plan.mults[nb - 1])
            else:
                mult = float(self._get_mult_for_next_level(st.num_buys))

            buy_usdt = float(self.first_buy_usdt) * mult

            # hard cap
            if (st.pos_cost_usdt + buy_usdt) > hard_cap:
                break

            fill_price = float(st.next_level_price)
            qty_add = buy_usdt / fill_price

            new_cost = float(pos.entry) * float(pos.qty) + buy_usdt
            new_qty = float(pos.qty) + qty_add
            new_entry = new_cost / new_qty

            pos.qty = new_qty
            pos.entry = new_entry

            st.pos_cost_usdt += buy_usdt
            st.pos_size = new_qty
            st.avg_price = new_entry
            st.lots.append((qty_add, fill_price))

            st.num_buys += 1
            st.last_fill_price = fill_price
            st.next_level_price = self._next_level(st.last_fill_price, st.num_buys)

            fills += 1
            self._register_signal(1)

        # 3) SUB-SELL (LIFO) — unchanged
        if st.num_buys > 5 and len(st.lots) > 0 and self._can_signal():
            qty_last, entry_last = st.lots[-1]
            last_lot_tp = entry_last * (1.0 + self.sub_sell_tp_percent / 100.0)

            if close >= last_lot_tp:
                qty_total = float(pos.qty)
                qty_close = min(float(qty_last), qty_total)

                if qty_total > 0 and qty_close > 0:
                    qty_frac = max(0.0, min(1.0, qty_close / qty_total))

                    total_cost = sum(q * p for (q, p) in st.lots)
                    profit = qty_close * (close - float(entry_last))

                    remaining_qty = qty_total - qty_close
                    remaining_cost = total_cost - qty_close * float(entry_last)

                    if self.auto_merge and remaining_qty > 0:
                        remaining_cost -= profit

                    if remaining_qty > 0:
                        st.pending_new_entry = remaining_cost / remaining_qty
                        st.avg_price = st.pending_new_entry

                    pos.entry = float(entry_last)

                    st.lots.pop()
                    st.num_buys = max(st.num_buys - 1, 0)
                    st.pos_size = max(0.0, qty_total - qty_close)

                    if len(st.lots) > 0:
                        st.last_fill_price = st.lots[-1][1]
                        st.next_level_price = self._next_level(st.last_fill_price, st.num_buys)
                    else:
                        st.last_fill_price = None
                        st.next_level_price = None

                    self._register_signal(1)
                    return ExitSig(action="TP_PARTIAL", exit_price=close, qty_frac=qty_frac, reason="Sub-sell last lot")

        return None
