# strategies/breakout_avaai_full_with_universe.py
# Wrapper that extends the original BreakoutAVAAIFull with `universe(...)` and `rank(...)`
# required by PAPER-API runners (e.g., bt_live_paper_runner_separated.py).
#
# Usage in YAML:
#   strategy_class: strategies.breakout_avaai_full_with_universe.BreakoutAVAAIFull
#
# This file does NOT change your entry/exit logic; it only adds universe+ranking.
from __future__ import annotations

from typing import Dict, List, Any, Optional, Tuple

try:
    # Import the original class from your project
    from .breakout_avaai_full import BreakoutAVAAIFull as _OrigBreakout
except Exception:
    # Fallback import path if used outside a package context
    from breakout_avaai_full import BreakoutAVAAIFull as _OrigBreakout


class BreakoutAVAAIFull(_OrigBreakout):
    """
    Extends the original BreakoutAVAAIFull with:
      - universe(t, md_slice) -> List[str]
      - rank(t, md_slice, universe) -> List[str]

    Universe selection & ranking use the same scoring metric:
      score = mom_sum for LONG, -mom_sum for SHORT, abs(mom_sum) for BOTH,
      where mom_sum = dp6h + dp12h.

    Hard filters: min_atr_ratio, min_qv_24h, min_qv_1h.
    Ties are broken by higher 24h quote-volume, then by higher ATR%.
    """

    # ---- helpers to read YAML ----
    def _cfg_top(self, key: str, default: Any):
        # prefer top-level cfg, fallback to strategy_params.*
        sp = (getattr(self, "cfg", None) or {}).get("strategy_params", {}) or {}
        return (getattr(self, "cfg", None) or {}).get(key, sp.get(key, default))

    def __init__(self, cfg: Dict[str, Any]):
        # keep original init
        super().__init__(cfg)
        # also store the full cfg for top-level keys
        self.cfg = cfg or {}

    # ---------- core extractors ----------
    @staticmethod
    def _float(row: Dict[str, Any], key: str, default: float = 0.0) -> float:
        try:
            v = row.get(key, default)
            return float(v if v is not None else default)
        except Exception:
            return default

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
        min_atr = float(self._cfg_top("min_atr_ratio", 0.0))
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
        """
        Return symbols ordered by desirability (best first). If `universe` is provided,
        only those symbols are considered.
        """
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
