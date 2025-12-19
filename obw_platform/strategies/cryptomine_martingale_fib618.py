# -*- coding: utf-8 -*-
"""
Cryptomine Martingale Grid v2 (Fib 0.618 from bottom + risk controls)

Features:
1) Hard cap on total cost: pos_cost_usdt + next_buy <= hardCapFrac * equity_mtm (default 0.99)
2) Rolling pmin (lookback bars) or optional static pmin
3) Adaptive fib: fib_eff = baseFib * confidence(ATR_HTF_pct), where confidence in [confMin..confMax]
4) Optional time-decay: if timeDecayBars == 0 -> off. If >0, after that age the strategy gradually reduces
   position when possible (minProfitForDecay), to reduce "hanging in drawdown".
5) km solved by budget equation only when needed (equity change / pmin update / N change)
6) km_max derived from maxBuyMult (cap of (largest buy / first buy)), to reduce tail risk.

Buy sizing:
  depth(p) = clip((p0 - p)/(p0 - pmin), 0..1)   # below pmin doesn't grow further
  buy_i    = firstBuyUSDT * km ** depth_i

Grid lower bound:
  pn = pmin + fib_eff * (p0 - pmin)
"""

from __future__ import annotations
from typing import Any, Dict, Optional, List, Tuple

from strategies.cryptomine_c_limit14_robust import CryptomineCLimit14Robust


