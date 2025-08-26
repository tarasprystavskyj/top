
from .base import StrategyBase, Signal, Adjust
from .cross_sectional_rs import CrossSectionalRS

class CrossSectionalRSHeat(CrossSectionalRS):
    """
    Drop-in variant of CrossSectionalRS with *heat* helpers:
      • entry_distance(t, sym, row, breadth=None) -> dict
      • best_entry_distance(t, md_slice, symbols=None) -> dict

    - Uses thresholds strictly from self.cfg (your YAML), so no extra CLI flags.
    - Does NOT change entry/exit logic; only adds metrics.
    - If you want the runner to print the market "heat", call best_entry_distance()
      every bar and format the result.
    """

    # ---------- helpers ----------
    @staticmethod
    def _pct_gap(actual: float, thresh: float) -> float:
        """
        Relative gap to threshold in [0..+inf), truncated to [0..1] for heat.
        If actual >= thresh, returns 0.0 (no gap).
        Else returns (thresh-actual)/max(thresh, eps).
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
        For conditions of the form actual >= +K (LONG) or actual <= -K (SHORT) after sign flip.
        Here we encode "distance" to min momentum requirement.
        For LONG: need mom_sum >= +min_mom; gap = max(0, (min_mom - mom_sum)/min_mom).
        For SHORT: need mom_sum <= -min_mom; gap = max(0, (mom_sum + min_mom)/min_mom).
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

    def _vol_ok_and_gap(self, row, vol_mult: float):
        qv24 = float(row.get("qv_24h", 0.0) or 0.0)
        qv1h = float(row.get("quote_volume", 0.0) or 0.0)
        if qv1h <= 0.0:
            # try to derive from volume * close if provided
            try:
                qv1h = float(row.get("volume", 0.0) or 0.0) * float(row.get("close", 0.0) or 0.0)
            except Exception:
                qv1h = 0.0
        avg1h = (qv24 / 24.0) if qv24 > 0 else 0.0
        if avg1h <= 0.0:
            gap = 1.0
            ok = False
        else:
            need = vol_mult * avg1h
            ok = qv1h >= need
            gap = self._pct_gap(qv1h, need)
        return ok, gap, dict(qv_24h=qv24, qv_1h=qv1h, avg1h=avg1h, need=vol_mult*avg1h if avg1h>0 else 0.0)

    # ---------- public: per-symbol distance ----------
    def entry_distance(self, t, sym, row, breadth=None):
        """
        Compute per-metric gaps against YAML thresholds and a combined gap in [0..1]:
          combined_gap = max(gap_atr, gap_volsurge, gap_qv24, gap_qv1h, gap_momentum, gap_breadth)
        Lower gap -> closer to entry. If all gaps are 0, the symbol meets entry filters (by thresholds).
        Returns dict:
          {
            "symbol": sym,
            "combined_gap": float,
            "gaps": {"atr":...,"volsurge":...,"qv24":...,"qv1h":...,"momentum":...,"breadth":...},
            "actuals": { "atr_ratio":..., "vol_surge_mult_equiv":..., "qv_24h":..., "qv_1h":..., "mom_sum":..., "breadth":... },
            "thresholds": { "min_atr_ratio":..., "min_vol_surge_mult":..., "min_qv_24h":..., "min_qv_1h":..., "min_momentum_sum":..., "min_breadth":... },
          }
        """
        dp6  = float(row.get("dp6h", 0.0) or 0.0)
        dp12 = float(row.get("dp12h", 0.0) or 0.0)
        mom_sum = dp6 + dp12
        atrr = float(row.get("atr_ratio", 0.0) or 0.0)

        min_atr     = float(self.cfg.get("min_atr_ratio", 0.016))
        vol_mult    = float(self.cfg.get("min_vol_surge_mult", 1.20))
        min_qv24    = float(self.cfg.get("min_qv_24h", 200_000))
        min_qv1h    = float(self.cfg.get("min_qv_1h", 10_000))
        min_mom     = float(self.cfg.get("min_momentum_sum", 0.08))
        min_breadth = float(self.cfg.get("min_breadth", 0.0))

        # momentum gap depends on allowed side(s)
        side_pref = str(self.cfg.get("side", "BOTH")).upper()
        if side_pref in ("BOTH", "LONG"):
            # LONG case: need mom_sum >= +min_mom
            gap_mom = self._pct_gap_rev(mom_sum, +min_mom)
        else:
            # SHORT case: need mom_sum <= -min_mom -> flip sign and compare to +min_mom
            gap_mom = self._pct_gap_rev(-mom_sum, +min_mom)

        gap_atr = self._pct_gap(atrr, min_atr)

        # volume surge / liquidity
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
                "vol_surge_mult_equiv": float(vctx.get("need", 0.0)),
                "qv_24h": float(qv24),
                "qv_1h": float(qv1),
                "mom_sum": float(mom_sum),
                "breadth": float(breadth),
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
    def best_entry_distance(self, t, md_slice: dict, symbols=None):
        """
        Evaluate distances for a set of symbols (or the whole md_slice) and
        return the nearest-to-entry item:
          { "symbol": ..., "combined_gap": ..., "gaps": {...}, "actuals": {...}, "thresholds": {...} }
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

    # ---------- OPTIONAL: enrich tags on allowed entry (does not change logic) ----------
    def entry_signal(self, t, sym, row, ctx):
        sig = super().entry_signal(t, sym, row, ctx)
        # Attach distance info to tags (purely informational)
        try:
            dist = self.entry_distance(t, sym, row, breadth=getattr(self, "_last_breadth", 1.0))
            if sig is not None:
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
                # Rebuild the Signal with enriched tags (keep exact prices/meta)
                return Signal(
                    side=sig.side, reason=sig.reason,
                    stop_price=sig.stop_price, take_profit=sig.take_profit,
                    max_hold_hours=sig.max_hold_hours, tags=tags
                )
            else:
                # Optionally expose into ctx so the runner can inspect top-N heat even when no entry
                if isinstance(ctx, dict):
                    heat_map = ctx.setdefault("last_entry_distance", {})
                    heat_map[sym] = dist
        except Exception:
            pass
        return sig
