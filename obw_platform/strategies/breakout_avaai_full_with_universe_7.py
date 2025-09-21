
# strategies/breakout_avaai_full_with_universe_7.py
# Refactored strategy: ALL entry/exit & TP/SL decisions live here.
# Backtester must only:
#   - apply allow/deny universe for OPENING,
#   - call universe()/rank() to get candidates,
#   - call entry_signal() to open (TP/SL must be provided here),
#   - call manage_position() to close,
#   - optionally print "heat" (purely reporting; not used for decisions).
from __future__ import annotations

import math

from dataclasses import dataclass
from typing import Optional, Literal, Mapping, Any, List, Dict, Tuple

Side = Literal["LONG","SHORT"]
ExitAction = Literal["HOLD","TP","SL","EXIT","TP_PARTIAL"]

@dataclass
class Sig:
    side: Side
    take_profit: float
    stop_price: float
    confidence: float = 0.0
    size: Optional[float] = None
    reason: Optional[str] = None
    tags: Optional[Dict[str, Any]] = None
    heat: Optional[float] = None

    # --- aliases for compatibility ---
    @property
    def tp(self): return self.take_profit
    @property
    def tp_price(self): return self.take_profit
    @property
    def sl(self): return self.stop_price
    @property
    def sl_price(self): return self.stop_price

@dataclass
class ExitSig:
    action: ExitAction
    exit_price: Optional[float] = None
    reason: Optional[str] = None
    qty_frac: Optional[float] = None

def _f(x, default=0.0) -> float | None:
    try:
        return float(x)
    except Exception:
        return None if default is None else float(default)

