"""
CrossSectionalRSHeat v2
- Heat-first entry: if open_on_heat and heat >= open_heat_min -> synthesize entry per YAML side
- Otherwise, fallback to base entry_signal()
- Enforce YAML side (unless BOTH)
- Auto-fill TP/SL using ATR multipliers if provided in YAML
- Provide entry_distance() and best_entry_distance() helpers
"""

from typing import Optional, Dict, Any

from .base import StrategyBase, Signal, Adjust  # type: ignore
from .cross_sectional_rs import CrossSectionalRS  # type: ignore


class CrossSectionalRSHeat(CrossSectionalRS):
    # ---------- helpers (gaps) ----------
    @staticmethod
    def _pct_gap(actual: float, thresh: float) -> float:
        """
        Distance-to-threshold in [0..+inf) then clamped to [0..1] for heat math.
        If actual >= thresh -> 0.0 gap, else (thresh-actual)/thresh.
        """
        try:
            a = float(actual)
            t = float(thresh)
        except Exception:
            return 1.0
        if t <= 0:
            return 0.0
        if a >= t:
            return 0.0
        return max(0.0, (t - a) / t)

    @staticmethod
    def _pct_gap_rev(actual: float, thresh: float) -> float:
        """
        Reverse variant: need actual >= thresh but caller may flip sign before passing.
        Used for momentum requirement depending on LONG/SHORT.
        """
        try:
            a = float(actual)
            t = float(thresh)
        except Exception:
            return 1.0
        if t <= 0:
            return 0.0
        if a >= t:
            return 0.0
        return max(0.0, (t - a) / t)

    def _vol_ok_and_gap(self, row: Dict[str, Any], vol_mult: float):
        qv24 = float(row.get("qv_24h", 0.0) or 0.0)
        qv1h = float(row.get("quote_volume", 0.0) or 0.0)
        if qv1h <= 0.0:
            # derive from volume × close if present
            try:
                qv1h = float(row.get("volume", 0.0) or 0.0) * float(row.get("close", 0.0) or 0.0)
            except Exception:
                qv1h = 0.0
        avg1h = (qv24 / 24.0) if qv24 > 0 else 0.0
        if avg1h <= 0.0:
            gap = 1.0
            ok = False
            need = 0.0
        else:
            need = vol_mult * avg1h
            ok = qv1h >= need
            gap = self._pct_gap(qv1h, need)
        return ok, gap, dict(qv_24h=qv24, qv_1h=qv1h, avg1h=avg1h, need=need)

    # ---------- public: per-symbol distance ----------
    def entry_distance(self, t, sym: str, row: Dict[str, Any], breadth: Optional[float] = None) -> Dict[str, Any]:
        """
        Compute per-metric gaps against YAML thresholds and a combined gap:
            combined_gap = max(gap_atr, gap_volsurge, gap_qv24, gap_qv1h, gap_momentum, gap_breadth)
        Lower gap -> closer to entry on the worst dimension; meanwhile "heat" is computed elsewhere as 1 - min(gaps).
        Returns dict with 'gaps', 'actuals', 'thresholds', and 'combined_gap'.
        """
        cfg = self.cfg if isinstance(self.cfg, dict) else {}
        sp = (cfg.get("strategy_params") or {})

        # actuals
        dp6  = float(row.get("dp6h",  0.0) or 0.0)
        dp12 = float(row.get("dp12h", 0.0) or 0.0)
        mom_sum = dp6 + dp12
        atrr = float(row.get("atr_ratio", 0.0) or 0.0)

        # thresholds (from YAML strategy_params)
        min_atr     = float(sp.get("min_atr_ratio",     0.016))
        vol_mult    = float(sp.get("min_vol_surge_mult", 1.20))
        min_qv24    = float(sp.get("min_qv_24h",        200_000))
        min_qv1h    = float(sp.get("min_qv_1h",         10_000))
        min_mom     = float(sp.get("min_momentum_sum",  0.08))
        min_breadth = float(sp.get("min_breadth",       0.0))

        # momentum gap depends on allowed side(s)
        side_pref = str(sp.get("side", "BOTH")).upper()
        if side_pref in ("BOTH", "LONG"):
            # need mom_sum >= +min_mom
            gap_mom = self._pct_gap_rev(mom_sum, +min_mom)
        else:
            # SHORT: need mom_sum <= -min_mom -> flip sign and compare to +min_mom
            gap_mom = self._pct_gap_rev(-mom_sum, +min_mom)

        gap_atr = self._pct_gap(atrr, min_atr)

        # volume / liquidity
        vol_ok, gap_vsm, vctx = self._vol_ok_and_gap(row, vol_mult)

        # hard liquidity floors
        qv24 = float(row.get("qv_24h", 0.0) or 0.0)
        qv1  = float(row.get("quote_volume", 0.0) or 0.0)
        if qv1 <= 0.0:
            try:
                qv1 = float(row.get("volume", 0.0) or 0.0) * float(row.get("close", 0.0) or 0.0)
            except Exception:
                qv1 = 0.0
        gap_qv24 = self._pct_gap(qv24, min_qv24)
        gap_qv1  = self._pct_gap(qv1,  min_qv1h)

        # breadth
        if breadth is None:
            breadth = getattr(self, "_last_breadth", 1.0)
        gap_breadth = self._pct_gap(breadth, min_breadth)

        combined_gap = max(gap_atr, gap_vsm, gap_qv24, gap_qv1, gap_mom, gap_breadth)

        return {
            "symbol": sym,
            "combined_gap": float(combined_gap),
            "gaps": {
                "atr": float(gap_atr),
                "volsurge": float(gap_vsm),
                "qv24": float(gap_qv24),
                "qv1h": float(gap_qv1),
                "momentum": float(gap_mom),
                "breadth": float(gap_breadth),
            },
            "actuals": {
                "atr_ratio": float(atrr),
                "qv_24h": float(qv24),
                "qv_1h": float(qv1),
                "mom_sum": float(mom_sum),
                "breadth": float(breadth),
                # equivalent need for surge
                "vol_surge_need": float(vctx.get("need", 0.0)),
            },
            "thresholds": {
                "min_atr_ratio": float(min_atr),
                "min_vol_surge_mult": float(vol_mult),
                "min_qv_24h": float(min_qv24),
                "min_qv_1h": float(min_qv1h),
                "min_momentum_sum": float(min_mom),
                "min_breadth": float(min_breadth),
            },
        }

    # ---------- public: scan for nearest across universe ----------
    def best_entry_distance(self, t, md_slice: dict, symbols=None) -> Optional[Dict[str, Any]]:
        """
        Evaluate distances for a set of symbols (or all md_slice) and return the nearest-to-entry item.
        """
        if symbols is None:
            symbols = list(md_slice.keys())

        breadth = getattr(self, "_last_breadth", 1.0)
        best = None
        best_gap = 1.0
        for sym in symbols:
            row = md_slice.get(sym)
            if not row:
                continue
            dist = self.entry_distance(t, sym, row, breadth=breadth)
            if dist["combined_gap"] < best_gap:
                best_gap = dist["combined_gap"]
                best = dist
        return best

    # ---------- main entry logic ----------
    def entry_signal(self, t, sym: str, row: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> Optional[Signal]:
        """
        Heat-first:
          - If open_on_heat and heat >= open_heat_min -> synthetic signal using YAML side
          - Else, fallback to base signal
        Always enforce YAML side (unless BOTH), and auto-fill TP/SL from ATR multipliers if missing.
        Also enrich tags with per-metric gaps for logging.
        """
        cfg = self.cfg if isinstance(self.cfg, dict) else {}
        sp = (cfg.get("strategy_params") or {})
        side_pref = str(sp.get("side", "BOTH")).upper()
        tp_mult = sp.get("tp_atr_mult", None)
        sl_mult = sp.get("sl_atr_mult", None)
        open_on_heat = bool(cfg.get("open_on_heat", False))
        open_heat_min = float(cfg.get("open_heat_min", 0.80))

        # compute heat (1 - min(gaps))
        heat = 0.0
        dist = None
        try:
            dist = self.entry_distance(t, sym, row, breadth=getattr(self, "_last_breadth", 1.0))
            gaps = (dist or {}).get("gaps") or {}
            if isinstance(gaps, dict) and gaps:
                nearest_gap = min(float(v) for v in gaps.values() if v is not None)
                heat = max(0.0, 1.0 - nearest_gap)
            else:
                heat = float((dist or {}).get("heat", 0.0))
        except Exception:
            pass

        # helper: fill TP/SL from ATR if missing
        def _fill_brackets(signal: Optional[Signal]) -> Optional[Signal]:
            if signal is None:
                return None
            need_brackets = (
                getattr(signal, "take_profit", None) is None or
                getattr(signal, "stop_price", None) is None
            )
            if need_brackets and (tp_mult is not None and sl_mult is not None):
                try:
                    entry_px = float(row.get("close") or 0.0)
                    atr_ratio = float(row.get("atr_ratio") or 0.0)
                    if entry_px > 0.0 and atr_ratio > 0.0:
                        atr_abs = max(1e-12, entry_px * float(atr_ratio))
                        side_up = str(getattr(signal, "side", "LONG")).upper()
                        if side_up == "SHORT":
                            tp_px = entry_px - float(tp_mult) * atr_abs
                            sl_px = entry_px + float(sl_mult) * atr_abs
                        else:
                            tp_px = entry_px + float(tp_mult) * atr_abs
                            sl_px = entry_px - float(sl_mult) * atr_abs
                        signal = Signal(
                            side=signal.side,
                            reason=getattr(signal, "reason", ""),
                            stop_price=sl_px,
                            take_profit=tp_px,
                            max_hold_hours=getattr(signal, "max_hold_hours", None),
                            tags=getattr(signal, "tags", None),
                        )
                except Exception:
                    pass
            return signal

        # 1) Heat-first
        if open_on_heat and heat >= open_heat_min:
            side = side_pref if side_pref in ("LONG", "SHORT") else "LONG"
            reason = f"open_on_heat >= {open_heat_min:.2f} (heat={heat:.3f})"
            sig = Signal(side=side, reason=reason, stop_price=None, take_profit=None,
                         max_hold_hours=sp.get("max_hold_hours", None), tags={"heat": heat})
        else:
            # 2) fallback to base
            sig = super().entry_signal(t, sym, row, ctx)

        # Enforce YAML side (unless BOTH)
        if sig is not None and side_pref in ("LONG", "SHORT"):
            cur = str(getattr(sig, "side", "")).upper()
            if cur not in ("LONG", "SHORT") or cur != side_pref:
                reason = (getattr(sig, "reason", "") or "") + f" | forced_side={side_pref} by YAML"
                sig = Signal(
                    side=side_pref,
                    reason=reason,
                    stop_price=getattr(sig, "stop_price", None),
                    take_profit=getattr(sig, "take_profit", None),
                    max_hold_hours=getattr(sig, "max_hold_hours", None),
                    tags=getattr(sig, "tags", None),
                )

        # Fill brackets if needed
        sig = _fill_brackets(sig)

        # Enrich tags with distances for logging
        if sig is not None and dist is not None:
            try:
                tags = dict(sig.tags or {})
                tags.update({
                    "combined_gap": dist["combined_gap"],
                    "gap_atr": dist["gaps"]["atr"],
                    "gap_volsurge": dist["gaps"]["volsurge"],
                    "gap_qv24": dist["gaps"]["qv24"],
                    "gap_qv1h": dist["gaps"]["qv1h"],
                    "gap_momentum": dist["gaps"]["momentum"],
                    "gap_breadth": dist["gaps"]["breadth"],
                })
                sig = Signal(
                    side=sig.side, reason=sig.reason,
                    stop_price=sig.stop_price, take_profit=sig.take_profit,
                    max_hold_hours=sig.max_hold_hours, tags=tags
                )
            except Exception:
                pass

        # Stash dist for diagnostics when no signal
        if sig is None and dist is not None and isinstance(ctx, dict):
            try:
                ctx.setdefault("last_entry_distance", {})[sym] = dist
            except Exception:
                pass

        return sig
