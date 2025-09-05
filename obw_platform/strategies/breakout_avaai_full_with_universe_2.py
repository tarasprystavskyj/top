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
    def _score_row(self, row: Dict[str, Any], side_pref: str, min_mom: float) -> Optional[Tuple[float, float, float]]:
        """Return (score, qv24, atrp) or None if filtered out."""
        dp6  = self._float(row, "dp6h", 0.0)
        dp12 = self._float(row, "dp12h", 0.0)
        mom_sum = dp6 + dp12

        atrp = self._float(row, "atr_ratio", 0.0)  # ATR/close
        qv24 = self._float(row, "qv_24h", 0.0)
        qv1  = self._float(row, "quote_volume", 0.0)
        if qv1 <= 0.0:
            vol = self._float(row, "volume", 0.0)
            close = self._float(row, "close", 0.0)
            qv1 = vol * close

        # Thresholds
        min_atr  = float(self._cfg_top("min_atr_ratio", 0.0))
        min_qv24 = float(self._cfg_top("min_qv_24h", 200000.0))
        min_qv1h = float(self._cfg_top("min_qv_1h", 10000.0))
        if atrp < min_atr or qv24 < min_qv24 or qv1 < min_qv1h:
            return None

        # Directional threshold & score
        if side_pref == "LONG":
            if mom_sum < +min_mom:
                return None
            score = mom_sum
        elif side_pref == "SHORT":
            if mom_sum > -min_mom:
                return None
            score = -mom_sum  # more negative mom -> larger score for shorts
        else:  # BOTH
            if abs(mom_sum) < min_mom:
                return None
            score = abs(mom_sum)

        return (score, qv24, atrp)

    # ---------- required by runner ----------
    def universe(self, t, md_slice: Dict[str, Dict[str, Any]] | None) -> List[str]:
        side_pref  = str(self._cfg_top("side", "BOTH")).upper()
        top_n      = int(self._cfg_top("top-n", 12))
        min_mom    = float(self._cfg_top("min_momentum_sum", 0.02))

        if not md_slice:
            return []

        scored: List[Tuple[str, float, float, float]] = []  # (sym, score, qv24, atrp)
        for sym, row in md_slice.items():
            s = self._score_row(row, side_pref, min_mom)
            if s is None:
                continue
            score, qv24, atrp = s
            scored.append((sym, score, qv24, atrp))

        # sort by score desc, then qv24 desc, then atrp desc
        scored.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)
        return [sym for sym, *_ in scored[:max(1, top_n)]]

    def rank(self, t, md_slice: Dict[str, Dict[str, Any]] | None, universe: List[str] | None = None) -> List[str]:
        """Return symbols ordered by desirability (best first). If `universe` is provided,
        only those symbols are considered."""
        side_pref  = str(self._cfg_top("side", "BOTH")).upper()
        min_mom    = float(self._cfg_top("min_momentum_sum", 0.02))

        if not md_slice:
            return []

        symbols = list(universe) if universe else list(md_slice.keys())

        scored: List[Tuple[str, float, float, float]] = []  # (sym, score, qv24, atrp)
        for sym in symbols:
            row = md_slice.get(sym, {})
            s = self._score_row(row, side_pref, min_mom)
            if s is None:
                continue
            score, qv24, atrp = s
            scored.append((sym, score, qv24, atrp))

        scored.sort(key=lambda x: (x[1], x[2], x[3]), reverse=True)
        return [sym for sym, *_ in scored]

        # Fallback: if filtered list is empty but open_on_heat is enabled, use HEAT ranking
        if not scored and self._cfg_bool(['open_on_heat','strategy_params.open_on_heat'], False):
            heats = []
            for sym in symbols:
                row2 = md_slice.get(sym, {})
                try:
                    d2 = self.entry_distance(t, sym, row2)
                    h2 = max(0.0, 1.0 - float(d2.get('combined_gap', 1.0)))
                except Exception:
                    h2 = 0.0
                heats.append((sym, h2))
            heats.sort(key=lambda x: x[1], reverse=True)
            top_n = int(self._cfg_top('top-n', 12))
            return [s for s,_ in heats[:max(1, top_n)]]

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
        """Return entry `Sig` with explicit TP/SL or None."""
        # --- try base breakout logic first ---
        base_sig = None
        try:
            base_sig = super().entry_signal(bar_close, symbol, dict(row), ctx=ctx)
        except Exception:
            base_sig = None
        if base_sig is not None and getattr(base_sig, "take", False) and getattr(base_sig, "side", None) in ("LONG", "SHORT"):
            side = getattr(base_sig, "side")
            close = self._float(row, "close", 0.0)
            atrr = self._float(row, "atr_ratio", 0.0)
            tp_mult = self._cfg_top("tp_atr_mult", None)
            sl_mult = self._cfg_top("sl_atr_mult", None)
            if tp_mult is None or sl_mult is None or atrr <= 0 or close <= 0:
                return None
            atr_abs = max(1e-12, close * float(atrr))
            if side == "SHORT":
                tp_price = close - float(tp_mult) * atr_abs
                sl_price = close + float(sl_mult) * atr_abs
            else:
                tp_price = close + float(tp_mult) * atr_abs
                sl_price = close - float(sl_mult) * atr_abs
            return Sig(side=side, take_profit=tp_price, stop_price=sl_price, reason="base_entry")

        # --- heat-based entry ---
        if not self._cfg_bool(["open_on_heat", "strategy_params.open_on_heat"], False):
            return None

        th = self._cfg_top("open_heat_min", None)
        if th is None:
            th = self._cfg_top("heat_min", 0.80)
        try:
            heat_min = float(th)
        except Exception:
            heat_min = 0.80

        try:
            dist = self.entry_distance(bar_close, symbol, row, breadth=getattr(self, "_last_breadth", 1.0))
            heat = max(0.0, 1.0 - float(dist.get("combined_gap", 1.0)))
        except Exception:
            return None

        if heat < heat_min:
            return None

        side_pref = str(self._cfg_top("side", "BOTH")).upper()
        dp6 = self._float(row, "dp6h", 0.0)
        dp12 = self._float(row, "dp12h", 0.0)
        mom_sum = dp6 + dp12
        if side_pref == "LONG":
            side = "LONG"
        elif side_pref == "SHORT":
            side = "SHORT"
        else:
            side = "SHORT" if mom_sum < 0 else "LONG"

        close = self._float(row, "close", 0.0)
        atrr = self._float(row, "atr_ratio", 0.0)
        tp_mult = self._cfg_top("tp_atr_mult", None)
        sl_mult = self._cfg_top("sl_atr_mult", None)
        if tp_mult is None or sl_mult is None or atrr <= 0 or close <= 0:
            return None
        atr_abs = max(1e-12, close * float(atrr))
        if side == "SHORT":
            tp_price = close - float(tp_mult) * atr_abs
            sl_price = close + float(sl_mult) * atr_abs
        else:
            tp_price = close + float(tp_mult) * atr_abs
            sl_price = close - float(sl_mult) * atr_abs

        return Sig(side=side, take_profit=tp_price, stop_price=sl_price, reason=f"open_on_heat >= {heat_min:.4f}")

    def manage_position(self, symbol: str, row: Mapping[str, Any], pos: Any, ctx: Optional[Mapping[str, Any]] = None) -> ExitSig:
        """Decide whether to close the position based on TP/SL."""
        high = self._float(row, "high", self._float(row, "close", 0.0))
        low = self._float(row, "low", self._float(row, "close", 0.0))
        side = getattr(pos, "side", None)
        tp = getattr(pos, "tp", getattr(pos, "take_profit", None))
        sl = getattr(pos, "sl", getattr(pos, "stop_price", None))
        if side == "LONG":
            if sl is not None and low <= float(sl):
                return ExitSig("SL", exit_price=float(sl), reason="stop_loss")
            if tp is not None and high >= float(tp):
                return ExitSig("TP", exit_price=float(tp), reason="take_profit")
        elif side == "SHORT":
            if sl is not None and high >= float(sl):
                return ExitSig("SL", exit_price=float(sl), reason="stop_loss")
            if tp is not None and low <= float(tp):
                return ExitSig("TP", exit_price=float(tp), reason="take_profit")
        return ExitSig("HOLD")
    
