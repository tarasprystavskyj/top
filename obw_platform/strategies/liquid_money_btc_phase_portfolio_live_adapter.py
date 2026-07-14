from __future__ import annotations

import copy
import math
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence

import numpy as np
import pandas as pd

from strategies.liquid_money_stubborn_live_adapter import LiquidMoneyStubbornLiveAdapter


def _safe(sym: str) -> str:
    return str(sym).replace("/", "_").replace(":", "_")


def _capped_normalize(raw: Sequence[float], min_weight: float, max_weight: float) -> np.ndarray:
    scores = np.asarray(raw, dtype=np.float64).copy()
    n = len(scores)
    if n <= 0:
        return scores
    scores[~np.isfinite(scores)] = 0.0
    scores = np.maximum(scores, 0.0)
    if scores.sum() <= 1e-12:
        scores[:] = 1.0

    min_weight = max(0.0, float(min_weight))
    max_weight = max(min_weight, float(max_weight))
    if min_weight * n > 1.0:
        min_weight = 1.0 / n
    if max_weight * n < 1.0:
        max_weight = 1.0 / n

    out = np.full(n, min_weight, dtype=np.float64)
    remaining = 1.0 - min_weight * n
    cap_extra = np.full(n, max_weight - min_weight, dtype=np.float64)
    active = np.ones(n, dtype=bool)
    scores = scores / max(float(scores.sum()), 1e-12)
    for _ in range(n + 1):
        if remaining <= 1e-12 or not bool(active.any()):
            break
        idx = np.where(active)[0]
        active_scores = scores[idx]
        if active_scores.sum() <= 1e-12:
            active_scores = np.ones_like(active_scores)
        alloc = remaining * active_scores / max(float(active_scores.sum()), 1e-12)
        over = alloc > cap_extra[idx] + 1e-12
        if not bool(over.any()):
            out[idx] += alloc
            remaining = 0.0
            break
        capped = idx[over]
        out[capped] += cap_extra[capped]
        remaining -= float(cap_extra[capped].sum())
        active[capped] = False
    if remaining > 1e-9:
        room = np.maximum(0.0, max_weight - out)
        if room.sum() > 1e-12:
            out += remaining * room / room.sum()
    return out / max(float(out.sum()), 1e-12)