class BreakoutAVAAIFull:
    """
    A deterministic, self-contained strategy that encodes BOTH the candidate selection
    and the trading rules. This mirrors the profitable behaviour discovered in your runs:
      - Universe/Rank: score = dp6h + dp12h (SHORT inverts sign).  No momentum threshold by default.
      - Entry: side from params; BOTH follows sign(mom_sum).
      - TP/SL: ALWAYS from ATR multiples at entry.
      - Exit: CLOSE-based TP/SL checks (match old backtester).
    Non-zero defaults that used to live in the backtester (e.g., top-n=8) are moved here.
    Top-level YAML keys override strategy_params to preserve your prior “lucky” overrides.
    """
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.cfg = dict(cfg or {})
        sp = (self.cfg.get("strategy_params") or {})

        # --- read knobs (top-level overrides SP to preserve historical behaviour) ---
        def _read(key, default):
            return self.cfg.get(key, sp.get(key, default))

        self.side: str = str(_read("side", "BOTH")).upper()
        self.top_n: int = int(_read("top_n", _read("top-n", 8)))  # accept both spellings

        # entry filters: disabled by default
        self.entry_min_atr_ratio: float = float(_read("min_atr_ratio", 0.0))
        self.entry_min_momentum_sum: float = float(_read("min_momentum_sum", 0.0))

        # volatility/SL guards (all optional; defaults keep legacy behaviour)
        self.max_atr_ratio_entry: float = float(_read("max_atr_ratio_entry", 0.0))
        self.max_sl_bps: float = float(_read("max_sl_bps", 0.0))
        self.vol_spike_k_median: float = float(_read("vol_spike_k_median", 0.0))
        self.vol_median_window: int = int(_read("vol_median_window", 0))
        self.impulse_bps: float = float(_read("impulse_bps", 0.0))
        self.wick_ratio_max: float = float(_read("wick_ratio_max", 1.0))
        self.position_downscale_on_high_vol: bool = bool(_read("position_downscale_on_high_vol", False))
        self.target_atr_ratio: float = float(_read("target_atr_ratio", 0.0))

        # exit-specific filters (can be overridden via exit_filters section)
        exit_f = (self.cfg.get("exit_filters") or sp.get("exit_filters") or {})
        self.exit_min_atr_ratio: float = float(exit_f.get("min_atr_ratio", 0.03))
        self.exit_min_momentum_sum: float = float(exit_f.get("min_momentum_sum", 0.05))

        # keep legacy names for compatibility
        self.min_atr_ratio = self.entry_min_atr_ratio
        self.min_momentum_sum = self.entry_min_momentum_sum

        self.tp_mult: float = float(_read("tp_atr_mult", 3.8))
        self.sl_mult: float = float(_read("sl_atr_mult", 1.04))

        # trading costs / buffers
        self.fee_rate: float = float(_read("fee_rate", 0.001))
        self.slip_per_side: float = float(_read("slippage_per_side", 0.0016))

        # HEAT-based exit
        self.exit_on_heat: bool = bool(_read("exit_on_heat", True))
        self.heat_exit_threshold: float = float(_read("heat_exit_threshold", 0.40))
        self.heat_exit_min_rr: float = float(_read("heat_exit_min_rr", 1.05))

        # Partial take-profit
        self.partial_tp_enable: bool = bool(_read("partial_tp_enable", True))
        self.partial_tp_frac: float = float(_read("partial_tp_frac", 0.50))
        self.partial_trigger_frac_of_tp: float = float(_read("partial_trigger_frac_of_tp", 0.50))
        self.exchange_min_notional: float = float(_read("exchange_min_notional", 2.2))
        self.min_qty: float = float(_read("min_qty", 0.0))

        # liquidity floors (kept but set lenient defaults; can be overridden in YAML)
        self.min_qv_24h: float = float(_read("min_qv_24h", 0.0))
        self.min_qv_1h: float  = float(_read("min_qv_1h", 0.0))

        # optional limits to control entry bursts
        self.max_new_positions_per_bar: int = int(_read("max_new_positions_per_bar", 0))
        self.first_bar_max_positions: int = int(
            _read("first_bar_max_positions", self.max_new_positions_per_bar)
        )
        # internal counters
        self._first_bar_ts: Optional[Any] = None
        self._last_bar_ts: Optional[Any] = None
        self._opens_this_bar: int = 0
        self._last_entry_debug: Dict[str, Any] = {}
    # ---------- helpers ----------
    def _mom_sum(self, row: Mapping[str, Any]) -> float:
        return _f(row.get("dp6h", 0.0)) + _f(row.get("dp12h", 0.0))

    def _liq_ok(self, row: Mapping[str, Any]) -> bool:
        qv24 = _f(row.get("qv_24h", 0.0))
        qv1  = _f(row.get("quote_volume", 0.0))
        if qv1 <= 0.0:
            # allow derived 1h volume if provided
            qv1 = _f(row.get("volume", 0.0)) * _f(row.get("close", 0.0))
        return (qv24 >= self.min_qv_24h) and (qv1 >= self.min_qv_1h)

    def _round_trip_buffer_rr(self) -> float:
        """Approximate round-trip cost in R units (relative to entry price)."""
        return 2 * self.fee_rate + 2 * self.slip_per_side

    def _unrealized_rr(self, side: str, entry: float, px: float) -> float:
        if entry <= 0:
            return 0.0
        pnl = (px - entry) / entry if side == "LONG" else (entry - px) / entry
        return float(pnl)

    def _resolve_atr_median(self, row: Mapping[str, Any], ctx: Optional[Mapping[str, Any]]) -> Optional[float]:
        """Best-effort lookup of rolling median atr_ratio for volatility guards."""
        if self.vol_median_window <= 0:
            return None

        cand_keys = [
            f"atr_ratio_median_{self.vol_median_window}",
            f"atr_ratio_med_{self.vol_median_window}",
            f"atr_ratio_median{self.vol_median_window}",
            f"atr_ratio_{self.vol_median_window}_median",
            "atr_ratio_median",
            "atr_ratio_med",
        ]
        for key in cand_keys:
            try:
                val = row.get(key)
            except Exception:
                val = None
            med = _f(val, None)
            if med is not None and med > 0:
                return med

        if ctx:
            history = None
            for hist_key in ("atr_history", "history", "df", "md"):
                if history is None:
                    history = ctx.get(hist_key) if isinstance(ctx, Mapping) else None
            if history is not None:
                try:
                    series = None
                    if hasattr(history, "get"):
                        try:
                            series = history.get("atr_ratio")
                        except Exception:
                            series = None
                    if series is None and hasattr(history, "__getitem__"):
                        try:
                            series = history["atr_ratio"]
                        except Exception:
                            series = None
                    if series is None:
                        return None
                    if hasattr(series, "tail"):
                        series = series.tail(self.vol_median_window)
                    values = []
                    if hasattr(series, "tolist"):
                        values = series.tolist()
                    else:
                        try:
                            values = list(series)
                        except Exception:
                            values = []
                    if not values:
                        return None
                    tail = values[-self.vol_median_window :]
                    clean: List[float] = []
                    for v in tail:
                        fv = _f(v, None)
                        if fv is None or math.isnan(fv):
                            continue
                        clean.append(fv)
                    if not clean:
                        return None
                    clean.sort()
                    n = len(clean)
                    mid = n // 2
                    if n % 2:
                        return clean[mid]
                    return 0.5 * (clean[mid - 1] + clean[mid])
                except Exception:
                    return None
        return None

    def _estimate_base_qty(self, ctx: Optional[Mapping[str, Any]], price: Optional[float]) -> Optional[float]:
        if price is None or price <= 0:
            return None
        if ctx is None:
            return None

        try_keys = ("qty", "size", "base_qty", "position_qty")
        for key in try_keys:
            if isinstance(ctx, Mapping) and key in ctx:
                val = _f(ctx.get(key), None)
                if val is not None and val > 0:
                    return val

        if isinstance(ctx, Mapping):
            notional = None
            for key in ("position_notional", "notional"):
                val = _f(ctx.get(key), None)
                if val is not None and val > 0:
                    notional = val
                    break
            if notional is not None and notional > 0:
                return float(notional) / price

            pf = ctx.get("portfolio")
            if pf is not None:
                try:
                    for attr in ("position_notional", "default_notional", "notional"):
                        val = getattr(pf, attr, None)
                        if val is not None and isinstance(val, (int, float)) and val > 0:
                            return float(val) / price
                    cfg = getattr(pf, "cfg", None)
                    if isinstance(cfg, Mapping):
                        val = _f(cfg.get("position_notional"), None)
                        if val is not None and val > 0:
                            return float(val) / price
                except Exception:
                    pass

        return None

    @staticmethod
    def _pct_gap(actual: float, thresh: float) -> float:
        """Percentage gap for checks of the form ``actual >= thresh``."""
        try:
            a = float(actual); t = float(thresh)
        except Exception:
            return 1.0
        if t <= 0:
            return 0.0
        if a >= t:
            return 0.0
        return max(0.0, min(1.0, (t - a) / t))

    @staticmethod
    def _pct_gap_rev(actual: float, thresh: float) -> float:
        """Reverse variant used for directional momentum thresholds."""
        try:
            a = float(actual); t = float(thresh)
        except Exception:
            return 1.0
        if t <= 0:
            return 0.0
        if a >= t:
            return 0.0
        return max(0.0, min(1.0, (t - a) / t))

    # ---------- universe & ranking ----------
    def universe(self, t: Any, md_map: Mapping[str, Mapping[str, Any]]) -> List[str]:
        """Filter symbols by minimal ATR and liquidity. Momentum threshold is optional and
        defaults to 0.0 (disabled) to match the profitable setting discovered earlier."""
        out: List[str] = []
        for sym, row in md_map.items():
            atr = _f(row.get("atr_ratio", 0.0))
            if self.entry_min_atr_ratio > 0 and atr < self.entry_min_atr_ratio:
                continue
            if not self._liq_ok(row):
                continue
            # Optional momentum threshold (mm<=0 disables)
            m = self._mom_sum(row)
            mm = self.entry_min_momentum_sum
            if mm > 0:
                if self.side == "LONG" and m < +mm:
                    continue
                if self.side == "SHORT" and m > -mm:
                    continue
                if self.side == "BOTH" and abs(m) < mm:
                    continue
            out.append(sym)
        return out

    def rank(self, t: Any, md_map: Mapping[str, Mapping[str, Any]], universe_syms: List[str]) -> List[str]:
        """Sort by directional momentum score; return already cut to top_n (keeps stability)."""
        invert = (self.side == "SHORT")
        scored: List[Tuple[float,int,str]] = []
        for idx, sym in enumerate(universe_syms):
            m = self._mom_sum(md_map.get(sym, {}))
            score = (-m) if invert else (m)
            scored.append((score, idx, sym))
        scored.sort(key=lambda x: x[0], reverse=True)  # stable via index
        # CUT to top_n here (moved from backtester); top_n<=0 disables the limit
        take = int(self.top_n)
        if take <= 0:
            return [sym for _, __, sym in scored]
        return [sym for _, __, sym in scored[:take]]

    # ---------- entry / exit ----------
    def entry_signal(self, t: Any, symbol: str, row: Mapping[str, Any], ctx: Optional[Mapping[str, Any]] = None) -> Optional[Sig]:
        """Return full Sig with TP/SL and enforce per-bar entry caps."""
        # Track bar transition
        if self._first_bar_ts is None:
            self._first_bar_ts = t
        if t != self._last_bar_ts:
            self._last_bar_ts = t
            self._opens_this_bar = 0
        # Determine limit for this bar
        limit = self.max_new_positions_per_bar
        if t == self._first_bar_ts and self.first_bar_max_positions > 0:
            limit = self.first_bar_max_positions
        if limit > 0 and self._opens_this_bar >= limit:
            return None

        m = self._mom_sum(row)
        if self.side == "LONG":
            side: Side = "LONG"
        elif self.side == "SHORT":
            side = "SHORT"
        else:  # BOTH follows momentum sign
            side = "LONG" if m >= 0.0 else "SHORT"

        close = _f(row.get("close", None), None)
        atrr = _f(row.get("atr_ratio", None), None)
        if close is None or atrr is None or close <= 0 or atrr <= 0:
            self._last_entry_debug = {
                "symbol": symbol,
                "time": t,
                "filters_hit": ["missing_data"],
            }
            return None

        atr_abs = max(1e-12, close * atrr)
        if side == "LONG":
            tp = close + self.tp_mult * atr_abs
            sl = close - self.sl_mult * atr_abs
            sl_dist_pct = max(0.0, (close - sl) / max(close, 1e-12))
        else:
            tp = close - self.tp_mult * atr_abs
            sl = close + self.sl_mult * atr_abs
            sl_dist_pct = max(0.0, (sl - close) / max(close, 1e-12))

        open_px = _f(row.get("open", None), None)
        high = _f(row.get("high", None), None)
        low = _f(row.get("low", None), None)
        true_range = _f(row.get("true_range", None), None)
        if true_range is None or true_range <= 0:
            if high is not None and low is not None:
                true_range = max(0.0, high - low)
            else:
                true_range = atr_abs
        true_range = max(true_range, 1e-12)

        impulse_ratio = 0.0
        if open_px is not None and open_px > 0:
            impulse_ratio = abs(close - open_px) / max(close, 1e-12)

        wick_ratio = 0.0
        if high is not None and low is not None and open_px is not None:
            body_high = max(open_px, close)
            body_low = min(open_px, close)
            upper_wick = max(0.0, high - body_high)
            lower_wick = max(0.0, body_low - low)
            wick_ratio = max(upper_wick, lower_wick) / true_range

        atr_median = self._resolve_atr_median(row, ctx)

        filters_hit: List[str] = []
        if self.max_atr_ratio_entry > 0 and atrr > self.max_atr_ratio_entry:
            filters_hit.append("atr_cap")

        if (
            self.vol_spike_k_median > 0
            and atr_median is not None
            and atr_median > 0
            and atrr > self.vol_spike_k_median * atr_median
        ):
            filters_hit.append("vol_spike")

        impulse_limit = self.impulse_bps / 10000.0 if self.impulse_bps > 0 else 0.0
        if impulse_limit > 0 and impulse_ratio > impulse_limit:
            filters_hit.append("impulse")

        if self.wick_ratio_max >= 0 and wick_ratio > self.wick_ratio_max:
            filters_hit.append("wick")

        max_sl_frac = self.max_sl_bps / 10000.0 if self.max_sl_bps > 0 else 0.0
        if max_sl_frac > 0 and sl_dist_pct > max_sl_frac:
            filters_hit.append("max_sl_bps")

        debug_info: Dict[str, Any] = {
            "symbol": symbol,
            "time": t,
            "entry_atr_ratio": float(atrr),
            "atr_median": (float(atr_median) if atr_median is not None else None),
            "sl_dist_pct": float(sl_dist_pct),
            "impulse_ratio": float(impulse_ratio),
            "wick_ratio": float(wick_ratio),
            "filters_hit": list(filters_hit),
        }

        if filters_hit:
            reason = "max_sl_bps" if "max_sl_bps" in filters_hit else "vol_spike"
            debug_info["reason"] = reason
            self._last_entry_debug = debug_info
            return None

        qty = self._estimate_base_qty(ctx, close)
        downscale_factor = 1.0
        if (
            self.position_downscale_on_high_vol
            and self.target_atr_ratio > 0
            and atrr > self.target_atr_ratio
        ):
            denom = max(atrr, 1e-9)
            downscale_factor = min(1.0, self.target_atr_ratio / denom)
            if qty is not None and qty > 0:
                qty *= downscale_factor

        downscale_applied = downscale_factor if downscale_factor < 1.0 else None

        if qty is not None and qty > 0:
            notional = qty * close
            if (self.min_qty and qty < self.min_qty) or (
                self.exchange_min_notional and notional < self.exchange_min_notional
            ):
                debug_info["filters_hit"] = debug_info.get("filters_hit", []) + ["min_qty"]
                debug_info["reason"] = "min_qty"
                debug_info["qty"] = float(qty)
                debug_info["notional"] = float(notional)
                if downscale_applied is not None:
                    debug_info["downscale"] = float(downscale_applied)
                self._last_entry_debug = debug_info
                return None

        self._opens_this_bar += 1
        entry_heat = self.heat(t, symbol, row)

        tags: Dict[str, Any] = {
            "entry_atr_ratio": float(atrr),
            "sl_dist_pct": float(sl_dist_pct),
            "filters_hit": [],
            "atr_median": debug_info.get("atr_median"),
            "impulse_ratio": float(impulse_ratio),
            "wick_ratio": float(wick_ratio),
        }
        if downscale_applied is not None:
            tags["position_scale"] = float(downscale_applied)

        sig = Sig(
            side=side,
            take_profit=float(tp),
            stop_price=float(sl),
            reason="rule/atr-multipliers",
            heat=float(entry_heat),
        )
        final_debug = dict(debug_info)
        final_debug["filters_hit"] = []
        if downscale_applied is not None:
            final_debug["downscale"] = float(downscale_applied)
        if qty is not None and qty > 0:
            sig.size = float(qty)
            final_debug["qty"] = float(qty)
            final_debug["notional"] = float(qty * close)
        sig.tags = tags
        self._last_entry_debug = final_debug
        return sig

    def manage_position(self, symbol, row, pos, ctx=None):
        close = _f(row.get("close", 0.0))
        side  = str(getattr(pos, "side", "LONG")).upper()
        tp    = _f(getattr(pos, "tp", getattr(pos, "take_profit", getattr(pos, "tp_price", None))), None)
        sl    = _f(getattr(pos, "sl", getattr(pos, "stop_price", getattr(pos, "sl_price", None))), None)
        entry = _f(getattr(pos, "entry", getattr(pos, "entry_price", None)), None)
        qty   = _f(getattr(pos, "qty", getattr(pos, "size", None)), None)
        if (qty is None or qty <= 0) and entry and entry > 0:
            try:
                notional = _f(getattr(pos, "notional", None), None)
                if notional and notional > 0:
                    qty = notional / entry
            except Exception:
                qty = None

        # 1) Стандартні TP/SL по close (як було)
        if side == "LONG":
            if sl and close <= sl: return ExitSig("SL", exit_price=sl, reason="SL")
            if tp and close >= tp: return ExitSig("TP", exit_price=tp, reason="TP")
        else:
            if sl and close >= sl: return ExitSig("SL", exit_price=sl, reason="SL")
            if tp and close <= tp: return ExitSig("TP", exit_price=tp, reason="TP")

        # Якщо немає потрібних даних — тримаємо
        if entry is None or entry <= 0 or qty is None or qty <= 0:
            return ExitSig("HOLD")

        # 2) Частковий TP (50%) — коли пройшли X% шляху до TP
        if self.partial_tp_enable and tp:
            path = (tp - entry) if side == "LONG" else (entry - tp)
            prog = (close - entry) if side == "LONG" else (entry - close)
            if path > 0 and prog >= self.partial_trigger_frac_of_tp * path:
                part_qty = qty * self.partial_tp_frac
                notional = part_qty * close
                if (self.min_qty and part_qty < self.min_qty) or (notional < self.exchange_min_notional):
                    pass
                else:
                    return ExitSig("TP_PARTIAL", exit_price=close, reason="TP50", qty_frac=self.partial_tp_frac)

        # 3) Heat-exit / Reverse-momentum exit за умови, що PnL >= буферу
        rr = self._unrealized_rr(side, entry, close)
        need = self._round_trip_buffer_rr() * self.heat_exit_min_rr

        if rr >= need:
            h_now = self.heat(None, symbol, row)
            if self.exit_on_heat and h_now < self.heat_exit_threshold:
                return ExitSig("EXIT", exit_price=close, reason=f"heat<{self.heat_exit_threshold:.2f}")

            m = self._mom_sum(row)
            if (side == "LONG" and m < 0) or (side == "SHORT" and m > 0):
                return ExitSig("EXIT", exit_price=close, reason="mom_reverse")

        return ExitSig("HOLD")

    # ---------- optional: heat reporting only (no decisions here) ----------
    def entry_distance(self, t: Any, sym: str, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Compute gaps against the strategy's filters for a single symbol."""
        m = self._mom_sum(row)
        atrr = _f(row.get("atr_ratio", 0.0))
        qv24 = _f(row.get("qv_24h", 0.0))
        qv1  = _f(row.get("quote_volume", 0.0))
        if qv1 <= 0.0:
            qv1 = _f(row.get("volume", 0.0)) * _f(row.get("close", 0.0))

        min_atr = float(self.exit_min_atr_ratio)
        min_qv24 = float(self.min_qv_24h)
        min_qv1h = float(self.min_qv_1h)
        min_mom = float(self.exit_min_momentum_sum)

        if self.side == "LONG":
            gap_mom = self._pct_gap_rev(m, +min_mom)
        elif self.side == "SHORT":
            gap_mom = self._pct_gap_rev(-m, +min_mom)
        else:
            gap_mom = self._pct_gap_rev(abs(m), +min_mom)

        gap_atr = self._pct_gap(atrr, min_atr)
        gap_qv24 = self._pct_gap(qv24, min_qv24)
        gap_qv1 = self._pct_gap(qv1, min_qv1h)

        combined_gap = max(gap_atr, gap_qv24, gap_qv1, gap_mom)

        gaps_map = {
            "atr": gap_atr,
            "qv24": gap_qv24,
            "qv1h": gap_qv1,
            "momentum": gap_mom,
        }
        worst_key = max(gaps_map, key=lambda k: gaps_map[k])
        reason = ""
        if worst_key == "atr":
            reason = f"atr low: {atrr:.4f} < {min_atr:.4f}"
        elif worst_key == "qv24":
            reason = f"qv24 low: {qv24:.0f} < {min_qv24:.0f}"
        elif worst_key == "qv1h":
            reason = f"qv1h low: {qv1:.0f} < {min_qv1h:.0f}"
        elif worst_key == "momentum":
            reason = f"momentum low: {m:.4f} < {min_mom:.4f}"

        return {
            "symbol": sym,
            "combined_gap": float(combined_gap),
            "gaps": {
                "atr": float(gap_atr),
                "qv24": float(gap_qv24),
                "qv1h": float(gap_qv1),
                "momentum": float(gap_mom),
            },
            "actuals": {
                "atr_ratio": float(atrr),
                "qv_24h": float(qv24),
                "qv_1h": float(qv1),
                "mom_sum": float(m),
            },
            "thresholds": {
                "min_atr_ratio": float(min_atr),
                "min_qv_24h": float(min_qv24),
                "min_qv_1h": float(min_qv1h),
                "min_momentum_sum": float(min_mom),
            },
            "reason": reason,
        }

    def best_entry_distance(self, t: Any, md_slice: dict, symbols=None) -> Optional[Dict[str, Any]]:
        """Evaluate distances for many symbols and return the nearest-to-entry one."""
        if not md_slice:
            return None
        if symbols is None:
            symbols = list(md_slice.keys())

        best = None
        best_gap = 1.0
        for sym in symbols:
            row = md_slice.get(sym)
            if not row:
                continue
            dist = self.entry_distance(t, sym, row)
            gap = float(dist.get("combined_gap", 1.0))
            if gap < best_gap:
                best_gap = gap
                best = dist
        return best

    def heat(self, t: Any, symbol: str, row: Mapping[str, Any]) -> float:
        """Return heat in [0..1] computed as ``1 - max(gaps)``."""
        try:
            dist = self.entry_distance(t, symbol, row)
            gaps = (dist or {}).get("gaps") or {}
            if not gaps:
                return 0.0
            worst = max(float(v) for v in gaps.values() if v is not None)
            return max(0.0, min(1.0, 1.0 - worst))
        except Exception:
            return 0.0
