
from dataclasses import dataclass, field
from typing import List, Optional
import pandas as pd, numpy as np

def round_tick(x: float, tick_pct: float) -> float:
    if tick_pct <= 0: return float(x)
    step = float(x) * tick_pct
    if step <= 0: return float(x)
    return float(np.round(x / step) * step)

@dataclass
class Position:
    symbol: str
    side: str
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
        # running metrics for equity accounting
        self.position_value = 0.0
        self.unrealized_pnl = 0.0
        self.realized_pnl_cum = 0.0
        # cache frequently used config knobs
        self.fee_rate = float(cfg.get("fee_rate", 0.001))
        self.slippage_per_side = float(cfg.get("slippage_per_side", 0.0003))
        self.tick_pct = float(cfg.get("tick_pct", 0.0001))
        self.default_notional = float(cfg.get("position_notional", 20.0))
        self.total_fees = 0.0

    def mark_equity(self, price_map: dict) -> float:
        """Mark portfolio equity to market using provided symbol->price map."""
        fee = self.fee_rate
        slip = self.slippage_per_side
        unreal = 0.0
        for p in self.positions:
            px = price_map.get(p.symbol)
            if px is None:
                continue
            if p.side == "LONG":
                gross = (px - p.entry_price) / max(p.entry_price, 1e-12)
            else:
                gross = (p.entry_price - px) / max(p.entry_price, 1e-12)
            net = gross - 2 * fee - 2 * slip
            unreal += net * p.notional
        self.unrealized_pnl = unreal
        self.position_value = sum(p.notional for p in self.positions)
        return self.equity + self.unrealized_pnl

    def can_open(self, port_cfg: dict, price_map: Optional[dict] = None) -> bool:
        max_frac = float(port_cfg.get("max_notional_frac", self.cfg.get("max_notional_frac", 0.5)))
        notional = float(port_cfg.get("position_notional", self.default_notional))
        eq = self.equity
        if price_map:
            eq = self.mark_equity(price_map)
        current_open = sum(p.notional for p in self.positions)
        return (current_open + notional) <= max_frac * eq

    def open(self, symbol, signal, t, last_price) -> Position:
        slip = self.slippage_per_side
        tick = self.tick_pct
        notional = float(signal.get("notional", self.cfg.get("position_notional", self.default_notional)))
        entry = round_tick(last_price * (1 - slip) if signal["side"]=="SHORT" else last_price * (1 + slip), tick)
        pos = Position(symbol=symbol, side=signal["side"], entry_time=t, entry_price=entry, notional=notional,
                       stop_price=signal.get("stop_price"), take_profit=signal.get("take_profit"),
                       meta={"reason": signal.get("reason","")})
        self.positions.append(pos)
        self.position_value += notional
        self.equity -= notional * self.fee_rate
        return pos

    def open_positions(self):
        return list(self.positions)

    def close(self, pos: Position, t, last_price, reason="exit"):
        slip = self.slippage_per_side
        tick = self.tick_pct
        funding_rate_hour = float(self.cfg.get("funding_rate_hour", 0.00002))

        exit_px = round_tick(last_price * (1 + slip) if pos.side=="SHORT" else last_price * (1 - slip), tick)
        gross = (pos.entry_price - exit_px)/max(pos.entry_price,1e-12) if pos.side=="SHORT" else (exit_px - pos.entry_price)/max(pos.entry_price,1e-12)
        holding_hours = max(0.0, (t - pos.entry_time).total_seconds()/3600.0)
        # total trading costs including entry/exit fees and funding
        costs = 2*self.fee_rate + funding_rate_hour * holding_hours
        fees_paid = pos.notional * costs
        net = gross - costs
        pnl = pos.notional * net
        self.equity += pnl
        self.position_value -= pos.notional
        self.realized_pnl_cum += pnl
        self.total_fees += fees_paid
        self.trades.append({
            "open_time_utc": pos.entry_time.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": pos.symbol, "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_time_utc": t.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": exit_px, "reason": reason,
            "gross_return": gross, "net_return": net,
            "notional": pos.notional, "fees_paid": fees_paid,
            "realized_pnl": pnl, "equity_after": self.equity
        })
        self.positions = [p for p in self.positions if p is not pos]
        return pnl

    def close_partial(self, pos: Position, t, last_price, qty_frac, reason="partial"):
        slip = self.slippage_per_side
        tick = self.tick_pct
        funding_rate_hour = float(self.cfg.get("funding_rate_hour", 0.00002))
        qty_frac = max(0.0, min(1.0, float(qty_frac)))
        if qty_frac <= 0:
            return 0.0
        exit_px = round_tick(last_price * (1 + slip) if pos.side == "SHORT" else last_price * (1 - slip), tick)
        gross = (pos.entry_price - exit_px) / max(pos.entry_price, 1e-12) if pos.side == "SHORT" else (exit_px - pos.entry_price) / max(pos.entry_price, 1e-12)
        holding_hours = max(0.0, (t - pos.entry_time).total_seconds() / 3600.0)
        costs = self.fee_rate + funding_rate_hour * holding_hours
        fees_paid = pos.notional * qty_frac * costs
        net = gross - costs
        pnl = pos.notional * qty_frac * net
        self.equity += pnl
        self.position_value -= pos.notional * qty_frac
        self.realized_pnl_cum += pnl
        self.total_fees += fees_paid
        self.trades.append({
            "open_time_utc": pos.entry_time.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
            "symbol": pos.symbol, "side": pos.side,
            "entry_price": pos.entry_price,
            "exit_time_utc": t.tz_convert("UTC").strftime("%Y-%m-%d %H:%M:%S"),
            "exit_price": exit_px, "reason": reason,
            "gross_return": gross, "net_return": net,
            "notional": pos.notional * qty_frac, "fees_paid": fees_paid,
            "realized_pnl": pnl, "equity_after": self.equity
        })
        pos.notional *= (1 - qty_frac)
        return pnl

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
                "win_rate_%": 0.0,
                "total_fees": float(self.total_fees),
            }
            pd.DataFrame([sm]).to_csv(path, index=False)
            return
        df = pd.DataFrame(self.trades)
        wins = df.loc[df["realized_pnl"] > 0, "realized_pnl"].sum()
        losses = -df.loc[df["realized_pnl"] < 0, "realized_pnl"].sum()
        pf = (wins / max(losses, 1e-12)) if losses > 0 else float("inf")
        eq = df["equity_after"].values
        peak = np.maximum.accumulate(eq)
        dd = (eq / np.maximum(peak, 1e-12)) - 1.0
        sm = {
            "equity_start": float(self.initial_equity),
            "equity_end": float(self.equity),
            "trades": int(len(df)),
            "profit_factor": float(pf),
            "max_drawdown_%": float(np.min(dd)) * 100.0 if len(dd) else 0.0,
            "win_rate_%": float((df["realized_pnl"] > 0).mean() * 100.0),
            "total_fees": float(self.total_fees),
        }
        pd.DataFrame([sm]).to_csv(path, index=False)
