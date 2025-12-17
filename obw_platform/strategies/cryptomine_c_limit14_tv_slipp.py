# cryptomine_c_limit14_tv_port.py
# TV "C - limit 14" port with adaptive dump-sled grid
# Author: ChatGPT (for Taras)
# --------------------------------------------------

from typing import Dict, List
import math

from strategies.base import StrategyBase


class CryptomineCLimit14TVPort(StrategyBase):
    """
    TV C-limit-14 port with adaptive linear-drop based on dump shock.
    """

    def __init__(self, cfg: dict):
        super().__init__(cfg)

        # ---- Base parameters (from YAML / TV defaults)
        self.tp_pct = cfg.get("tp_pct", 1.1) / 100.0
        self.subtp_pct = cfg.get("subtp_pct", 0.5) / 100.0
        self.callback_pct = cfg.get("callback_pct", 0.2) / 100.0

        self.max_steps = int(cfg.get("max_steps", 244))
        self.first_buy_usd = float(cfg.get("first_buy_usd", 5.0))

        # linear drop base (TV-style nonlinear ladder is already baked here)
        self.linear_drop_pct = float(cfg.get("linear_drop_pct", 0.15)) / 100.0

        # buy multipliers
        self.multipliers = cfg.get(
            "multipliers", [1.0, 1.5, 1.0, 2.0, 3.5]
        )

        # ---- Adaptive dump parameters
        self.dump_lookback = int(cfg.get("dump_lookback", 100))
        self.dump_k = float(cfg.get("dump_k", 1.2))
        self.dump_max_mult = float(cfg.get("dump_max_mult", 4.0))

        # ---- Runtime state
        self._bar_drops: Dict[str, List[float]] = {}

    # --------------------------------------------------
    # Required by backtester
    # --------------------------------------------------
    def universe(self, t, md_map):
        return list(md_map.keys())

    # --------------------------------------------------
    def _update_bar_drop(self, sym: str, prev_close: float, low: float):
        if prev_close <= 0:
            return

        drop = max(0.0, (prev_close - low) / prev_close)

        buf = self._bar_drops.setdefault(sym, [])
        buf.append(drop)

        if len(buf) > self.dump_lookback:
            buf.pop(0)

    # --------------------------------------------------
    def _adaptive_drop(self, sym: str) -> float:
        """
        Returns effective linear drop percent for current bar.
        """
        buf = self._bar_drops.get(sym)
        if not buf or len(buf) < 10:
            return self.linear_drop_pct

        avg_drop = sum(buf) / len(buf)
        if avg_drop <= 1e-9:
            return self.linear_drop_pct

        shock = buf[-1] / avg_drop

        if shock <= 1.0:
            return self.linear_drop_pct

        mult = 1.0 + self.dump_k * (shock - 1.0)
        mult = max(1.0, min(mult, self.dump_max_mult))

        return self.linear_drop_pct / mult

    # --------------------------------------------------
    def entry_signal(self, is_open: bool, sym: str, row, ctx=None):
        """
        BUY logic only (DCA accumulation).
        """
        price = row["open"]
        low = row["low"]
        prev_close = row["prev_close"]

        self._update_bar_drop(sym, prev_close, low)

        pos = self.positions.get(sym)
        if pos is None:
            # first buy
            return {
                "side": "buy",
                "price": price,
                "qty_usd": self.first_buy_usd,
                "take_profit": price * (1.0 + self.tp_pct),
            }

        # already in position
        steps = pos.get("steps", 1)
        if steps >= self.max_steps:
            return None

        avg_price = pos["avg_price"]

        eff_drop = self._adaptive_drop(sym)
        next_buy_price = avg_price * (1.0 - eff_drop)

        if price <= next_buy_price:
            mult_idx = min(steps, len(self.multipliers) - 1)
            mult = self.multipliers[mult_idx]

            return {
                "side": "buy",
                "price": price,
                "qty_usd": self.first_buy_usd * mult,
                "take_profit": avg_price * (1.0 + self.tp_pct),
            }

        return None

    # --------------------------------------------------
    def exit_signal(self, is_open: bool, sym: str, row, ctx=None):
        """
        Full TP only (TV-style).
        """
        pos = self.positions.get(sym)
        if pos is None:
            return None

        price = row["open"]
        avg_price = pos["avg_price"]

        tp_price = avg_price * (1.0 + self.tp_pct)

        if price >= tp_price:
            return {
                "side": "sell",
                "price": price,
                "qty": pos["qty"],
            }

        return None
