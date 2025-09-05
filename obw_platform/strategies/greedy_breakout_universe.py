
# strategies/greedy_breakout_universe.py
# A minimal, deterministic strategy that reproduces the profitable "greedy" behavior:
# - Universe filter: ONLY min_atr_ratio (no momentum threshold)
# - Rank: score = dp6h + dp12h (SHORT inverts sign); no extra tiebreakers
# - Entry: side from params; if BOTH -> LONG when mom_sum>=0 else SHORT
# - TP/SL: ALWAYS from ATR multiples (tp_atr_mult, sl_atr_mult)
# - Exit: by CLOSE price (not high/low), matching the old backtester behavior
#
# Expected YAML:
# strategy_class: strategies.greedy_breakout_universe.GreedyBreakoutUniverse
# strategy_params:
#   side: BOTH           # or LONG / SHORT
#   top_n: 8
#   min_atr_ratio: 0.02
#   tp_atr_mult: 3.8
#   sl_atr_mult: 1.04
#
from dataclasses import dataclass
from typing import Optional, Literal, Mapping, Any, List, Dict

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

    # synonyms (for other runners)
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

class GreedyBreakoutUniverse:
    def __init__(self, cfg: Mapping[str, Any]) -> None:
        sp = (cfg or {}).get("strategy_params", {}) if isinstance(cfg, dict) else {}
        self.side: str = str(sp.get("side", "BOTH")).upper()
        self.top_n: int = int(sp.get("top_n", 8))
        self.min_atr_ratio: float = _f(sp.get("min_atr_ratio", 0.02), 0.02)
        self.tp_mult: float = _f(sp.get("tp_atr_mult", 3.8), 3.8)
        self.sl_mult: float = _f(sp.get("sl_atr_mult", 1.04), 1.04)

    # --- Helpers ---
    def _mom_sum(self, row: Mapping[str, Any]) -> float:
        return _f(row.get("dp6h", 0.0)) + _f(row.get("dp12h", 0.0))

    # --- Contract: Universe ---
    def universe(self, t: Any, md_map: Mapping[str, Mapping[str, Any]]) -> List[str]:
        # Only ATR filter to reproduce the broad candidate set
        out: List[str] = []
        for sym, row in md_map.items():
            atr_ratio = _f(row.get("atr_ratio", 0.0))
            if atr_ratio >= self.min_atr_ratio:
                out.append(sym)
        return out

    # --- Contract: Rank ---
    def rank(self, t: Any, md_map: Mapping[str, Mapping[str, Any]], universe_syms: List[str]) -> List[str]:
        invert = (self.side == "SHORT")
        scored: List[tuple] = []
        # Preserve input order for equal scores: include index in tuple; Python's sort is stable.
        for idx, sym in enumerate(universe_syms):
            m = self._mom_sum(md_map.get(sym, {}))
            score = (-m) if invert else (m)
            scored.append((score, idx, sym))
        scored.sort(key=lambda x: x[0], reverse=True)  # score desc; idx keeps stability
        return [sym for _, __, sym in scored]

    # --- Contract: Entry ---
    def entry_signal(self, bar_close: bool, symbol: str, row: Mapping[str, Any], ctx: Optional[Mapping[str, Any]] = None) -> Optional[Sig]:
        # Decide side
        ms = self._mom_sum(row)
        side: Side
        if self.side == "LONG":
            side = "LONG"
        elif self.side == "SHORT":
            side = "SHORT"
        else:
            side = "LONG" if ms >= 0.0 else "SHORT"

        close = _f(row.get("close", None), None)
        atr_ratio = _f(row.get("atr_ratio", None), None)
        if close is None or atr_ratio is None:
            return None

        atr_abs = max(1e-12, close * atr_ratio)
        if side == "LONG":
            tp = close + self.tp_mult * atr_abs
            sl = close - self.sl_mult * atr_abs
        else:  # SHORT
            tp = close - self.tp_mult * atr_abs
            sl = close + self.sl_mult * atr_abs

        return Sig(side=side, take_profit=float(tp), stop_price=float(sl), confidence=0.0)

    # --- Contract: Manage/Exit ---
    def manage_position(self, symbol: str, row: Mapping[str, Any], pos: Any, ctx: Optional[Mapping[str, Any]] = None) -> ExitSig:
        # CLOSE-based exits (match the old backtester behavior)
        close = _f(row.get("close", 0.0))
        side: str = getattr(pos, "side", "LONG")
        tp = _f(getattr(pos, "tp", getattr(pos, "take_profit", getattr(pos, "tp_price", None))), None)
        sl = _f(getattr(pos, "sl", getattr(pos, "stop_price", getattr(pos, "sl_price", None))), None)

        if side == "LONG":
            if sl is not None and close <= sl: return ExitSig("SL", exit_price=sl)
            if tp is not None and close >= tp: return ExitSig("TP", exit_price=tp)
        else:  # SHORT
            if sl is not None and close >= sl: return ExitSig("SL", exit_price=sl)
            if tp is not None and close <= tp: return ExitSig("TP", exit_price=tp)

        return ExitSig("HOLD")
