from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd
import numpy as np

def round_tick(x: float, tick_pct: float) -> float:
    if tick_pct <= 0: return float(x)
    step = float(x) * tick_pct
    if step <= 0: return float(x)
    return float(np.round(x / step) * step)

@dataclass
class Position:
    symbol: str
    side: str  # "LONG" or "SHORT"
    entry_time: pd.Timestamp
    entry_price: float
    notional: float
    stop_price: Optional[float] = None
    take_profit: Optional[float] = None
    meta: dict = field(default_factory=dict)

class Portfolio:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.initial_equity = float(cfg.get("initial_equity", 200.0))
        self.equity = float(self.initial_equity)
        self.positions: List[Position] = []
        self.trades = []

    def can_open(self, port_cfg: dict) -> bool:
        max_frac = float(self.cfg.get("max_notional_frac", 0.5))
        max_open = max_frac * self.equity
        current_open = sum(p.notional for p in self.positions)
        return (current_open + float(self.cfg.get("position_notional", 20.0))) <= max_open

    def open(self, symbol, signal, t, last_price) -> Position:
        fee_rate = float(self.cfg.get("fee_rate", 0.001))
        slip = float(self.cfg.get("slippage_per_side", 0.0003))
        tick = float(self.cfg.get("tick_pct", 0.0001))
        notional = float(self.cfg.get("position_notional", 20.0))

        if signal.side == "SHORT":
            entry = round_tick(last_price * (1 - slip), tick)
        else:
            entry = round_tick(last_price * (1 + slip), tick)
        pos = Position(symbol=symbol, side=signal.side, entry_time=t, entry_price=entry, notional=notional,
                       stop_price=signal.stop_price, take_profit=signal.take_profit, meta={"reason": signal.reason, **(signal.tags or {})})
        self.positions.append(pos)
        self.equity -= notional * fee_rate
        return pos

    def open_positions(self):
        return list(self.positions)

    def close(self, pos: Position, t, last_price, reason="exit"):
        fee_rate = float(self.cfg.get("fee_rate", 0.001))
        slip = float(self.cfg.get("slippage_per_side", 0.0003))
        tick = float(self.cfg.get("tick_pct", 0.0001))
        funding_rate_hour = float(self.cfg.get("funding_rate_hour", 0.00002))

        if pos.side == "SHORT":
            exit_px = round_tick(last_price * (1 + slip), tick)
            gross_ret = (pos.entry_price - exit_px) / max(pos.entry_price, 1e-12)
        else:
            exit_px = round_tick(last_price * (1 - slip), tick)
            gross_ret = (exit_px - pos.entry_price) / max(pos.entry_price, 1e-12)

        holding_hours = max(0.0, (t - pos.entry_time).total_seconds()/3600.0)
        costs = 2*fee_rate + funding_rate_hour * holding_hours
        net_ret = gross_ret - costs
        pnl = pos.notional * net_ret
        self.equity += pnl

        self.trades.append({
            "open_time_utc": pos.entry_time.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": pos.symbol,
            "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_time_utc": t.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": exit_px,
            "reason": reason,
            "gross_return": gross_ret,
            "net_return": net_ret,
            "notional": pos.notional,
            "realized_pnl": pnl,
            "equity_after": self.equity
        })
        self.positions = [p for p in self.positions if p is not pos]

    def save_trades(self, path: str):
        pd.DataFrame(self.trades).to_csv(path, index=False)

    def save_summary(self, path: str):
        if not self.trades:
            sm = {
                "equity_start": float(self.initial_equity),
                "equity_end": float(self.equity),
                "trades": 0,
                "profit_factor": 0.0,
                "max_drawdown_%": 0.0,
                "win_rate_%": 0.0
            }
            pd.DataFrame([sm]).to_csv(path, index=False); return
        df = pd.DataFrame(self.trades)
        wins = df.loc[df["realized_pnl"]>0, "realized_pnl"].sum()
        losses = -df.loc[df["realized_pnl"]<0, "realized_pnl"].sum()
        pf = (wins / max(losses,1e-12)) if losses>0 else float("inf")
        eq = df["equity_after"].values
        peak = np.maximum.accumulate(eq)
        dd = (eq/np.maximum(peak,1e-12))-1.0
        max_dd = float(np.min(dd))*100.0 if len(dd) else 0.0
        sm = {
            "equity_start": float(self.initial_equity),
            "equity_end": float(self.equity),
            "trades": int(len(df)),
            "profit_factor": float(pf),
            "max_drawdown_%": max_dd,
            "win_rate_%": float((df["realized_pnl"]>0).mean()*100.0)
        }
        pd.DataFrame([sm]).to_csv(path, index=False)