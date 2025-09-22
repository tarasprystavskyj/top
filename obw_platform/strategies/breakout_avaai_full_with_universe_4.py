
# strategies/breakout_avaai_full_with_universe_4.py
# Refactored strategy: ALL entry/exit & TP/SL decisions live here.
# Backtester must only:
#   - apply allow/deny universe for OPENING,
#   - call universe()/rank() to get candidates,
#   - call entry_signal() to open (TP/SL must be provided here),
#   - call manage_position() to close,
#   - optionally print "heat" (purely reporting; not used for decisions).
from __future__ import annotations

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
        self.min_atr_ratio: float = float(_read("min_atr_ratio", 0.02))
        # Important: default to 0.0 to reproduce the wide-entry behaviour unless overridden
        self.min_momentum_sum: float = float(_read("min_momentum_sum", 0.0))
        self.tp_mult: float = float(_read("tp_atr_mult", 3.8))
        self.sl_mult: float = float(_read("sl_atr_mult", 1.04))

        # trading costs / buffers
        self.fee_rate: float = float(_read("fee_rate", 0.001))
        self.slip_per_side: float = float(_read("slippage_per_side", 0.0016))

        # HEAT-based exit
        self.exit_on_heat: bool = bool(_read("exit_on_heat", True))
        self.heat_exit_threshold: float = float(_read("heat_exit_threshold", 0.40))
        self.heat_exit_min_rr: float = float(_read("heat_exit_min_rr", 1.05))
        self.heat_exit_require_adverse_momentum: bool = bool(
            _read("heat_exit_require_adverse_momentum", True)
        )
        self.heat_exit_use_momentum_slope: bool = bool(
            _read("heat_exit_use_momentum_slope", True)
        )
        self.heat_exit_slope_window: int = int(_read("heat_exit_slope_window", 1))
        self.exit_grace_bars: int = int(_read("exit_grace_bars", 2))
        self.ignore_htf_on_local_reversal: bool = bool(
            _read("ignore_htf_on_local_reversal", True)
        )
        self.local_reversal_m_abs: float = float(_read("local_reversal_m_abs", 0.002))
        self.heat_drop_k: float = float(_read("heat_drop_k", 0.90))

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
            if self.min_atr_ratio > 0 and atr < self.min_atr_ratio:
                continue
            if not self._liq_ok(row):
                continue
            # Optional momentum threshold (mm<=0 disables)
            m = self._mom_sum(row)
            mm = self.min_momentum_sum
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
            # if self.side == "BOTH":
                # score = abs(m)
            # else:
                # score = (-m) if invert else (m)
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
        atrr  = _f(row.get("atr_ratio", None), None)
        if close is None or atrr is None or close <= 0 or atrr <= 0:
            return None

        atr_abs = max(1e-12, close * atrr)
        if side == "LONG":
            tp = close + self.tp_mult * atr_abs
            sl = close - self.sl_mult * atr_abs
        else:
            tp = close - self.tp_mult * atr_abs
            sl = close + self.sl_mult * atr_abs

        self._opens_this_bar += 1
        entry_heat = self.heat(t, symbol, row)
        sig = Sig(
            side=side,
            take_profit=float(tp),
            stop_price=float(sl),
            reason="rule/atr-multipliers",
            heat=float(entry_heat),
        )
        try:
            tags = {"last_mom": float(m)}
            try:
                tags["mom_hist"] = [float(m)]
            except Exception:
                pass
            try:
                tags["last_heat"] = float(entry_heat)
            except Exception:
                pass
            sig.tags = tags
        except Exception:
            pass
        try:
            setattr(sig, "bars_held", 0)
        except Exception:
            pass
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

        m_now = self._mom_sum(row)
        try:
            bars_held = int(getattr(pos, "bars_held", 0))
        except Exception:
            bars_held = 0

        try:
            tags_raw = getattr(pos, "tags", {}) or {}
            tags_map = dict(tags_raw)
        except Exception:
            tags_map = {}

        last_mom = _f(tags_map.get("last_mom"), None)
        mom_hist_list: List[float] = []
        try:
            hist_src = tags_map.get("mom_hist")
            if isinstance(hist_src, list):
                for val in hist_src:
                    fv = _f(val, None)
                    if fv is not None:
                        mom_hist_list.append(float(fv))
        except Exception:
            mom_hist_list = []

        last_heat = _f(tags_map.get("last_heat"), None)

        def _normalize_bias(val: Any) -> Optional[str]:
            if val is None:
                return None
            if isinstance(val, (int, float)):
                if float(val) > 0:
                    return "LONG"
                if float(val) < 0:
                    return "SHORT"
                return None
            try:
                text = str(val).strip().upper()
            except Exception:
                return None
            if not text:
                return None
            mapping = {
                "LONG": "LONG",
                "L": "LONG",
                "BUY": "LONG",
                "UP": "LONG",
                "BULL": "LONG",
                "SHORT": "SHORT",
                "S": "SHORT",
                "SELL": "SHORT",
                "DOWN": "SHORT",
                "BEAR": "SHORT",
            }
            return mapping.get(text)

        def _extract_htf_bias(*sources: Any) -> Optional[str]:
            keys = (
                "htf_bias",
                "htf_bias_side",
                "htf_trend",
                "htf_trend_side",
                "htf_side",
                "htf_direction",
                "htf_dir",
            )
            for src in sources:
                if src is None:
                    continue
                if isinstance(src, Mapping):
                    for key in keys:
                        if key in src:
                            bias_val = _normalize_bias(src.get(key))
                            if bias_val:
                                return bias_val
                else:
                    bias_val = _normalize_bias(src)
                    if bias_val:
                        return bias_val
            return None

        htf_bias_side = _extract_htf_bias(
            tags_map,
            getattr(pos, "meta", None),
            getattr(pos, "htf_bias", None),
            ctx or {},
            row,
        )
        htf_supports_side = htf_bias_side == side

        heat_for_update: Optional[float] = None

        def _finalize(sig: ExitSig, heat_val: Optional[float] = None) -> ExitSig:
            try:
                updated_tags = dict(tags_map)
                updated_tags["last_mom"] = float(m_now)
                hist_out = list(mom_hist_list)
                hist_out.append(float(m_now))
                keep = max(1, int(self.heat_exit_slope_window))
                hist_out = hist_out[-keep:]
                updated_tags["mom_hist"] = hist_out
                if heat_val is not None:
                    updated_tags["last_heat"] = float(heat_val)
                if hasattr(pos, "tags"):
                    pos.tags = updated_tags
                else:
                    setattr(pos, "tags", updated_tags)
                if hasattr(pos, "bars_held"):
                    pos.bars_held = bars_held + 1
                else:
                    setattr(pos, "bars_held", bars_held + 1)
            except Exception:
                pass
            return sig

        # 1) Стандартні TP/SL по close (як було)
        if side == "LONG":
            if sl and close <= sl: return ExitSig("SL", exit_price=sl, reason="SL")
            if tp and close >= tp: return ExitSig("TP", exit_price=tp, reason="TP")
        else:
            if sl and close >= sl: return ExitSig("SL", exit_price=sl, reason="SL")
            if tp and close <= tp: return ExitSig("TP", exit_price=tp, reason="TP")

        if entry is None or entry <= 0:
            return _finalize(ExitSig("HOLD"), heat_for_update)

        trigger_frac = max(0.0, float(self.partial_trigger_frac_of_tp))
        path = prog = None
        trigger_reached = False
        if tp is not None:
            if side == "LONG":
                path = tp - entry
                prog = close - entry
            else:
                path = entry - tp
                prog = entry - close
            if path is not None and path > 0 and trigger_frac > 0:
                trigger_reached = prog >= trigger_frac * path

        if trigger_reached:
            be_price = float(entry)
            tol = max(abs(be_price) * 1e-6, 1e-8)

            def _set_stop(px: float) -> None:
                try:
                    if hasattr(pos, "stop_price"):
                        pos.stop_price = float(px)
                except Exception:
                    pass
                try:
                    if hasattr(pos, "sl"):
                        pos.sl = float(px)
                except Exception:
                    pass

            if side == "LONG":
                if sl is None or sl < be_price - tol:
                    _set_stop(be_price)
                    sl = be_price
            else:
                if sl is None or sl > be_price + tol:
                    _set_stop(be_price)
                    sl = be_price

        if qty is None or qty <= 0:
            return _finalize(ExitSig("HOLD"), heat_for_update)

        if self.partial_tp_enable and tp and path and path > 0 and trigger_reached:
            part_qty = qty * self.partial_tp_frac
            notional = part_qty * close
            if (self.min_qty and part_qty < self.min_qty) or (notional < self.exchange_min_notional):
                pass
            else:
                return _finalize(
                    ExitSig("TP_PARTIAL", exit_price=close, reason="TP50", qty_frac=self.partial_tp_frac),
                    heat_for_update,
                )

        # 3) Heat-exit / Reverse-momentum exit за умови, що PnL >= буферу
        rr = self._unrealized_rr(side, entry, close)
        need = self._round_trip_buffer_rr() * self.heat_exit_min_rr

        if rr >= need:
            h_now = self.heat(None, symbol, row)
            heat_for_update = h_now
            allow_heat = self.exit_on_heat and (h_now < self.heat_exit_threshold)

            if self.exit_grace_bars > 0 and bars_held < self.exit_grace_bars:
                allow_heat = False

            adverse = (side == "LONG" and m_now < 0) or (side == "SHORT" and m_now > 0)
            if allow_heat and self.heat_exit_require_adverse_momentum and not adverse:
                allow_heat = False

            if allow_heat and self.heat_exit_use_momentum_slope:
                prev_for_slope: Optional[float] = None
                window = max(1, int(self.heat_exit_slope_window))
                if mom_hist_list and len(mom_hist_list) >= window:
                    prev_for_slope = mom_hist_list[-window]
                elif last_mom is not None:
                    prev_for_slope = float(last_mom)
                if prev_for_slope is not None:
                    improving = (m_now - prev_for_slope) > 0 if side == "LONG" else (m_now - prev_for_slope) < 0
                    if improving:
                        allow_heat = False

            local_reversal = False
            if adverse and abs(m_now) >= self.local_reversal_m_abs:
                heat_drop_ok = True
                if last_heat is not None and self.heat_drop_k > 0:
                    try:
                        heat_drop_ok = h_now < float(last_heat) * float(self.heat_drop_k)
                    except Exception:
                        heat_drop_ok = True
                if heat_drop_ok:
                    local_reversal = True

            if allow_heat and htf_supports_side and not (
                self.ignore_htf_on_local_reversal and local_reversal
            ):
                allow_heat = False

            if allow_heat:
                return ExitSig("EXIT", exit_price=close, reason=f"heat<{self.heat_exit_threshold:.2f}")

            if (self.exit_grace_bars <= 0) or (bars_held >= self.exit_grace_bars):
                if adverse:
                    return ExitSig("EXIT", exit_price=close, reason="mom_reverse")

        return _finalize(ExitSig("HOLD"), heat_for_update)

    # ---------- optional: heat reporting only (no decisions here) ----------
    def entry_distance(self, t: Any, sym: str, row: Mapping[str, Any]) -> Dict[str, Any]:
        """Compute gaps against the strategy's filters for a single symbol."""
        m = self._mom_sum(row)
        atrr = _f(row.get("atr_ratio", 0.0))
        qv24 = _f(row.get("qv_24h", 0.0))
        qv1  = _f(row.get("quote_volume", 0.0))
        if qv1 <= 0.0:
            qv1 = _f(row.get("volume", 0.0)) * _f(row.get("close", 0.0))

        min_atr = float(self.min_atr_ratio)
        min_qv24 = float(self.min_qv_24h)
        min_qv1h = float(self.min_qv_1h)
        min_mom = float(self.min_momentum_sum)

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
