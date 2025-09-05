
# strategies/breakout_avaai_full_with_universe_2_refactored.py
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
ExitAction = Literal["HOLD","TP","SL","EXIT"]

@dataclass
class Sig:
    side: Side
    take_profit: float
    stop_price: float
    confidence: float = 0.0
    size: Optional[float] = None
    reason: Optional[str] = None
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

def _f(x, default=0.0) -> float:
    try: return float(x)
    except Exception: return float(default)

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

        # liquidity floors (kept but set lenient defaults; can be overridden in YAML)
        self.min_qv_24h: float = float(_read("min_qv_24h", 0.0))
        self.min_qv_1h: float  = float(_read("min_qv_1h", 0.0))

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

    # ---------- universe & ranking ----------
    def universe(self, t: Any, md_map: Mapping[str, Mapping[str, Any]]) -> List[str]:
        """Filter symbols by minimal ATR and liquidity. Momentum threshold is optional and
        defaults to 0.0 (disabled) to match the profitable setting discovered earlier."""
        out: List[str] = []
        for sym, row in md_map.items():
            if _f(row.get("atr_ratio", 0.0)) < self.min_atr_ratio:
                continue
            if not self._liq_ok(row):
                continue
            # Optional momentum threshold
            m = self._mom_sum(row)
            mm = self.min_momentum_sum
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
        # CUT to top_n here (moved from backtester)
        take = max(1, int(self.top_n))
        return [sym for _, __, sym in scored[:take]]

    # ---------- entry / exit ----------
    def entry_signal(self, bar_close: bool, symbol: str, row: Mapping[str, Any], ctx: Optional[Mapping[str, Any]] = None) -> Optional[Sig]:
        """Return full Sig with TP/SL. No fallbacks in backtester."""
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

        return Sig(side=side, take_profit=float(tp), stop_price=float(sl), reason="rule/atr-multipliers")

    def manage_position(self, symbol: str, row: Mapping[str, Any], pos: Any, ctx: Optional[Mapping[str, Any]] = None):
        """CLOSE-based TP/SL (match the old behaviour)."""
        close = _f(row.get("close", 0.0))
        side  = getattr(pos, "side", "LONG")
        tp    = _f(getattr(pos, "tp", getattr(pos, "take_profit", getattr(pos, "tp_price", None))), None)
        sl    = _f(getattr(pos, "sl", getattr(pos, "stop_price", getattr(pos, "sl_price", None))), None)

        if side == "LONG":
            if sl is not None and close <= sl: return ExitSig("SL", exit_price=sl)
            if tp is not None and close >= tp: return ExitSig("TP", exit_price=tp)
        else:
            if sl is not None and close >= sl: return ExitSig("SL", exit_price=sl)
            if tp is not None and close <= tp: return ExitSig("TP", exit_price=tp)
        return ExitSig("HOLD")

    # ---------- optional: heat reporting only (no decisions here) ----------
    def heat(self, t: Any, symbol: str, row: Mapping[str, Any]) -> Optional[float]:
        """Purely reporting helper. For now: normalized |momentum| with soft cap."""
        try:
            m = abs(self._mom_sum(row))
            # normalize by a soft scale so it ends in [0..1)-ish for reports
            scale = max(1e-9, self.min_momentum_sum if self.min_momentum_sum>0 else 0.08)
            h = m / (scale * 2.0)
            if h > 1.0: h = 1.0
            return float(h)
        except Exception:
            return None
