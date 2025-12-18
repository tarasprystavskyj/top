# -*- coding: utf-8 -*-
"""
Cryptomine Martingale Grid (Fib 0.618 from bottom) for OBW backtester.

Goal:
- Allocate ~budgetFrac of equity_mtm into a preplanned DCA ladder from p0 down to pn,
  where pn is the Fib level measured from the bottom:
      pn = pmin + fibLevel * (p0 - pmin)     (fibLevel default 0.618)
- Buy sizing uses a martingale-like multiplier that *does not grow below pmin* by clipping depth:
      depth(p) = clip((p0 - p)/(p0 - pmin), 0..1)
      buy_i    = firstBuyUSDT * km ** depth_i
- km is solved once and only recalculated when:
    * abs(ei - ei_ref)/ei_ref > equityRecalcThreshold
    * pmin updates
    * N changes
"""

from __future__ import annotations
from typing import Any, Dict, Optional, List

from strategies.cryptomine_c_limit14_robust import CryptomineCLimit14Robust


class CryptomineMartingaleFib618(CryptomineCLimit14Robust):
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        sp = cfg.get("strategy_params", {}) or {}

        # --- Martingale grid params ---
        self.grid_steps = int(sp.get("gridSteps", self.margin_call_limit))  # max total buys (incl first)
        self.fib_level = float(sp.get("fibLevel", 0.618))
        self.budget_frac = float(sp.get("budgetFrac", 0.90))
        self.equity_recalc_threshold = float(sp.get("equityRecalcThreshold", 0.05))

        # Rolling min while flat (bars). If <=0, uses min since strategy start.
        self.pmin_lookback_bars = int(sp.get("pminLookbackBars", 0))
        self.pmin_static = sp.get("pminStatic", None)

        self._global_min: Dict[str, float] = {}
        self._global_min_ring: Dict[str, List[float]] = {}  # lows ring buffer
        self._global_min_ring_i: Dict[str, int] = {}

    # --------- helpers ----------
    @staticmethod
    def _clip(x: float, lo: float, hi: float) -> float:
        return lo if x < lo else hi if x > hi else x

    def _update_pmin_flat(self, sym: str, low: float) -> None:
        if self.pmin_static is not None:
            self._global_min[sym] = float(self.pmin_static)
            return

        low = float(low)
        if self.pmin_lookback_bars and self.pmin_lookback_bars > 0:
            buf = self._global_min_ring.get(sym)
            if buf is None:
                buf = [low] * self.pmin_lookback_bars
                self._global_min_ring[sym] = buf
                self._global_min_ring_i[sym] = 0
            i = self._global_min_ring_i[sym]
            buf[i] = low
            i = (i + 1) % self.pmin_lookback_bars
            self._global_min_ring_i[sym] = i
            self._global_min[sym] = min(buf)
        else:
            cur = self._global_min.get(sym)
            self._global_min[sym] = low if (cur is None) else min(cur, low)

    def _get_pmin(self, sym: str) -> Optional[float]:
        if self.pmin_static is not None:
            return float(self.pmin_static)
        return self._global_min.get(sym)

    def _fib_pn(self, p0: float, pmin: float) -> float:
        # fib measured from bottom: pmin + fib*(p0-pmin)
        return pmin + self.fib_level * (p0 - pmin)

    def _level_price(self, p0: float, pn: float, idx: int, N: int) -> float:
        # idx in [0..N-1], N>=1, linear in price p0 -> pn
        if N <= 1:
            return p0
        t = idx / float(N - 1)
        return p0 - t * (p0 - pn)

    def _depth(self, p0: float, p: float, pmin: float) -> float:
        den = (p0 - pmin)
        if den <= 0:
            return 0.0
        d = (p0 - p) / den
        return self._clip(d, 0.0, 1.0)  # <-- critical: below pmin doesn't grow further

    def _sum_budget(self, km: float, p0: float, pmin: float, pn: float, N: int, b: float) -> float:
        s = 0.0
        for idx in range(N):
            p_i = self._level_price(p0, pn, idx, N)
            d_i = self._depth(p0, p_i, pmin)
            s += b * (km ** d_i)
        return s

    def _solve_km(self, target: float, p0: float, pmin: float, pn: float, N: int, b: float) -> float:
        if target <= 0:
            return 1.0
        s1 = self._sum_budget(1.0, p0, pmin, pn, N, b)
        if s1 >= target:
            return 1.0

        lo, hi = 1.0, 2.0
        while self._sum_budget(hi, p0, pmin, pn, N, b) < target:
            hi *= 2.0
            if hi > 1e6:
                return hi

        for _ in range(60):
            mid = (lo + hi) / 2.0
            if self._sum_budget(mid, p0, pmin, pn, N, b) >= target:
                hi = mid
            else:
                lo = mid
        return hi

    # --------- interface overrides ----------
    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None):
        low = float(row.get("low", row.get("close", 0.0)))
        self._update_pmin_flat(sym, low)
        return super().entry_signal(is_opening, sym, row, ctx=ctx)

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx: Optional[Dict[str, Any]] = None):
        low = float(row.get("low", row.get("close", 0.0)))
        self._update_pmin_flat(sym, low)
        return self._manage_position_mg(sym, row, pos, ctx=ctx)

    def _manage_position_mg(self, sym: str, row: Dict[str, Any], pos, ctx: Optional[Dict[str, Any]] = None):
        close = float(row.get("close"))
        st = self._get_state(sym)

        # If no open position state, defer to parent (it will init st on entry bar)
        if st.pos_size <= 0:
            return super().manage_position(sym, row, pos, ctx=ctx)

        # --- Ensure martingale params computed ---
        if getattr(st, "mg_p0", None) is None:
            st.mg_p0 = float(st.avg_price) if st.avg_price is not None else float(getattr(pos, "entry", close) or close)

        p0 = float(st.mg_p0)
        pmin = self._get_pmin(sym) or p0
        pmin = min(pmin, p0)

        N = int(getattr(st, "mg_N", 0) or self.grid_steps or self.margin_call_limit)
        if N < 1:
            N = 1

        ei = None
        if ctx and isinstance(ctx, dict):
            ei = ctx.get("equity_mtm", None)
        if ei is None:
            ei = float(getattr(st, "equity_fallback", 0.0) or 0.0)

        pn = self._fib_pn(p0, pmin)
        pn = self._clip(pn, pmin, p0)

        need_recalc = False
        if getattr(st, "mg_km", None) is None:
            need_recalc = True

        ei_ref = float(getattr(st, "mg_ei_ref", 0.0) or 0.0)
        if ei_ref > 0 and ei is not None:
            if abs(float(ei) - ei_ref) / ei_ref > self.equity_recalc_threshold:
                need_recalc = True
        if getattr(st, "mg_pmin_ref", None) is None or abs(float(pmin) - float(getattr(st, "mg_pmin_ref", pmin))) > 1e-12:
            need_recalc = True
        if int(getattr(st, "mg_N_ref", N)) != int(N):
            need_recalc = True

        if need_recalc and ei is not None and float(ei) > 0:
            target = self.budget_frac * float(ei)
            b = float(self.first_buy_usdt)
            km = self._solve_km(target, p0, pmin, pn, N, b)
            st.mg_km = float(km)
            st.mg_ei_ref = float(ei)
            st.mg_pmin_ref = float(pmin)
            st.mg_N_ref = int(N)
            st.mg_pn = float(pn)
            st.mg_N = int(N)
        else:
            st.mg_pn = float(pn)
            st.mg_N = int(N)

        return self._manage_position_parentlike(sym, row, pos, ctx=ctx)

    def _manage_position_parentlike(self, sym: str, row: Dict[str, Any], pos, ctx: Optional[Dict[str, Any]] = None):
        close = float(row.get("close"))
        st = self._get_state(sym)

        # Run parent manage_position but temporarily disable parent's DCA ladder
        saved_next = st.next_level_price
        st.next_level_price = None
        sig = super().manage_position(sym, row, pos, ctx=ctx)

        if pos.qty <= 0:
            st.next_level_price = saved_next
            return sig

        st.next_level_price = saved_next

        # ---- Our DCA buys ----
        p0 = float(getattr(st, "mg_p0", None) or (float(st.avg_price) if st.avg_price is not None else float(getattr(pos, "entry", close) or close)))
        pmin = float(getattr(st, "mg_pmin_ref", self._get_pmin(sym) or p0))
        pn = float(getattr(st, "mg_pn", self._fib_pn(p0, pmin)))
        N = int(getattr(st, "mg_N", self.grid_steps or self.margin_call_limit))
        km = float(getattr(st, "mg_km", 1.0))

        def next_level_for(num_buys: int) -> Optional[float]:
            idx = int(num_buys)  # 1..N-1
            if idx >= N:
                return None
            return self._level_price(p0, pn, idx, N)

        if st.next_level_price is None:
            st.next_level_price = next_level_for(st.num_buys)

        fills = 0
        while (
            st.num_buys < N
            and fills < self.max_fills_per_bar
            and st.next_level_price is not None
            and close <= st.next_level_price
            and self._can_signal()
        ):
            idx = int(st.num_buys)
            p_i = self._level_price(p0, pn, idx, N)
            d_i = self._depth(p0, p_i, pmin)
            buy_usdt = float(self.first_buy_usdt) * (km ** d_i)

            # optional budget cap (same spirit as parent)
            if self.max_budget_frac < 1.0 and ctx and isinstance(ctx, dict):
                eq = float(ctx.get("equity_mtm", ctx.get("equity_realized", 0.0)) or 0.0)
                cap = max(0.0, self.max_budget_frac * eq)
                if st.pos_cost_usdt + buy_usdt > cap:
                    buy_usdt = max(0.0, cap - st.pos_cost_usdt)

            if buy_usdt <= 0:
                break

            fill_price = close
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
            st.next_level_price = next_level_for(st.num_buys)

            fills += 1
            self._register_signal(1)

        return sig
