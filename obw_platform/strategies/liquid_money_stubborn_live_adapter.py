from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from types import SimpleNamespace
from typing import Any, Dict, List, Optional


@dataclass
class _SymState:
    highs: deque
    lows: deque
    fast: Optional[float] = None
    slow: Optional[float] = None
    side: int = 0
    exposure_pct: float = 0.0
    avg_entry: float = 0.0
    entry_bar: int = 0
    bar_i: int = 0
    last_bar_key: str = ""
    bootstrapped: bool = False
    buy: bool = False
    sell: bool = False
    long_target: float = 0.0
    short_target: float = 0.0
    force_exit: bool = False


class LiquidMoneyStubbornLiveAdapter:
    """Paper/live adapter for tuned liquid-money stubborn champions.

    This class intentionally keeps all exchange actions inside the paper/live
    runner. It only emits Signal/Exit-like objects expected by the existing
    runner.
    """

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        sp = cfg.get("strategy_params", {}) or {}
        self.params_by_symbol = sp.get("symbols", {}) or {}
        self.default_params = sp.get("default", {}) or {}
        self.symbols = list((cfg.get("universe", {}) or {}).get("allow", []) or self.params_by_symbol.keys())
        self.initial_equity = float(cfg.get("initial_equity", sp.get("initial_equity", 100.0)))
        self.position_notional = float(cfg.get("notional", cfg.get("position_notional", 1.0)))
        self.states: Dict[str, _SymState] = {}

    def universe(self, t, md_slice) -> List[str]:
        available = list(md_slice.keys())
        if not self.symbols:
            return available
        allowed = set(self.symbols)
        return [s for s in available if s in allowed]

    def rank(self, t, md_slice, symbols: List[str]) -> List[str]:
        return list(symbols)

    def _params(self, sym: str) -> Dict[str, Any]:
        out = dict(self.default_params)
        out.update(self.params_by_symbol.get(sym, {}) or {})
        return out

    def _state(self, sym: str) -> _SymState:
        if sym not in self.states:
            p = self._params(sym)
            lookback = int(p.get("lookback", 360))
            self.states[sym] = _SymState(highs=deque(maxlen=max(2, lookback)), lows=deque(maxlen=max(2, lookback)))
        return self.states[sym]

    def _existing_symbol_notional(self, pf, sym: str) -> float:
        total = 0.0
        for pos in list(getattr(pf, "positions", []) or []):
            if getattr(pos, "symbol", None) == sym:
                total += float(getattr(pos, "notional", 0.0) or 0.0)
        return total

    def _existing_symbol_side(self, pf, sym: str) -> int:
        for pos in list(getattr(pf, "positions", []) or []):
            if getattr(pos, "symbol", None) == sym:
                return 1 if str(getattr(pos, "side", "LONG")).upper() == "LONG" else -1
        return 0

    def _leverage(self, p: Dict[str, Any], side: int) -> float:
        if side > 0:
            return float(p.get("leverage_long", p.get("leverage", 1.5)))
        if side < 0:
            return float(p.get("leverage_short", p.get("leverage", 1.5)))
        return float(p.get("leverage", p.get("leverage_long", 1.5)))

    def _min_step_pct(self, p: Dict[str, Any], side: int) -> float:
        step_pct = float(p.get("min_step_pct", 1.0))
        step_notional = p.get("min_step_notional_usdt")
        if step_notional is None:
            return step_pct
        lev = self._leverage(p, side)
        denom = max(self.initial_equity * lev, 1e-12)
        return max(0.0, float(step_notional) / denom * 100.0)

    def _sync_from_portfolio(self, st: _SymState, sym: str, pf) -> None:
        if pf is None:
            return
        notional = self._existing_symbol_notional(pf, sym)
        if notional <= 1e-12:
            st.side = 0
            st.exposure_pct = 0.0
            st.avg_entry = 0.0
            return
        st.side = self._existing_symbol_side(pf, sym) or st.side
        lev = self._leverage(self._params(sym), st.side)
        denom = max(self.initial_equity * lev, 1e-12)
        st.exposure_pct = max(0.0, min(100.0, notional / denom * 100.0))
        weighted = 0.0
        total = 0.0
        for pos in list(getattr(pf, "positions", []) or []):
            if getattr(pos, "symbol", None) != sym:
                continue
            n = float(getattr(pos, "notional", 0.0) or 0.0)
            px = float(getattr(pos, "entry_price", getattr(pos, "entry", 0.0)) or 0.0)
            weighted += n * px
            total += n
        if total > 0:
            st.avg_entry = weighted / total

    def _update_emas(self, st: _SymState, px: float, fast_len: int, slow_len: int) -> tuple[float, float]:
        alpha_fast = 2.0 / (fast_len + 1.0)
        alpha_slow = 2.0 / (slow_len + 1.0)
        if st.fast is None:
            st.fast = px
            st.slow = px
            return px, px
        fast_prev = st.fast
        slow_prev = st.slow if st.slow is not None else px
        st.fast = alpha_fast * px + (1.0 - alpha_fast) * st.fast
        st.slow = alpha_slow * px + (1.0 - alpha_slow) * slow_prev
        return fast_prev, slow_prev

    def _bootstrap_from_tail(self, st: _SymState, row: Dict[str, Any], fast_len: int, slow_len: int) -> None:
        if st.bootstrapped:
            return
        tail = row.get("_ohlcv_tail")
        if not isinstance(tail, list) or len(tail) < 2:
            return
        for rec in tail[:-1]:
            try:
                px = float(rec.get("close") or 0.0)
                hi = float(rec.get("high") or px)
                lo = float(rec.get("low") or px)
            except Exception:
                continue
            if px <= 0:
                continue
            self._update_emas(st, px, fast_len, slow_len)
            st.highs.append(hi)
            st.lows.append(lo)
            st.bar_i += 1
        st.bootstrapped = True

    def _on_bar(self, sym: str, row: Dict[str, Any], pf=None) -> _SymState:
        st = self._state(sym)
        bar_key = str(row.get("datetime_utc") or row.get("timestamp") or row.get("close_time") or "")
        px = float(row.get("close") or 0.0)
        hi = float(row.get("high") or px)
        lo = float(row.get("low") or px)
        if bar_key and bar_key == st.last_bar_key:
            self._sync_from_portfolio(st, sym, pf)
            return st

        p = self._params(sym)
        fast_len = int(p.get("fast_len", 5))
        slow_len = int(p.get("slow_len", 55))
        self._bootstrap_from_tail(st, row, fast_len, slow_len)

        prior_hi = max(st.highs) if st.highs else hi
        prior_lo = min(st.lows) if st.lows else lo

        fast_prev, slow_prev = self._update_emas(st, px, fast_len, slow_len)

        st.buy = bool(fast_prev <= slow_prev and st.fast > st.slow)
        st.sell = bool(fast_prev >= slow_prev and st.fast < st.slow)

        rng = max(prior_hi - prior_lo, max(abs(px) * 1e-6, 1e-12))
        cap = float(p.get("interval_cap_pct", 35.0))
        gamma = float(p.get("gamma", 1.0))
        long_score = max(0.0, (prior_hi - px) / rng)
        short_score = max(0.0, (px - prior_lo) / rng)
        st.long_target = min(100.0, cap * (long_score ** gamma))
        st.short_target = min(100.0, cap * (short_score ** gamma))

        self._sync_from_portfolio(st, sym, pf)
        if st.side != 0 and st.exposure_pct > 0:
            lev = self._leverage(p, st.side)
            notional = self.initial_equity * lev * st.exposure_pct / 100.0
            if st.side > 0:
                mtm = notional * (px / max(st.avg_entry, 1e-12) - 1.0)
            else:
                mtm = notional * (st.avg_entry / max(px, 1e-12) - 1.0)
            mtm_pct = mtm / max(self.initial_equity, 1e-12) * 100.0
            max_hold = int(p.get("max_hold_bars", 0))
            loss_cut = float(p.get("loss_cut_mtm_pct", 0.0))
            st.force_exit = (max_hold > 0 and st.bar_i - st.entry_bar >= max_hold) or (loss_cut < 0 and mtm_pct <= loss_cut)
        else:
            st.force_exit = False

        st.highs.append(hi)
        st.lows.append(lo)
        st.bar_i += 1
        st.last_bar_key = bar_key or f"bar_{st.bar_i}"
        return st

    def _entry_signal_obj(self, side: str, px: float, reason: str):
        if side == "LONG":
            tp = px * 100.0
            sl = px * 0.0001
        else:
            tp = px * 0.0001
            sl = px * 100.0
        return SimpleNamespace(side=side, take_profit=tp, stop_price=sl, reason=reason, tags=["liquid_money", "paper"])

    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx=None):
        if not is_opening:
            return None
        pf = (ctx or {}).get("portfolio") if isinstance(ctx, dict) else None
        st = self._on_bar(sym, row, pf)
        p = self._params(sym)
        px = float(row.get("close") or 0.0)
        if len(st.highs) < min(20, int(p.get("lookback", 360))):
            return None

        # Entries/adds happen only on same-side signals. Opposite signals are
        # handled by manage_position as only-win scale-outs or forced exits.
        if st.buy and st.side >= 0:
            min_step = self._min_step_pct(p, 1)
            add_pct = max(min_step, st.long_target - st.exposure_pct)
            if add_pct > 0 and st.exposure_pct < 100.0:
                if st.side == 0:
                    st.entry_bar = st.bar_i
                    st.avg_entry = px
                st.side = 1
                st.exposure_pct = min(100.0, st.exposure_pct + add_pct)
                return self._entry_signal_obj("LONG", px, f"LiquidMoney BUY add target={st.long_target:.2f}")
        if st.sell and st.side <= 0:
            min_step = self._min_step_pct(p, -1)
            add_pct = max(min_step, st.short_target - st.exposure_pct)
            if add_pct > 0 and st.exposure_pct < 100.0:
                if st.side == 0:
                    st.entry_bar = st.bar_i
                    st.avg_entry = px
                st.side = -1
                st.exposure_pct = min(100.0, st.exposure_pct + add_pct)
                return self._entry_signal_obj("SHORT", px, f"LiquidMoney SELL add target={st.short_target:.2f}")
        return None

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx=None):
        pf = (ctx or {}).get("portfolio") if isinstance(ctx, dict) else None
        st = self._on_bar(sym, row, pf)
        p = self._params(sym)
        px = float(row.get("close") or 0.0)
        min_profit = float(p.get("min_profit_pct", 0.08)) / 100.0

        pos_side = 1 if str(getattr(pos, "side", "LONG")).upper() == "LONG" else -1
        entry = float(getattr(pos, "entry_price", getattr(pos, "entry", st.avg_entry)) or st.avg_entry or px)
        if st.force_exit:
            st.side = 0
            st.exposure_pct = 0.0
            st.avg_entry = 0.0
            return SimpleNamespace(action="EXIT", exit_price=px, reason="LiquidMoney forced loss/hold")

        if pos_side > 0 and st.sell:
            if px > entry * (1.0 + min_profit):
                return SimpleNamespace(action="EXIT", exit_price=px, reason="LiquidMoney only-win long close")
        if pos_side < 0 and st.buy:
            if px < entry * (1.0 - min_profit):
                return SimpleNamespace(action="EXIT", exit_price=px, reason="LiquidMoney only-win short close")
        return None