class CryptomineMartingaleFib618V2(CryptomineCLimit14Robust):
    # ----------------------- init -----------------------
    def __init__(self, cfg: Dict[str, Any]):
        super().__init__(cfg)
        sp = cfg.get("strategy_params", {}) or {}

        # ---- Grid params ----
        self.grid_steps = int(sp.get("gridSteps", self.margin_call_limit))  # N (buys incl first)
        if self.grid_steps < 1:
            self.grid_steps = 1

        self.base_fib = float(sp.get("fibLevel", 0.618))  # base fib, from bottom
        self.budget_frac = float(sp.get("budgetFrac", 0.90))
        self.equity_recalc_threshold = float(sp.get("equityRecalcThreshold", 0.05))

        # ---- Hard cap ----
        self.hard_cap_frac = float(sp.get("hardCapFrac", 0.99))  # 99% of equity_mtm
        if self.hard_cap_frac <= 0:
            self.hard_cap_frac = 0.99

        # ---- pmin (rolling or static) ----
        self.pmin_lookback_bars = int(sp.get("pminLookbackBars", 0))  # 0 -> since start
        self.pmin_static = sp.get("pminStatic", None)

        # ---- Adaptive fib via HTF ATR ----
        # HTF ATR computed by aggregating base bars into HTF bars
        self.htf_bars = int(sp.get("atrHTFBars", 60))  # e.g., 60 for 1h when base is 1m
        if self.htf_bars < 1:
            self.htf_bars = 60

        self.atr_len = int(sp.get("atrLen", 14))
        if self.atr_len < 2:
            self.atr_len = 14

        # ATR normalization: atr_pct = atr / close. confidence = clip(atr_pct / atrPctRef, confMin..confMax)
        self.atr_pct_ref = float(sp.get("atrPctRef", 0.02))  # 2% typical; tune per market
        if self.atr_pct_ref <= 0:
            self.atr_pct_ref = 0.02

        self.conf_min = float(sp.get("confMin", 0.35))
        self.conf_max = float(sp.get("confMax", 1.00))
        if self.conf_min < 0:
            self.conf_min = 0.0
        if self.conf_max <= 0:
            self.conf_max = 1.0
        if self.conf_max < self.conf_min:
            self.conf_max = self.conf_min

        # Optional clamp of fib_eff to avoid extreme depths
        self.fib_min = float(sp.get("fibMin", 0.20))
        self.fib_max = float(sp.get("fibMax", self.base_fib))
        if self.fib_max < self.fib_min:
            self.fib_max = self.fib_min

        # ---- km_max via maxBuyMult (tail risk control) ----
        # maxBuyMult caps (largest planned buy) / firstBuyUSDT. Smaller -> safer (less "hang").
        self.max_buy_mult = float(sp.get("maxBuyMult", 8.0))  # e.g., 6..12 typically
        if self.max_buy_mult < 1.0:
            self.max_buy_mult = 1.0

        # absolute km_max override (optional)
        self.km_max_override = sp.get("kmMax", None)

        # ---- time decay option ----
        # If timeDecayBars == 0 -> disabled.
        # If enabled: after position_age >= timeDecayBars, reduce when min profit exists.
        self.time_decay_bars = int(sp.get("timeDecayBars", 0))
        self.decay_sell_frac = float(sp.get("decaySellFrac", 0.10))  # fraction of position to sell
        self.min_profit_for_decay = float(sp.get("minProfitForDecay", 0.001))  # 0.1% above avg

        if self.decay_sell_frac <= 0:
            self.decay_sell_frac = 0.10
        if self.min_profit_for_decay < 0:
            self.min_profit_for_decay = 0.0

        # ---- internal per-symbol tracking for rolling min & HTF ATR ----
        self._pmin: Dict[str, float] = {}
        self._pmin_ring: Dict[str, List[float]] = {}
        self._pmin_ring_i: Dict[str, int] = {}

        # HTF aggregation state per symbol
        self._htf_acc: Dict[str, Dict[str, Any]] = {}   # {count, o,h,l,c}
        self._atr_state: Dict[str, Dict[str, Any]] = {} # {atr, prev_close, ready}

    # ----------------------- utils -----------------------
    @staticmethod
    def _clip(x: float, lo: float, hi: float) -> float:
        return lo if x < lo else hi if x > hi else x

    # ----------------------- rolling pmin -----------------------
    def _update_pmin_flat(self, sym: str, low: float) -> None:
        if self.pmin_static is not None:
            self._pmin[sym] = float(self.pmin_static)
            return

        low = float(low)
        if self.pmin_lookback_bars and self.pmin_lookback_bars > 0:
            buf = self._pmin_ring.get(sym)
            if buf is None:
                buf = [low] * self.pmin_lookback_bars
                self._pmin_ring[sym] = buf
                self._pmin_ring_i[sym] = 0
            i = self._pmin_ring_i[sym]
            buf[i] = low
            i = (i + 1) % self.pmin_lookback_bars
            self._pmin_ring_i[sym] = i
            self._pmin[sym] = min(buf)
        else:
            cur = self._pmin.get(sym)
            self._pmin[sym] = low if (cur is None) else min(cur, low)

    def _get_pmin(self, sym: str) -> Optional[float]:
        if self.pmin_static is not None:
            return float(self.pmin_static)
        return self._pmin.get(sym)

    # ----------------------- HTF ATR -----------------------
    def _update_htf_atr(self, sym: str, row: Dict[str, Any]) -> None:
        o = float(row.get("open", row.get("close")))
        h = float(row.get("high", row.get("close")))
        l = float(row.get("low", row.get("close")))
        c = float(row.get("close"))

        acc = self._htf_acc.get(sym)
        if acc is None:
            acc = {"count": 0, "o": o, "h": h, "l": l, "c": c}
            self._htf_acc[sym] = acc

        if acc["count"] == 0:
            acc["o"] = o
            acc["h"] = h
            acc["l"] = l

        acc["h"] = max(acc["h"], h)
        acc["l"] = min(acc["l"], l)
        acc["c"] = c
        acc["count"] += 1

        if acc["count"] >= self.htf_bars:
            # finalize one HTF candle
            htf_h, htf_l, htf_c = float(acc["h"]), float(acc["l"]), float(acc["c"])
            self._consume_htf_candle(sym, htf_h, htf_l, htf_c)
            acc["count"] = 0  # reset for next HTF candle

    def _consume_htf_candle(self, sym: str, h: float, l: float, c: float) -> None:
        st = self._atr_state.get(sym)
        if st is None:
            st = {"atr": 0.0, "prev_close": c, "ready": False, "n": 0}
            self._atr_state[sym] = st

        prev_close = float(st["prev_close"])
        tr = max(h - l, abs(h - prev_close), abs(l - prev_close))

        n = int(st["n"])
        atr = float(st["atr"])

        # Wilder's ATR init & update
        if not st["ready"]:
            n += 1
            atr = atr + tr
            if n >= self.atr_len:
                atr = atr / float(self.atr_len)
                st["ready"] = True
            st["n"] = n
            st["atr"] = atr
        else:
            atr = (atr * (self.atr_len - 1) + tr) / float(self.atr_len)
            st["atr"] = atr

        st["prev_close"] = c

    def _get_atr_pct(self, sym: str, price: float) -> Optional[float]:
        st = self._atr_state.get(sym)
        if not st or not st.get("ready", False):
            return None
        atr = float(st["atr"])
        if price <= 0:
            return None
        return atr / price

    def _confidence(self, sym: str, price: float) -> float:
        atr_pct = self._get_atr_pct(sym, price)
        if atr_pct is None:
            return self.conf_max  # until ATR ready, assume "ok" (or set to conf_min if you want conservative)
        raw = atr_pct / self.atr_pct_ref
        return self._clip(raw, self.conf_min, self.conf_max)

    def _fib_eff(self, sym: str, price: float) -> float:
        conf = self._confidence(sym, price)
        fib = self.base_fib * conf
        return self._clip(fib, self.fib_min, self.fib_max)

    # ----------------------- grid math -----------------------
    def _pn(self, sym: str, p0: float, pmin: float, price_for_conf: float) -> float:
        fib = self._fib_eff(sym, price_for_conf)
        return pmin + fib * (p0 - pmin)

    def _level_price(self, p0: float, pn: float, idx: int, N: int) -> float:
        if N <= 1:
            return p0
        t = idx / float(N - 1)
        return p0 - t * (p0 - pn)

    def _depth(self, p0: float, p: float, pmin: float) -> float:
        den = (p0 - pmin)
        if den <= 0:
            return 0.0
        d = (p0 - p) / den
        return self._clip(d, 0.0, 1.0)  # below pmin doesn't grow further

    def _sum_budget(self, km: float, p0: float, pmin: float, pn: float, N: int, b: float) -> float:
        s = 0.0
        for idx in range(N):
            p_i = self._level_price(p0, pn, idx, N)
            d_i = self._depth(p0, p_i, pmin)
            s += b * (km ** d_i)
        return s

    def _km_max(self, depth_max: float, b: float, target_budget: float) -> float:
        # Two caps:
        # A) via maxBuyMult: last_buy/first_buy <= maxBuyMult  => km^depth_max <= maxBuyMult
        #    => km <= maxBuyMult^(1/depth_max)
        # B) via budget sanity: last_buy <= target_budget  (extreme cap; rarely binds)
        # Final km_max = min(A,B,override if given)
        if depth_max <= 0:
            km_a = 1.0
        else:
            km_a = self.max_buy_mult ** (1.0 / depth_max)

        km_b = 1.0
        if b > 0 and target_budget > 0 and depth_max > 0:
            # last_buy = b * km^depth_max <= target_budget  => km <= (target_budget/b)^(1/depth_max)
            km_b = (target_budget / b) ** (1.0 / depth_max)

        km = min(km_a, km_b)
        if self.km_max_override is not None:
            try:
                km = min(km, float(self.km_max_override))
            except Exception:
                pass
        if km < 1.0:
            km = 1.0
        return km

    def _solve_km(self, target: float, p0: float, pmin: float, pn: float, N: int, b: float, km_max: float) -> float:
        if target <= 0:
            return 1.0
        s1 = self._sum_budget(1.0, p0, pmin, pn, N, b)
        if s1 >= target:
            return 1.0

        lo, hi = 1.0, min(2.0, km_max)
        # expand hi but not above km_max
        while hi < km_max and self._sum_budget(hi, p0, pmin, pn, N, b) < target:
            hi = min(hi * 2.0, km_max)

        # If even at km_max we can't reach target, return km_max (will under-spend budget).
        if self._sum_budget(hi, p0, pmin, pn, N, b) < target:
            return km_max

        for _ in range(60):
            mid = (lo + hi) / 2.0
            if self._sum_budget(mid, p0, pmin, pn, N, b) >= target:
                hi = mid
            else:
                lo = mid
        return hi

    # ----------------------- hooks -----------------------
    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None):
        # update pmin + HTF ATR
        self._update_pmin_flat(sym, float(row.get("low", row.get("close", 0.0))))
        self._update_htf_atr(sym, row)
        return super().entry_signal(is_opening, sym, row, ctx=ctx)

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx: Optional[Dict[str, Any]] = None):
        self._update_pmin_flat(sym, float(row.get("low", row.get("close", 0.0))))
        self._update_htf_atr(sym, row)
        return self._manage_position_mg(sym, row, pos, ctx=ctx)

    # ----------------------- core logic -----------------------
    def _manage_position_mg(self, sym: str, row: Dict[str, Any], pos, ctx: Optional[Dict[str, Any]] = None):
        close = float(row.get("close"))

        st = self._get_state(sym)

        # If no open pos, defer to parent (initializes state on entry)
        if st.pos_size <= 0:
            return super().manage_position(sym, row, pos, ctx=ctx)

        # reference price p0 fixed at first cycle
        if getattr(st, "mg_p0", None) is None:
            st.mg_p0 = float(st.avg_price) if st.avg_price is not None else float(getattr(pos, "entry", close) or close)
            st.mg_entry_bar = int(getattr(st, "bar_index", 0))

        p0 = float(st.mg_p0)
        pmin = self._get_pmin(sym) or p0
        pmin = min(pmin, p0)

        N = int(getattr(st, "mg_N", 0) or self.grid_steps)
        if N < 1:
            N = 1

        # equity_mtm
        ei = None
        if ctx and isinstance(ctx, dict):
            ei = ctx.get("equity_mtm", None)
        if ei is None:
            # fallback if ctx absent
            ei = float(getattr(st, "equity_fallback", 0.0) or 0.0)
        ei = float(ei) if ei is not None else 0.0

        # adaptive pn
        pn = self._pn(sym, p0, pmin, close)
        pn = self._clip(pn, pmin, p0)

        # depth range up to pn
        depth_max = self._depth(p0, pn, pmin)  # typically 1 - fib_eff (for fib from bottom)
        target_budget = self.budget_frac * ei

        # recompute km only when needed
        need_recalc = False
        if getattr(st, "mg_km", None) is None:
            need_recalc = True

        ei_ref = float(getattr(st, "mg_ei_ref", 0.0) or 0.0)
        if ei_ref > 0 and abs(ei - ei_ref) / ei_ref > self.equity_recalc_threshold:
            need_recalc = True

        pmin_ref = getattr(st, "mg_pmin_ref", None)
        if pmin_ref is None or abs(float(pmin) - float(pmin_ref)) > 1e-12:
            need_recalc = True

        if int(getattr(st, "mg_N_ref", N)) != int(N):
            need_recalc = True

        # also if pn shifts because confidence shifts a lot (optional)
        pn_ref = getattr(st, "mg_pn_ref", None)
        if pn_ref is None or abs(float(pn) - float(pn_ref)) / max(float(pn), 1e-9) > 0.01:
            # 1% move in pn triggers recalculation (lightweight)
            need_recalc = True

        if need_recalc and ei > 0 and p0 > 0:
            b = float(self.first_buy_usdt)
            km_max = self._km_max(depth_max=max(depth_max, 1e-9), b=b, target_budget=max(target_budget, 1e-9))
            km = self._solve_km(target=target_budget, p0=p0, pmin=pmin, pn=pn, N=N, b=b, km_max=km_max)

            st.mg_km = float(km)
            st.mg_km_max = float(km_max)
            st.mg_ei_ref = float(ei)
            st.mg_pmin_ref = float(pmin)
            st.mg_pn_ref = float(pn)
            st.mg_N_ref = int(N)
            st.mg_pn = float(pn)
            st.mg_N = int(N)
        else:
            st.mg_pn = float(pn)
            st.mg_N = int(N)

        # ---- time decay option ----
        if self.time_decay_bars and self.time_decay_bars > 0:
            # we need a bar counter; many OBW states already keep something similar. We'll use st.bar_index if exists.
            bar_i = int(getattr(st, "bar_index", 0))
            entry_bar = int(getattr(st, "mg_entry_bar", bar_i))
            age = max(0, bar_i - entry_bar)

            if age >= self.time_decay_bars:
                # if close above avg by min profit => reduce a bit
                if st.avg_price and close >= float(st.avg_price) * (1.0 + self.min_profit_for_decay):
                    qty = float(pos.qty)
                    sell_qty = qty * self.decay_sell_frac
                    if sell_qty > 0:
                        # Use parent's sell helper (if exists) else do a simple direct adjustment:
                        # We do minimal safe direct update consistent with your engine.
                        # NOTE: exact realized accounting is handled by backtester on order fill events;
                        # if your engine requires emitting explicit signals, we can hook into that later.
                        pos.qty = max(0.0, qty - sell_qty)
                        st.pos_size = float(pos.qty)
                        # keep avg_price unchanged for remaining lots
                        # (If you want exact lot accounting, we can implement FIFO lot trimming.)

        # ---- run parent's manage_position for TP/sub-sell logic (but disable parent's own DCA ladder) ----
        saved_next = st.next_level_price
        st.next_level_price = None
        sig = super().manage_position(sym, row, pos, ctx=ctx)
        st.next_level_price = saved_next

        if pos.qty <= 0:
            return sig

        # ---- our martingale DCA buys with hard cap ----
        km = float(getattr(st, "mg_km", 1.0))
        pn = float(getattr(st, "mg_pn", pn))
        N = int(getattr(st, "mg_N", N))

        def next_level_for(num_buys: int) -> Optional[float]:
            idx = int(num_buys)
            if idx >= N:
                return None
            return self._level_price(p0, pn, idx, N)

        if st.next_level_price is None:
            st.next_level_price = next_level_for(st.num_buys)

        fills = 0
        hard_cap_usdt = self.hard_cap_frac * ei if ei > 0 else None

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

            # HARD CAP: total cost must not exceed 99% ei
            if hard_cap_usdt is not None:
                remaining = max(0.0, hard_cap_usdt - float(st.pos_cost_usdt))
                if remaining <= 0:
                    break
                if buy_usdt > remaining:
                    buy_usdt = remaining

            # optional cap from config maxBudgetFrac (kept for compatibility)
            if self.max_budget_frac < 1.0 and ei > 0:
                cap2 = max(0.0, self.max_budget_frac * ei)
                remaining2 = max(0.0, cap2 - float(st.pos_cost_usdt))
                buy_usdt = min(buy_usdt, remaining2)

            if buy_usdt <= 0:
                break

            fill_price = close
            qty_add = buy_usdt / fill_price

            # Update position (cost-weighted avg)
            new_cost = float(pos.entry) * float(pos.qty) + buy_usdt
            new_qty = float(pos.qty) + qty_add
            new_entry = new_cost / new_qty

            pos.qty = new_qty
            pos.entry = new_entry

            # Update state
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
