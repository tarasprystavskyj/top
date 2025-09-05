from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Literal

Side = Literal["LONG", "SHORT"]
ExitAction = Literal["HOLD", "TP", "SL", "EXIT"]


@dataclass
class Sig:
    side: Side
    take_profit: float
    stop_price: float
    confidence: float = 0.0
    size: Optional[float] = None

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


class GreedyBreakoutUniverse:
    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = cfg or {}
        self.params = self.cfg.get("strategy_params", self.cfg)

    def _param(self, key: str, default: Any) -> Any:
        return self.params.get(key, self.cfg.get(key, default))

    # -------- universe ---------
    def universe(self, t, md_map: Dict[str, Dict[str, Any]]) -> List[str]:
        if not md_map:
            return []
        min_atr = float(self._param("min_atr_ratio", 0.02))
        symbols: List[str] = []
        for sym, row in md_map.items():
            try:
                atr = float(row.get("atr_ratio") or 0.0)
            except Exception:
                continue
            if atr >= min_atr:
                symbols.append(sym)
        return symbols

    # -------- ranking ---------
    def rank(self, t, md_map: Dict[str, Dict[str, Any]], universe_syms: List[str]) -> List[str]:
        side_pref = str(self._param("side", "BOTH")).upper()
        scores: List[tuple[float, str]] = []
        for sym in universe_syms:
            row = md_map.get(sym, {})
            dp6 = row.get("dp6h") or 0.0
            dp12 = row.get("dp12h") or 0.0
            try:
                mom_sum = float(dp6) + float(dp12)
            except Exception:
                mom_sum = 0.0
            score = -mom_sum if side_pref == "SHORT" else mom_sum
            scores.append((score, sym))
        scores.sort(key=lambda x: x[0], reverse=True)
        return [sym for _, sym in scores]

    # -------- entry ---------
    def entry_signal(
        self, bar_close: float, symbol: str, row: Dict[str, Any], ctx: Any | None = None
    ) -> Optional[Sig]:
        try:
            close = float(bar_close)
            atr_ratio = float(row.get("atr_ratio"))
        except Exception:
            return None
        if close != close or atr_ratio != atr_ratio:  # NaN checks
            return None

        dp6 = row.get("dp6h") or 0.0
        dp12 = row.get("dp12h") or 0.0
        mom_sum = float(dp6) + float(dp12)

        side_pref = str(self._param("side", "BOTH")).upper()
        if side_pref == "BOTH":
            side: Side = "LONG" if mom_sum >= 0 else "SHORT"
        else:
            side = "LONG" if side_pref == "LONG" else "SHORT"

        atr_abs = close * atr_ratio
        tp_mult = float(self._param("tp_atr_mult", 3.8))
        sl_mult = float(self._param("sl_atr_mult", 1.04))

        if side == "LONG":
            tp = close + tp_mult * atr_abs
            sl = close - sl_mult * atr_abs
        else:  # SHORT
            tp = close - tp_mult * atr_abs
            sl = close + sl_mult * atr_abs

        return Sig(side=side, take_profit=tp, stop_price=sl)

    # -------- exit / manage ---------
    def manage_position(
        self, symbol: str, row: Dict[str, Any], pos: Any, ctx: Any | None = None
    ) -> ExitSig:
        try:
            close = float(row.get("close"))
        except Exception:
            return ExitSig("HOLD")

        side = getattr(pos, "side", None)
        if side is None and isinstance(pos, dict):
            side = pos.get("side")

        tp = getattr(pos, "take_profit", None)
        if tp is None and isinstance(pos, dict):
            tp = pos.get("take_profit") or pos.get("tp")

        sl = getattr(pos, "stop_price", None)
        if sl is None and isinstance(pos, dict):
            sl = pos.get("stop_price") or pos.get("sl")

        if side == "LONG":
            if tp is not None and close >= tp:
                return ExitSig("TP", exit_price=float(tp))
            if sl is not None and close <= sl:
                return ExitSig("SL", exit_price=float(sl))
        elif side == "SHORT":
            if tp is not None and close <= tp:
                return ExitSig("TP", exit_price=float(tp))
            if sl is not None and close >= sl:
                return ExitSig("SL", exit_price=float(sl))

        return ExitSig("HOLD")
