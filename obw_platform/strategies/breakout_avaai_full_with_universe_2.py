# strategies/breakout_avaai_full_with_universe.py
# Wrapper that extends the original BreakoutAVAAIFull with `universe(...)`, `rank(...)`
# and now HEAT utilities (`entry_distance`, `best_entry_distance`, `heat`).
#
# Usage in YAML:
#   strategy_class: strategies.breakout_avaai_full_with_universe.BreakoutAVAAIFull
#
# Notes:
# - Heat math mirrors cross_sectional_rs_heat_v2: gaps per metric in [0..1], then
#   heat = 1 - max(gaps). Lower gap -> closer to threshold; heat -> higher = better.
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Any, Optional, Tuple, Mapping, Literal

try:
    # Import the original class from your project
    from .breakout_avaai_full import BreakoutAVAAIFull as _OrigBreakout
except Exception:
    # Fallback import path if used outside a package context
    from breakout_avaai_full import BreakoutAVAAIFull as _OrigBreakout


Side = Literal["LONG", "SHORT"]
ExitAction = Literal["HOLD", "TP", "SL", "EXIT"]


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

    # synonyms
    @property
    def tp(self) -> float:
        return self.take_profit

    @property
    def tp_price(self) -> float:
        return self.take_profit

    @property
    def sl(self) -> float:
        return self.stop_price

    @property
    def sl_price(self) -> float:
        return self.stop_price


@dataclass
class ExitSig:
    action: ExitAction
    exit_price: Optional[float] = None
    reason: Optional[str] = None