class BTCPhaseAllocator:
    """Daily allocator keyed by the previous day's BTC velocity/acceleration phase."""

    def __init__(self, cfg: Mapping[str, Any], symbols: Sequence[str]):
        acfg = dict(cfg.get("btc_phase_allocator", {}) or {})
        self.symbols = list(symbols)
        self.btc_symbol = str(acfg.get("btc_symbol") or "BTC/USDT:USDT")
        self.lookback_days = int(acfg.get("lookback_days", 30))
        self.beta = float(acfg.get("beta", 4.0))
        self.min_weight = float(acfg.get("min_weight", 0.0))
        self.max_weight = float(acfg.get("max_weight", 0.60))
        self.smooth = float(acfg.get("smooth", 0.0))
        self.base_mode = str(acfg.get("base_mode") or "equal")
        self.min_same_phase_days = int(acfg.get("min_same_phase_days", 7))
        self.static_weights = self._weights_from_mapping(acfg.get("bootstrap_weights", {}) or {})

        self.daily: pd.DataFrame | None = None
        self.asset_rets: np.ndarray | None = None
        self.signal_phases: np.ndarray | None = None
        self.hist_last_day: pd.Timestamp | None = None
        self.hist_prevday_phase: str = "neutral"
        self.hist_phase_by_day: Dict[str, str] = {}
        self.hist_btc_close_by_day: Dict[str, float] = {}
        self.live_btc_close_by_day: Dict[str, float] = {}
        self.day_cache: Dict[str, Dict[str, float]] = {}
        self.weight_cache: Dict[str, np.ndarray] = {}

        daily_path = acfg.get("history_daily_csv")
        if daily_path and Path(str(daily_path)).exists():
            self._load_history(Path(str(daily_path)))

    def _weights_from_mapping(self, weights: Mapping[str, Any]) -> Dict[str, float]:
        vals = [float(weights.get(sym, weights.get(_safe(sym), 1.0 / max(len(self.symbols), 1)))) for sym in self.symbols]
        norm = _capped_normalize(vals, 0.0, 1.0)
        return {sym: float(norm[i]) for i, sym in enumerate(self.symbols)}

    def _load_history(self, path: Path) -> None:
        daily = pd.read_csv(path)
        if "day" not in daily.columns or "BTC_close" not in daily.columns:
            return
        ret_cols = [f"ret_{_safe(sym)}" for sym in self.symbols]
        if any(c not in daily.columns for c in ret_cols):
            return
        daily["day_ts"] = pd.to_datetime(daily["day"], utc=True).dt.normalize()
        daily = daily.dropna(subset=["BTC_close"] + ret_cols).reset_index(drop=True)
        if len(daily) < max(14, self.lookback_days):
            return
        self.daily = daily
        self.asset_rets = daily[ret_cols].to_numpy(np.float64)
        self.signal_phases = daily.get("btc_phase_signal_prevday", pd.Series(["neutral"] * len(daily))).astype(str).to_numpy()
        self.hist_last_day = pd.Timestamp(daily["day_ts"].iloc[-1]).tz_convert("UTC")
        if "btc_phase_v3_a3" in daily.columns:
            self.hist_prevday_phase = str(daily["btc_phase_v3_a3"].iloc[-1])
            self.hist_phase_by_day = {
                pd.Timestamp(row["day_ts"]).strftime("%Y-%m-%d"): str(row["btc_phase_v3_a3"])
                for _, row in daily.iterrows()
            }
        self.hist_btc_close_by_day = {
            pd.Timestamp(row["day_ts"]).strftime("%Y-%m-%d"): float(row["BTC_close"])
            for _, row in daily.iterrows()
        }

    def observe_market(self, t: Any, md_slice: Mapping[str, Mapping[str, Any]]) -> None:
        btc_row = md_slice.get(self.btc_symbol)
        if not btc_row:
            return
        try:
            px = float(btc_row.get("close") or 0.0)
        except Exception:
            return
        if px <= 0:
            return
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        self.live_btc_close_by_day[ts.normalize().strftime("%Y-%m-%d")] = px

    def _phase_from_closes(self, closes: Sequence[float]) -> str:
        if len(closes) < 5:
            return self.hist_prevday_phase
        logp = np.log(np.asarray(closes, dtype=np.float64))
        v = float(logp[-1] - logp[-4])
        prev_v = float(logp[-2] - logp[-5])
        a = v - prev_v
        if v > 0.0 and a > 0.0:
            return "rise_accelerating"
        if v > 0.0 and a <= 0.0:
            return "rise_decelerating"
        if v <= 0.0 and a > 0.0:
            return "fall_decelerating"
        return "fall_accelerating"

    def _phase_signal_for_day(self, day: pd.Timestamp) -> str:
        prev_day = (day - pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if prev_day in self.hist_phase_by_day:
            return self.hist_phase_by_day[prev_day]
        all_days = sorted(set(self.hist_btc_close_by_day) | set(self.live_btc_close_by_day))
        closes = []
        for key in all_days:
            if key > prev_day:
                continue
            closes.append(float(self.live_btc_close_by_day.get(key, self.hist_btc_close_by_day.get(key, 0.0))))
        return self._phase_from_closes([x for x in closes if x > 0])

    def weights(self, t: Any, md_slice: Mapping[str, Mapping[str, Any]] | None = None) -> Dict[str, float]:
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        day = ts.normalize()
        key = day.strftime("%Y-%m-%d")
        if md_slice is not None:
            self.observe_market(ts, md_slice)
        if key in self.day_cache:
            return dict(self.day_cache[key])
        if self.asset_rets is None or self.signal_phases is None:
            self.day_cache[key] = dict(self.static_weights)
            return dict(self.static_weights)

        phase = self._phase_signal_for_day(day)
        n = len(self.asset_rets)
        start = max(0, n - self.lookback_days)
        hist = self.asset_rets[start:n]
        phases = self.signal_phases[start:n]
        same = phases == phase
        if int(same.sum()) < self.min_same_phase_days:
            same = np.ones(len(hist), dtype=bool)
        sample = hist[same]
        mu = np.nanmean(sample, axis=0)
        vol = np.nanstd(sample, axis=0)
        base = 1.0 / np.maximum(vol, 1e-4) if self.base_mode == "risk_parity" else np.ones(len(self.symbols), dtype=np.float64)
        score = np.nan_to_num(mu / np.maximum(vol, 1e-4), nan=0.0)
        score = np.clip(score, -2.0, 2.0)
        raw = base * np.exp(self.beta * score)
        w = _capped_normalize(raw, self.min_weight, self.max_weight)
        if self.smooth > 0.0 and self.weight_cache:
            prev_key = sorted(self.weight_cache)[-1]
            prev = self.weight_cache[prev_key]
            w = _capped_normalize((1.0 - self.smooth) * w + self.smooth * prev, self.min_weight, self.max_weight)
        self.weight_cache[key] = w
        out = {sym: float(w[i]) for i, sym in enumerate(self.symbols)}
        self.day_cache[key] = out
        return dict(out)

    def phase(self, t: Any) -> str:
        ts = pd.Timestamp(t)
        if ts.tzinfo is None:
            ts = ts.tz_localize("UTC")
        else:
            ts = ts.tz_convert("UTC")
        return self._phase_signal_for_day(ts.normalize())


class LiquidMoneyBTCPhasePortfolioLiveAdapter:
    """Portfolio wrapper for liquid-money sleeves with BTC phase weights."""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg
        sp = cfg.get("strategy_params", {}) or {}
        self.params_by_symbol = sp.get("symbols", {}) or {}
        acfg = cfg.get("btc_phase_allocator", {}) or {}
        self.btc_symbol = str(acfg.get("btc_symbol") or "BTC/USDT:USDT")
        allowed = list((cfg.get("universe", {}) or {}).get("allow", []) or self.params_by_symbol.keys())
        self.symbols = [s for s in allowed if s != self.btc_symbol and s in self.params_by_symbol]
        self.initial_equity = float(cfg.get("initial_equity", cfg.get("start_cash", 300.0)))
        self.min_order_notional = float(sp.get("min_order_notional_usdt", cfg.get("notional", 2.5)))
        self.max_order_notional = float(sp.get("max_order_notional_usdt", 0.0))
        self.min_sleeve_equity = float(sp.get("min_sleeve_equity_usdt", self.min_order_notional))
        self.exchange_min_notional = float(sp.get("exchange_min_notional_usdt", self.min_order_notional))
        self.min_qty = 0.0
        self.allocator = BTCPhaseAllocator(cfg, self.symbols)
        self.adapters: Dict[str, LiquidMoneyStubbornLiveAdapter] = {}
        for sym in self.symbols:
            self.adapters[sym] = LiquidMoneyStubbornLiveAdapter(self._inner_cfg(sym, self.initial_equity / max(len(self.symbols), 1)))

    def _inner_cfg(self, sym: str, sleeve_equity: float) -> Dict[str, Any]:
        inner = copy.deepcopy(self.cfg)
        params = copy.deepcopy(self.params_by_symbol.get(sym, {}))
        inner["initial_equity"] = float(max(sleeve_equity, self.min_sleeve_equity))
        inner["notional"] = self.min_order_notional
        inner["position_notional"] = self.min_order_notional
        inner["universe"] = {"allow": [sym]}
        inner["strategy_params"] = {"default": {}, "symbols": {sym: params}}
        return inner

    def universe(self, t, md_slice) -> List[str]:
        self.allocator.observe_market(t, md_slice)
        available = list(md_slice.keys())
        allowed = set(self.symbols)
        return [s for s in available if s in allowed]

    def rank(self, t, md_slice, symbols: List[str]) -> List[str]:
        weights = self.allocator.weights(t, md_slice)
        return sorted(list(symbols), key=lambda s: weights.get(s, 0.0), reverse=True)

    def _account_equity(self, pf) -> float:
        if pf is None:
            return self.initial_equity
        equity = float(getattr(pf, "equity", self.initial_equity) or self.initial_equity)
        unreal = float(getattr(pf, "unrealized_pnl", 0.0) or 0.0)
        return max(1e-9, equity + unreal)

    def _params(self, sym: str) -> Dict[str, Any]:
        return dict(self.params_by_symbol.get(sym, {}) or {})

    def _leverage(self, p: Mapping[str, Any], side: str) -> float:
        side_u = str(side).upper()
        if side_u == "LONG":
            return float(p.get("leverage_long", p.get("leverage", 1.0)))
        return float(p.get("leverage_short", p.get("leverage", 1.0)))

    def _symbol_notional(self, pf, sym: str) -> float:
        total = 0.0
        for pos in list(getattr(pf, "positions", []) or []):
            if getattr(pos, "symbol", None) == sym:
                total += float(getattr(pos, "notional", 0.0) or 0.0)
        return total

    def _prepare_inner(self, sym: str, t, pf) -> tuple[LiquidMoneyStubbornLiveAdapter, float, float]:
        weights = self.allocator.weights(t)
        weight = float(weights.get(sym, 0.0))
        sleeve = max(self.min_sleeve_equity, self._account_equity(pf) * weight)
        inner = self.adapters[sym]
        inner.initial_equity = sleeve
        return inner, weight, sleeve

    def entry_signal(self, is_opening: bool, sym: str, row: Dict[str, Any], ctx=None):
        if not is_opening or sym not in self.adapters:
            return None
        pf = (ctx or {}).get("portfolio") if isinstance(ctx, dict) else None
        t = row.get("datetime_utc") or row.get("timestamp") or pd.Timestamp.now(tz="UTC")
        inner, weight, sleeve = self._prepare_inner(sym, t, pf)
        current_notional = self._symbol_notional(pf, sym)
        sig = inner.entry_signal(is_opening, sym, row, ctx=ctx)
        if sig is None:
            return None

        side = str(getattr(sig, "side", "LONG")).upper()
        p = self._params(sym)
        lev = self._leverage(p, side)
        cap_notional = sleeve * lev
        remaining = max(0.0, cap_notional - current_notional)
        if remaining + 1e-9 < self.min_order_notional:
            return None

        st = inner.states.get(sym)
        exposure_after = float(getattr(st, "exposure_pct", 0.0) if st is not None else 0.0)
        target_after = max(0.0, sleeve * lev * exposure_after / 100.0)
        desired = max(self.min_order_notional, target_after - current_notional)
        desired = min(desired, remaining)
        if self.max_order_notional > 0:
            desired = min(desired, self.max_order_notional)
        if not math.isfinite(desired) or desired + 1e-9 < self.min_order_notional:
            return None

        phase = self.allocator.phase(t)
        setattr(sig, "notional", float(desired))
        setattr(sig, "target_notional", float(desired))
        tags = list(getattr(sig, "tags", []) or [])
        tags.extend(["btc_phase", f"phase={phase}", f"w={weight:.4f}", f"cap={cap_notional:.2f}"])
        setattr(sig, "tags", tags)
        reason = str(getattr(sig, "reason", "") or "")
        setattr(sig, "reason", f"{reason} | btc_phase={phase} w={weight:.4f} sleeve={sleeve:.2f} notional={desired:.2f}")
        return sig

    def manage_position(self, sym: str, row: Dict[str, Any], pos, ctx=None):
        if sym not in self.adapters:
            return None
        pf = (ctx or {}).get("portfolio") if isinstance(ctx, dict) else None
        t = row.get("datetime_utc") or row.get("timestamp") or pd.Timestamp.now(tz="UTC")
        inner, _weight, _sleeve = self._prepare_inner(sym, t, pf)
        return inner.manage_position(sym, row, pos, ctx=ctx)
