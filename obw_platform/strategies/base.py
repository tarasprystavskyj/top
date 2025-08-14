from dataclasses import dataclass
from typing import List, Dict, Optional, Literal

Side = Literal["LONG","SHORT"]

@dataclass
class Signal:
    side: Side
    reason: str
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    max_hold_hours: Optional[int] = None
    tags: Optional[Dict] = None

@dataclass
class Adjust:
    action: Literal["HOLD","EXIT","MOVE_SL","MOVE_TP"]
    reason: str
    new_stop: Optional[float] = None
    new_tp: Optional[float] = None
    tags: Optional[Dict] = None

class StrategyBase:
    def __init__(self, cfg: Dict):
        self.cfg = cfg

    def universe(self, t, md_slice) -> List[str]:
        return list(md_slice.keys())

    def rank(self, t, md_slice, symbols: List[str]) -> List[str]:
        return symbols

    def entry_signal(self, t, sym: str, row, ctx) -> Optional[Signal]:
        return None

    def manage_position(self, t, sym: str, pos, row, ctx) -> Adjust:
        return Adjust(action="HOLD", reason="default")

    def hope_score(self, t, sym, pos, indicators: Dict) -> float:
        return 0.0