class BreakoutAVAAIFull(_OrigBreakout):
    """
    Extends the original BreakoutAVAAIFull with:
      - universe(t, md_slice) -> List[str]
      - rank(t, md_slice, universe) -> List[str]
      - entry_distance(t, sym, row, breadth=None) -> Dict[str, Any]
      - best_entry_distance(t, md_slice, symbols=None) -> Optional[Dict[str, Any]]
      - heat(t, sym, row, breadth=None) -> float  in [0..1]

    Universe & ranking use the same scoring metric:
      score = mom_sum for LONG, -mom_sum for SHORT, abs(mom_sum) for BOTH,
      where mom_sum = dp6h + dp12h.

    Hard filters in scoring: min_atr_ratio, min_qv_24h, min_qv_1h.
    """

    # ---- helpers to read YAML ----
    def _cfg_top(self, key: str, default: Any):
        # prefer top-level cfg, fallback to strategy_params.*
        sp = (getattr(self, "cfg", None) or {}).get("strategy_params", {}) or {}
        return (getattr(self, "cfg", None) or {}).get(key, sp.get(key, default))

    def _cfg_bool(self, keys, default: bool=False) -> bool:
        """Read bool from top-level or strategy_params; accept 1/0, true/false, yes/no, on/off."""
        if isinstance(keys, (list, tuple)):
            for k in keys:
                v = self._cfg_top(k, None)
                if v is not None:
                    if isinstance(v, bool):
                        return v
                    s = str(v).strip().lower()
                    return s in ("1","true","yes","y","on")
            return bool(default)
        v = self._cfg_top(keys, None)
        if v is None:
            return bool(default)
        if isinstance(v, bool):
            return v
        s = str(v).strip().lower()
        return s in ("1","true","yes","y","on")

    def __init__(self, cfg: Dict[str, Any]):
        # keep original init
        super().__init__(cfg)
        # also store the full cfg for top-level keys
        self.cfg = cfg or {}
        # optional breadth cache (if зовнішній код її оновлює)
        self._last_breadth = 1.0

    # ---------- tiny utils ----------
    @staticmethod
    def _float(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
        try:
            v = row.get(key, default)
            return float(v if v is not None else default)
        except Exception:
            return default

    @staticmethod
    def _pct_gap(actual: float, thresh: float) -> float:
        """Distance-to-threshold in [0..1] for 'need actual >= thresh' checks.
        If actual >= thresh -> 0.0 gap, else (thresh-actual)/thresh (clamped to [0..1])."""
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
        """Reverse variant for directional momentum.
        Caller flips sign before passing when needed (SHORT)."""
        try:
            a = float(actual); t = float(thresh)
        except Exception:
            return 1.0
        if t <= 0:
            return 0.0
        if a >= t:
            return 0.0
        return max(0.0, min(1.0, (t - a) / t))

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
            need = float(vol_mult) * avg1h
            ok = qv1h >= need
            gap = self._pct_gap(qv1h, need)
        return ok, gap, dict(qv_24h=qv24, qv_1h=qv1h, avg1h=avg1h, need=need)

    # ---------- scoring helpers (for universe/rank) ----------
    # ------------------ core helpers ------------------
    def _mom_sum(self, row: Mapping[str, Any]) -> float:
        dp6 = self._float(row, "dp6h", 0.0)
        dp12 = self._float(row, "dp12h", 0.0)
        return dp6 + dp12

    def _passes_filters(self, row: Mapping[str, Any], side_pref: str, min_mom: float,
                         min_atr: float, min_qv24: float, min_qv1h: float) -> bool:
        atr = self._float(row, "atr_ratio", 0.0)
        qv24 = self._float(row, "qv_24h", 0.0)
        qv1 = self._float(row, "quote_volume", 0.0)
        if qv1 <= 0.0:
            qv1 = self._float(row, "volume", 0.0) * self._float(row, "close", 0.0)
        mom_sum = self._mom_sum(row)
        if atr < min_atr or qv24 < min_qv24 or qv1 < min_qv1h:
            return False
        if side_pref == "SHORT":
            return mom_sum < -min_mom
        else:  # LONG or BOTH behave like LONG
            return mom_sum > +min_mom

    # ---------- required by runner ----------
    def universe(self, t, md_slice: Dict[str, Dict[str, Any]] | None) -> List[str]:
        """Return symbols that pass hard filters."""
        if not md_slice:
            return []

        side_pref = str(self._cfg_top("side", "BOTH")).upper()
        min_atr = float(self._cfg_top("min_atr_ratio", 0.0))
        min_qv24 = float(self._cfg_top("min_qv_24h", 0.0))
        min_qv1h = float(self._cfg_top("min_qv_1h", 0.0))
        min_mom = float(self._cfg_top("min_momentum_sum", 0.0))

        out: List[str] = []
        for sym, row in md_slice.items():
            if self._passes_filters(row, side_pref, min_mom, min_atr, min_qv24, min_qv1h):
                out.append(sym)
        return out

    def rank(self, t, md_slice: Dict[str, Dict[str, Any]] | None, universe: List[str] | None = None) -> List[str]:
        """Rank symbols strictly by momentum sum (score)."""
        if not md_slice:
            return []

        side_pref = str(self._cfg_top("side", "BOTH")).upper()
        symbols = list(universe) if universe else list(md_slice.keys())

        scored: List[Tuple[str, float]] = []
        for sym in symbols:
            row = md_slice.get(sym, {})
            mom_sum = self._mom_sum(row)
            score = -mom_sum if side_pref == "SHORT" else mom_sum
            scored.append((sym, score))

        scored.sort(key=lambda x: x[1], reverse=True)
        return [sym for sym, _ in scored]

    # ---------- HEAT utilities ----------
    def entry_distance(self, t, sym: str, row: Dict[str, Any], breadth: Optional[float] = None) -> Dict[str, Any]:
        """Compute per-metric gaps against YAML thresholds and a combined gap.
        combined_gap = max(gap_atr, gap_volsurge, gap_qv24, gap_qv1h, gap_momentum, gap_breadth).
        Lower gap -> closer to entry (worst dimension)."""
        # actuals
        dp6  = self._float(row, "dp6h", 0.0)
        dp12 = self._float(row, "dp12h", 0.0)
        mom_sum = dp6 + dp12
        atrr = self._float(row, "atr_ratio", 0.0)

        # thresholds (prefer top-level, fallback to strategy_params)
        min_atr     = float(self._cfg_top("min_atr_ratio",      0.016))
        vol_mult    = float(self._cfg_top("min_vol_surge_mult", 1.20))
        min_qv24    = float(self._cfg_top("min_qv_24h",         200_000))
        min_qv1h    = float(self._cfg_top("min_qv_1h",          10_000))
        min_mom     = float(self._cfg_top("min_momentum_sum",   0.08))
        min_breadth = float(self._cfg_top("min_breadth",        0.0))

        side_pref = str(self._cfg_top("side", "BOTH")).upper()

        # momentum gap depends on allowed side(s)
        if side_pref in ("BOTH", "LONG"):
            # need mom_sum >= +min_mom
            gap_mom = self._pct_gap_rev(mom_sum, +min_mom)
        else:
            # SHORT: need mom_sum <= -min_mom -> flip sign and compare to +min_mom
            gap_mom = self._pct_gap_rev(-mom_sum, +min_mom)

        gap_atr = self._pct_gap(atrr, min_atr)

        # volume / liquidity
        _, gap_vsm, vctx = self._vol_ok_and_gap(row, vol_mult)

        # hard liquidity floors
        qv24 = self._float(row, "qv_24h", 0.0)
        qv1  = self._float(row, "quote_volume", 0.0)
        if qv1 <= 0.0:
            qv1 = self._float(row, "volume", 0.0) * self._float(row, "close", 0.0)
        gap_qv24 = self._pct_gap(qv24, min_qv24)
        gap_qv1  = self._pct_gap(qv1,  min_qv1h)

        # breadth
        if breadth is None:
            breadth = getattr(self, "_last_breadth", 1.0)
        gap_breadth = self._pct_gap(breadth, min_breadth)

        combined_gap = max(gap_atr, gap_vsm, gap_qv24, gap_qv1, gap_mom, gap_breadth)

        # reason: explain the worst (largest) gap
        gaps_map = {
            "atr": gap_atr,
            "volsurge": gap_vsm,
            "qv24": gap_qv24,
            "qv1h": gap_qv1,
            "momentum": gap_mom,
            "breadth": gap_breadth,
        }
        worst_key = max(gaps_map, key=lambda k: gaps_map[k])
        reason = ""
        if worst_key == "volsurge":
            need_v = vctx.get("need", 0.0)
            reason = f"volsurge low: qv1h {qv1:.0f} < need {need_v:.0f}"
        elif worst_key == "qv1h":
            reason = f"qv1h low: {qv1:.0f} < {min_qv1h:.0f}"
        elif worst_key == "qv24":
            reason = f"qv24 low: {qv24:.0f} < {min_qv24:.0f}"
        elif worst_key == "atr":
            reason = f"atr low: {atrr:.4f} < {min_atr:.4f}"
        elif worst_key == "momentum":
            reason = f"momentum low: {mom_sum:.4f} < {min_mom:.4f}"
        elif worst_key == "breadth":
            reason = f"breadth low: {breadth:.3f} < {min_breadth:.3f}"

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
            "reason": reason,
            "thresholds": {
                "min_atr_ratio": float(min_atr),
                "min_vol_surge_mult": float(vol_mult),
                "min_qv_24h": float(min_qv24),
                "min_qv_1h": float(min_qv1h),
                "min_momentum_sum": float(min_mom),
                "min_breadth": float(min_breadth),
            },
        }

    def best_entry_distance(self, t, md_slice: dict, symbols=None) -> Optional[Dict[str, Any]]:
        """Evaluate distances for a set of symbols (or all md_slice) and return the nearest-to-entry item."""
        if not md_slice:
            return None
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

    def heat(self, t, sym: str, row: Dict[str, Any], breadth: Optional[float] = None) -> float:
        """Return heat in [0..1] computed as (1 - max(gaps))."""
        try:
            dist = self.entry_distance(t, sym, row, breadth=breadth)
            gaps = (dist or {}).get("gaps") or {}
            if not gaps:
                return 0.0
            # nearest gap defines heat the most
            worst = max(float(v) for v in gaps.values() if v is not None)
            return max(0.0, min(1.0, 1.0 - worst))
        except Exception:
            return 0.0

    def entry_signal(self, bar_close: bool, symbol: str, row: Mapping[str, Any], ctx: Optional[Mapping[str, Any]] = None) -> Optional[Sig]:
        """Generate mandatory entry signals with TP/SL calculated from ATR multipliers."""
        side_pref = str(self._cfg_top("side", "BOTH")).upper()
        side = "SHORT" if side_pref == "SHORT" else "LONG"

        close = self._float(row, "close", 0.0)
        atrr = self._float(row, "atr_ratio", 0.0)
        tp_mult = self._cfg_top("tp_atr_mult", None)
        sl_mult = self._cfg_top("sl_atr_mult", None)

        try:
            tp_mult = float(tp_mult)
            sl_mult = float(sl_mult)
        except Exception:
            return None

        if close <= 0 or atrr <= 0:
            return None

        atr_abs = max(1e-12, close * atrr)
        if side == "SHORT":
            tp_price = close - tp_mult * atr_abs
            sl_price = close + sl_mult * atr_abs
        else:
            tp_price = close + tp_mult * atr_abs
            sl_price = close - sl_mult * atr_abs

        return Sig(side=side, take_profit=float(tp_price), stop_price=float(sl_price))

    def manage_position(self, symbol: str, row: Mapping[str, Any], pos: Any, ctx: Optional[Mapping[str, Any]] = None) -> ExitSig:
        """Exit only on TP/SL using close price."""
        close = self._float(row, "close", 0.0)
        side = getattr(pos, "side", None)
        tp = getattr(pos, "tp", getattr(pos, "take_profit", None))
        sl = getattr(pos, "sl", getattr(pos, "stop_price", None))

        if side == "LONG":
            if tp is not None and close >= float(tp):
                return ExitSig("TP", exit_price=float(tp))
            if sl is not None and close <= float(sl):
                return ExitSig("SL", exit_price=float(sl))
        elif side == "SHORT":
            if tp is not None and close <= float(tp):
                return ExitSig("TP", exit_price=float(tp))
            if sl is not None and close >= float(sl):
                return ExitSig("SL", exit_price=float(sl))
        return ExitSig("HOLD")
    
