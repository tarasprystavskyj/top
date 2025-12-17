# Auto-generated: TV port wrapper using proven robust engine.
# This file defines CryptomineCLimit14TVPort which reuses CryptomineCLimit14Robust
# (same backtester interface: universe/rank/entry_signal/manage_position).
#
# You can later modify only the parameter translation / decision logic here
# to match TradingView behavior more exactly.

from __future__ import annotations

from typing import Any, Dict

# Import the working robust implementation from the same strategies package.
from strategies.cryptomine_c_limit14_robust import CryptomineCLimit14Robust


class CryptomineCLimit14TVPort(CryptomineCLimit14Robust):
    """TradingView 'C - limit 14' port (initially a robust-compatible wrapper).

    Why this exists:
    - Your backtester requires: universe(), rank(), entry_signal(), manage_position()
      and numeric take_profit/stop_price on entry signals.
    - The previous draft TV port missed some required methods and could emit
      signals without TP/SL, causing runtime errors.

    Current behavior:
    - Uses CryptomineCLimit14Robust logic and reads the SAME strategy_params
      from YAML (tp_pct, subtp_pct, callback_pct, max_buys, d2..d6, m2..m5, etc.)

    Next step (optional):
    - If you want 1:1 TradingView behavior, we can override methods here while
      keeping the interface stable.
    """

    def __init__(self, cfg: Dict[str, Any]):
        # No translation needed: YAML keys already match the robust engine.
        super().__init__(cfg)
