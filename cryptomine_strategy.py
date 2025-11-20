"""Cryptomine DCA trading strategy and backtester.

This module provides a bar-based backtester that simulates the
Cryptomine grid/DCA strategy described in the provided technical
specification. Only pandas, numpy and the Python standard library are
required.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional, Dict, Any

import numpy as np
import pandas as pd


@dataclass
class CryptomineConfig:
    """Configuration parameters for the Cryptomine strategy."""

    first_buy_usdt: float = 5.0
    margin_call_limit: int = 5
    margin_call_drop: float = 0.5  # percentage drop between grid levels
    tp_percent: float = 1.3  # take-profit target in percent
    callback_percent: float = 0.2  # trailing callback percentage
    auto_merge: bool = True
    nonlinear_multipliers: List[float] = field(default_factory=lambda: [1.0, 1.5, 1.0, 2.0, 3.5])
    timeframe: str = "1h"
    capital_per_coin: float = 1000.0
    max_active_trades: Optional[int] = None
    sl_enabled: bool = False
    sl_type: str = "from_avg"  # from_initial, from_avg, from_equity
    sl_value_percent: float = 5.0


@dataclass
class TradeEvent:
    """Represents a buy or sell event executed by the strategy."""

    timestamp: Any
    event_type: str
    price: float
    quantity: float
    pnl_usdt: float = 0.0


@dataclass
class SymbolState:
    """Stateful information for a single traded symbol."""

    position_size: float = 0.0
    position_cost_usdt: float = 0.0
    avg_price: float = 0.0
    num_buys: int = 0
    mode: str = "whole_warehouse"
    next_grid_levels: List[Dict[str, float]] = field(default_factory=list)
    tp_price: float = 0.0
    trailing_active: bool = False
    trailing_max_price: float = 0.0
    used_capital_usdt: float = 0.0
    first_buy_price: float = 0.0
    buy_lots: List[Dict[str, float]] = field(default_factory=list)

    def reset(self) -> None:
        """Reset the state after a full exit."""

        self.position_size = 0.0
        self.position_cost_usdt = 0.0
        self.avg_price = 0.0
        self.num_buys = 0
        self.mode = "whole_warehouse"
        self.next_grid_levels.clear()
        self.tp_price = 0.0
        self.trailing_active = False
        self.trailing_max_price = 0.0
        self.used_capital_usdt = 0.0
        self.first_buy_price = 0.0
        self.buy_lots.clear()

    @property
    def has_position(self) -> bool:
        return self.position_size > 0


@dataclass
class BacktestResult:
    trades: List[TradeEvent]
    total_pnl_usdt: float
    total_pnl_percent: float
    max_drawdown_percent: float
    max_used_capital_usdt: float
    num_full_cycles: int

    def summary(self) -> Dict[str, float]:
        return {
            "total_pnl_usdt": self.total_pnl_usdt,
            "total_pnl_percent": self.total_pnl_percent,
            "max_drawdown_percent": self.max_drawdown_percent,
            "max_used_capital_usdt": self.max_used_capital_usdt,
            "num_full_cycles": self.num_full_cycles,
            "num_trades": len(self.trades),
        }


def percent_to_multiplier(percent: float) -> float:
    return percent / 100.0


def get_multiplier(config: CryptomineConfig, level_index: int) -> float:
    """Return the volume multiplier for a given DCA level."""

    if level_index < len(config.nonlinear_multipliers):
        return config.nonlinear_multipliers[level_index]
    return 1.0


def update_tp_price(state: SymbolState, config: CryptomineConfig) -> None:
    tp_mult = 1 + percent_to_multiplier(config.tp_percent)
    state.tp_price = state.avg_price * tp_mult


def schedule_next_level(state: SymbolState, config: CryptomineConfig, last_buy_price: float) -> None:
    """Append a new grid level based on the last buy price."""

    drop_mult = 1 - percent_to_multiplier(config.margin_call_drop)
    target_price = last_buy_price * drop_mult
    level_index = state.num_buys  # zero-based: first buy already executed
    multiplier = get_multiplier(config, level_index)
    state.next_grid_levels.append({"price": target_price, "multiplier": multiplier})


def place_first_order(timestamp: Any, price: float, state: SymbolState, config: CryptomineConfig, trades: List[TradeEvent]) -> None:
    """Execute the first buy order when no position is open."""

    order_usdt = min(config.first_buy_usdt, config.capital_per_coin)
    qty = order_usdt / price
    state.position_size = qty
    state.position_cost_usdt = order_usdt
    state.used_capital_usdt = order_usdt
    state.avg_price = price
    state.first_buy_price = price
    state.num_buys = 1
    state.mode = "whole_warehouse"
    state.buy_lots = [{"price": price, "qty": qty}]
    update_tp_price(state, config)
    state.trailing_active = False
    state.trailing_max_price = 0.0
    state.next_grid_levels.clear()
    if state.num_buys <= config.margin_call_limit:
        schedule_next_level(state, config, price)

    trades.append(TradeEvent(timestamp=timestamp, event_type="buy", price=price, quantity=qty, pnl_usdt=0.0))


def handle_take_profit(timestamp: Any, price: float, state: SymbolState, config: CryptomineConfig, trades: List[TradeEvent]) -> float:
    """Check and execute take-profit or trailing logic. Returns realized PnL."""

    realized_pnl = 0.0
    if not state.has_position:
        return realized_pnl

    if config.callback_percent > 0 and (price >= state.tp_price or state.trailing_active):
        if not state.trailing_active:
            state.trailing_active = True
            state.trailing_max_price = price
        else:
            state.trailing_max_price = max(state.trailing_max_price, price)
        callback_drop = state.trailing_max_price * (1 - percent_to_multiplier(config.callback_percent))
        if price <= callback_drop:
            realized_pnl = execute_full_sell(timestamp, price, state, trades, event_type="sell_full")
    elif config.callback_percent == 0 and price >= state.tp_price:
        realized_pnl = execute_full_sell(timestamp, price, state, trades, event_type="sell_full")

    return realized_pnl


def execute_full_sell(timestamp: Any, price: float, state: SymbolState, trades: List[TradeEvent], event_type: str = "sell_full") -> float:
    """Close the entire position and reset the state."""

    proceeds = price * state.position_size
    pnl = proceeds - state.position_cost_usdt
    trades.append(TradeEvent(timestamp=timestamp, event_type=event_type, price=price, quantity=state.position_size, pnl_usdt=pnl))
    state.reset()
    return pnl


def handle_stop_loss(timestamp: Any, price: float, state: SymbolState, config: CryptomineConfig, trades: List[TradeEvent]) -> float:
    if not config.sl_enabled or not state.has_position:
        return 0.0

    sl_trigger = None
    if config.sl_type == "from_initial" and state.first_buy_price > 0:
        sl_trigger = state.first_buy_price * (1 - percent_to_multiplier(config.sl_value_percent))
    elif config.sl_type == "from_avg" and state.avg_price > 0:
        sl_trigger = state.avg_price * (1 - percent_to_multiplier(config.sl_value_percent))

    if sl_trigger is not None and price <= sl_trigger:
        return execute_full_sell(timestamp, price, state, trades, event_type="sl_exit")

    return 0.0


def handle_margin_call(timestamp: Any, price: float, state: SymbolState, config: CryptomineConfig, trades: List[TradeEvent]) -> None:
    if not state.has_position or not state.next_grid_levels:
        return

    next_level = state.next_grid_levels[0]
    if price > next_level["price"]:
        return

    if state.num_buys >= config.margin_call_limit:
        state.next_grid_levels.pop(0)
        return

    multiplier = next_level["multiplier"]
    order_usdt = config.first_buy_usdt * multiplier
    if state.used_capital_usdt + order_usdt > config.capital_per_coin:
        return

    qty = order_usdt / price
    state.position_size += qty
    state.position_cost_usdt += order_usdt
    state.used_capital_usdt += order_usdt
    state.num_buys += 1
    state.buy_lots.append({"price": price, "qty": qty})
    state.avg_price = state.position_cost_usdt / state.position_size
    if state.num_buys > 5:
        state.mode = "sub_warehouse"

    trades.append(TradeEvent(timestamp=timestamp, event_type="buy", price=price, quantity=qty, pnl_usdt=0.0))

    state.next_grid_levels.pop(0)
    if state.num_buys < config.margin_call_limit:
        schedule_next_level(state, config, price)

    update_tp_price(state, config)


def handle_sub_sell(timestamp: Any, price: float, state: SymbolState, config: CryptomineConfig, trades: List[TradeEvent]) -> float:
    """Sell the most recent lot in sub-warehouse mode when profit target is reached."""

    if state.mode != "sub_warehouse" or state.num_buys <= 5 or not state.buy_lots:
        return 0.0

    last_lot = state.buy_lots[-1]
    target_price = last_lot["price"] * (1 + percent_to_multiplier(config.tp_percent))
    if price < target_price:
        return 0.0

    qty = last_lot["qty"]
    lot_cost = last_lot["price"] * qty
    proceeds = price * qty
    profit = proceeds - lot_cost

    # Update position
    state.position_size -= qty
    state.buy_lots.pop()
    state.used_capital_usdt = max(0.0, state.used_capital_usdt - lot_cost)

    if config.auto_merge:
        state.position_cost_usdt -= lot_cost + profit
    else:
        state.position_cost_usdt -= lot_cost
    if state.position_size > 0:
        state.avg_price = state.position_cost_usdt / state.position_size
    else:
        state.avg_price = 0.0

    trades.append(TradeEvent(timestamp=timestamp, event_type="sell_sub", price=price, quantity=qty, pnl_usdt=profit))
    return profit


def compute_drawdown(equity_curve: List[float]) -> float:
    peak = -np.inf
    max_dd = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        dd = 0 if peak == 0 else (peak - value) / peak * 100
        max_dd = max(max_dd, dd)
    return max_dd


def backtest_cryptomine(df: pd.DataFrame, config: CryptomineConfig) -> BacktestResult:
    """Run the Cryptomine strategy on historical OHLCV data."""

    required_cols = {"timestamp", "open", "high", "low", "close", "volume"}
    if not required_cols.issubset(df.columns):
        raise ValueError(f"DataFrame must contain columns: {required_cols}")

    df_sorted = df.sort_values("timestamp")
    state = SymbolState()
    trades: List[TradeEvent] = []
    realized_pnl = 0.0
    equity_curve: List[float] = []
    max_used_capital = 0.0
    full_cycles = 0

    for _, row in df_sorted.iterrows():
        price = float(row["close"])
        timestamp = row["timestamp"]

        if not state.has_position:
            place_first_order(timestamp, price, state, config, trades)
            update_tp_price(state, config)
            max_used_capital = max(max_used_capital, state.used_capital_usdt)
            equity_curve.append(realized_pnl)
            continue

        realized_pnl += handle_take_profit(timestamp, price, state, config, trades)
        if not state.has_position:
            full_cycles += 1
            equity_curve.append(realized_pnl)
            continue

        realized_pnl += handle_stop_loss(timestamp, price, state, config, trades)
        if not state.has_position:
            full_cycles += 1
            equity_curve.append(realized_pnl)
            continue

        realized_pnl += handle_sub_sell(timestamp, price, state, config, trades)
        handle_margin_call(timestamp, price, state, config, trades)

        max_used_capital = max(max_used_capital, state.used_capital_usdt)
        unrealized = state.position_size * (price - state.avg_price)
        equity_curve.append(realized_pnl + unrealized)

    # If position left open, close at last price for reporting
    if state.has_position:
        last_price = float(df_sorted.iloc[-1]["close"])
        realized_pnl += execute_full_sell(df_sorted.iloc[-1]["timestamp"], last_price, state, trades)
        full_cycles += 1
        equity_curve.append(realized_pnl)

    total_pnl_percent = 0.0
    if max_used_capital > 0:
        total_pnl_percent = realized_pnl / max_used_capital * 100

    max_drawdown_percent = compute_drawdown(equity_curve)

    return BacktestResult(
        trades=trades,
        total_pnl_usdt=realized_pnl,
        total_pnl_percent=total_pnl_percent,
        max_drawdown_percent=max_drawdown_percent,
        max_used_capital_usdt=max_used_capital,
        num_full_cycles=full_cycles,
    )


__all__ = [
    "CryptomineConfig",
    "SymbolState",
    "TradeEvent",
    "BacktestResult",
    "backtest_cryptomine",
]
