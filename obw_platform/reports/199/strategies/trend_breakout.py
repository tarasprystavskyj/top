from typing import List
import pandas as pd
import numpy as np
from strategies.base import StrategyBase

class TrendBreakout(StrategyBase):
    """
    Trend breakout strategy: торгує LONG і SHORT, якщо ціна пробиває
    екстремуми за N барів з достатньою волатильністю та моментумом.
    """

    def __init__(self, cfg):
        super().__init__(cfg)
        self.lookback_breakout = cfg.get("lookback_breakout", 20)
        self.breakout_mult = cfg.get("breakout_mult", 1.4)
        self.min_momentum_sum = cfg.get("min_momentum_sum", 0.05)
        self.min_atr_ratio = cfg.get("min_atr_ratio", 0.015)
        self.tp_atr_mult = cfg.get("tp_atr_mult", 2.0)
        self.sl_atr_mult = cfg.get("sl_atr_mult", 1.5)
        self.max_hold_hours = cfg.get("max_hold_hours", 72)
        self.max_mae_atr_mult = cfg.get("max_mae_atr_mult", 1.5)
        self.trail_start_atr = cfg.get("trail_start_atr", 1.0)
        self.trail_dist_atr = cfg.get("trail_dist_atr", 1.0)
        self.mom_flip_thresh = cfg.get("mom_flip_thresh", 0.0)

    def universe(self, t, md):
        df = md.df
        return df[(df["qv_1h"] >= self.cfg.get("min_qv_1h", 10_000)) &
                  (df["qv_24h"] >= self.cfg.get("min_qv_24h", 200_000))].index.tolist()

    def rank(self, t, md, symbols: List[str]):
        # сортуємо по сумарному моментуму за 6h і 12h
        df = md.df.loc[symbols]
        mom_sum = df["dp6h"] + df["dp12h"]
        return mom_sum.sort_values(ascending=False).index.tolist()

    def entry_signal(self, t, sym, row: pd.Series, ctx):
        high_n = row["high"].rolling(self.lookback_breakout).max().iloc[-2]
        low_n = row["low"].rolling(self.lookback_breakout).min().iloc[-2]
        atr_val = row["atr_ratio"].iloc[-1]
        mom_sum = row["dp6h"].iloc[-1] + row["dp12h"].iloc[-1]

        if atr_val < self.min_atr_ratio or mom_sum < self.min_momentum_sum:
            return None

        last_close = row["close"].iloc[-1]

        if last_close > high_n * self.breakout_mult:
            return {"side": "long", "size": ctx.notional}
        elif last_close < low_n / self.breakout_mult:
            return {"side": "short", "size": ctx.notional}
        return None

    def manage_position(self, t, sym, pos, row: pd.Series, ctx):
        atr_val = row["atr_ratio"].iloc[-1]
        price = row["close"].iloc[-1]
        entry_price = pos.entry_price
        pnl = (price - entry_price) * pos.direction_sign

        # SL/TP
        if pnl <= -self.sl_atr_mult * atr_val * entry_price:
            return "exit"
        if pnl >= self.tp_atr_mult * atr_val * entry_price:
            return "exit"

        # trailing stop
        if abs(pnl) >= self.trail_start_atr * atr_val * entry_price:
            if pnl < (self.trail_start_atr - self.trail_dist_atr) * atr_val * entry_price:
                return "exit"

        # max holding time
        if (t - pos.entry_time).total_seconds() / 3600 >= self.max_hold_hours:
            return "exit"

        return None
    