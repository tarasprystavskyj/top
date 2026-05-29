#!/usr/bin/env python3
"""Local parity backtest for the TradingView HYPE ie500 Pine strategy copy.

Research-only. Reads local OHLC NPZ and does not place orders or use secrets.
This is a close Python emulation of the Pine strategy defaults, not the
TradingView broker emulator itself.
"""
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


@dataclass
class Config:
    name: str = "C - LONG - MA driven HYPE ie500 fixed champion copy defaults"
    initial_capital: float = 500.0
    commission_pct: float = 0.05
    use_strategy_equity_for_sizing: bool = True
    base_order_pct_eq: float = 16.0
    equity_for_sizing_usdt: float = 500.0
    tp_percent: float = 0.52
    callback_percent: float = 0.10
    margin_call_limit: int = 4
    linear_drop_percent: float = 1.20
    auto_merge: bool = False
    sub_sell_tp_percent: float = 0.65
    require_close_above_full_tp: bool = True
    sub_sell_close_confirm_mode: str = "breakeven"
    require_close_below_dca_level: bool = True
    block_dca_on_tp_touch: bool = False
    drops_pct: tuple[float, ...] = (0.25, 0.35, 0.55, 3.00, 4.00)
    multipliers: tuple[float, ...] = (1.0, 1.5, 2.75, 1.5)
    max_fills_per_bar: int = 2
    max_sub_sells_per_bar: int = 5
    use_high_low_touch: bool = True
    max_orders_per_3min: int = 6
    liquidation_line_pct: float = -100.0
    use_trend_adaptive_sizing: bool = False
    hard_breakeven_deleverage_pct: float = 25.0
    max_position_cost_pct: float = 100.0
    hard_max_total_dd_pct: float = 50.0
    tv_max_drawdown_stop_pct: float = 50.0
    anchor_year: int = 2026


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def load_npz(path: Path) -> dict[str, np.ndarray]:
    z = np.load(path, allow_pickle=True)
    return {
        "t": z["timestamp_s"].astype(np.int64) * 1000,
        "open": z["open"].astype(float),
        "high": z["high"].astype(float),
        "low": z["low"].astype(float),
        "close": z["close"].astype(float),
    }


class PineLongDcaEmu:
    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.cash_equity = cfg.initial_capital
        self.realized_pnl = 0.0
        self.commission_paid = 0.0
        self.pos_size = 0.0
        self.pos_cost = 0.0
        self.avg_price: float | None = None
        self.num_buys = 0
        self.last_fill_price: float | None = None
        self.next_level_price: float | None = None
        self.trailing_active = False
        self.trailing_max: float | None = None
        self.cycle_base_qty: float | None = None
        self.reset_cycle = False
        self.risk_stopped = False
        self.cycle_ss_counter = 0
        self.lot_counter = 0
        self.lots: list[dict[str, Any]] = []
        self.orders_this_bar = 0
        self.recent_bar_fills: list[int] = []
        self.max_orders_per_bar = 0
        self.max_orders_per_window = 0
        self.full_tp_closes = 0
        self.sub_sells = 0
        self.dca_buys = 0
        self.first_buys = 0
        self.restart_buys = 0
        self.hard_dd_stops = 0
        self.min_total_pnl = 0.0
        self.min_total_pnl_pct = 0.0
        self.equity_curve: list[dict[str, Any]] = []
        self.trades: list[dict[str, Any]] = []

    def unrealized(self, close: float) -> float:
        return sum(l["qty"] * (close - l["price"]) for l in self.lots)

    def equity(self, close: float) -> float:
        return self.cash_equity + self.unrealized(close)

    def equity_for_sizing(self, close: float) -> float:
        if self.cfg.use_strategy_equity_for_sizing:
            return max(self.equity(close), 0.0)
        return self.cfg.equity_for_sizing_usdt

    def total_pnl(self, close: float) -> float:
        return self.realized_pnl + self.unrealized(close)

    def total_pnl_pct(self, close: float) -> float:
        eq = self.equity_for_sizing(close)
        return (self.total_pnl(close) / eq) * 100.0 if eq > 0 else 0.0

    def recent_orders(self) -> int:
        return sum(self.recent_bar_fills) + self.orders_this_bar

    def can_place(self, allow_bar: bool) -> bool:
        return allow_bar and not self.risk_stopped and self.recent_orders() < self.cfg.max_orders_per_3min

    def drop_for_next_level(self) -> float:
        nb = self.num_buys + 1
        if 2 <= nb <= 6:
            return self.cfg.drops_pct[nb - 2]
        return self.cfg.linear_drop_percent

    def mult_for_next_level(self) -> float:
        nb = self.num_buys + 1
        if 2 <= nb <= 5:
            return self.cfg.multipliers[nb - 2]
        return 1.0

    def next_level(self, last_fill: float) -> float:
        return last_fill * (1.0 - self.drop_for_next_level() / 100.0)

    def max_cost(self, close: float) -> float:
        return self.equity_for_sizing(close) * self.cfg.max_position_cost_pct / 100.0

    def base_qty(self, close: float) -> float:
        return (self.equity_for_sizing(close) * self.cfg.base_order_pct_eq / 100.0) / close

    def charge_commission(self, notional: float) -> None:
        fee = notional * self.cfg.commission_pct / 100.0
        self.commission_paid += fee
        self.cash_equity -= fee

    def buy(self, *, ts: int, close: float, kind: str, qty: float, tag: str) -> None:
        notional = qty * close
        self.charge_commission(notional)
        self.lot_counter += 1
        self.lots.append({"id": self.lot_counter, "qty": qty, "price": close, "tag": tag, "usdt": notional})
        self.pos_size += qty
        self.pos_cost += notional
        self.avg_price = self.pos_cost / self.pos_size if self.pos_size > 0 else None
        self.num_buys += 1
        self.last_fill_price = close if kind in {"FIRST", "RESTART"} else self.last_fill_price
        if kind == "DCA":
            self.dca_buys += 1
        elif kind == "FIRST":
            self.first_buys += 1
        elif kind == "RESTART":
            self.restart_buys += 1
        self.orders_this_bar += 1
        self.max_orders_per_bar = max(self.max_orders_per_bar, self.orders_this_bar)
        self.trades.append({"ts": ts, "iso": iso_ms(ts), "event": kind, "qty": qty, "price": close, "notional": notional})

    def sell_lot(self, *, ts: int, close: float, idx: int, kind: str) -> None:
        lot = self.lots.pop(idx)
        qty = lot["qty"]
        entry = lot["price"]
        sell_notional = qty * close
        pnl = qty * (close - entry)
        self.charge_commission(sell_notional)
        self.realized_pnl += pnl
        self.cash_equity += pnl
        self.pos_size -= qty
        self.pos_cost -= qty * entry
        if self.cfg.auto_merge:
            self.pos_cost -= pnl
        self.avg_price = self.pos_cost / self.pos_size if self.pos_size > 0 else None
        self.num_buys = max(self.num_buys - 1, 0)
        self.orders_this_bar += 1
        self.max_orders_per_bar = max(self.max_orders_per_bar, self.orders_this_bar)
        self.sub_sells += 1 if kind == "SUB_SELL" else 0
        self.trades.append({"ts": ts, "iso": iso_ms(ts), "event": kind, "qty": qty, "price": close, "pnl": pnl})

    def close_all(self, *, ts: int, close: float, kind: str) -> None:
        qty = self.pos_size
        pnl = self.unrealized(close)
        sell_notional = qty * close
        if qty > 0:
            self.charge_commission(sell_notional)
        self.realized_pnl += pnl
        self.cash_equity += pnl
        self.trades.append({"ts": ts, "iso": iso_ms(ts), "event": kind, "qty": qty, "price": close, "pnl": pnl})
        self.pos_size = 0.0
        self.pos_cost = 0.0
        self.avg_price = None
        self.num_buys = 0
        self.last_fill_price = None
        self.next_level_price = None
        self.trailing_active = False
        self.trailing_max = None
        self.cycle_base_qty = None
        self.lots.clear()
        self.orders_this_bar += 1
        self.max_orders_per_bar = max(self.max_orders_per_bar, self.orders_this_bar)
        if kind.startswith("FULL_CLOSE"):
            self.full_tp_closes += 1

    def step(self, *, idx: int, ts: int, o: float, h: float, l: float, c: float, bar_ms: int, history_window: int) -> None:
        if idx > 0:
            self.recent_bar_fills.append(self.orders_this_bar)
            if len(self.recent_bar_fills) > history_window:
                self.recent_bar_fills.pop(0)
            self.orders_this_bar = 0
        self.max_orders_per_window = max(self.max_orders_per_window, self.recent_orders())

        anchor = int(datetime(self.cfg.anchor_year, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        bars_from_anchor = (ts - anchor) // bar_ms
        allow_bar = ts >= anchor and bars_from_anchor % 2 == 0
        trigger_high = h if self.cfg.use_high_low_touch else max(o, c)
        trigger_low = l if self.cfg.use_high_low_touch else min(o, c)
        restarted = False

        if self.reset_cycle:
            self.pos_size = self.pos_cost = 0.0
            self.avg_price = None
            self.num_buys = 0
            self.last_fill_price = None
            self.next_level_price = None
            self.trailing_active = False
            self.trailing_max = None
            self.cycle_base_qty = None
            self.lots.clear()
            self.reset_cycle = False
            if self.can_place(allow_bar):
                qty = self.base_qty(c)
                if qty * c <= self.max_cost(c):
                    self.cycle_base_qty = qty
                    self.buy(ts=ts, close=c, kind="RESTART", qty=qty, tag="dl1")
                    self.last_fill_price = c
                    self.next_level_price = self.next_level(c)
                    restarted = True

        if self.pos_size == 0 and not self.reset_cycle and not restarted:
            if self.can_place(allow_bar):
                qty = self.base_qty(c)
                if qty * c <= self.max_cost(c):
                    self.cycle_base_qty = qty
                    self.buy(ts=ts, close=c, kind="FIRST", qty=qty, tag="dl1")
                    self.last_fill_price = c
                    self.next_level_price = self.next_level(c)

        tp_price = self.avg_price * (1.0 + self.cfg.tp_percent / 100.0) if self.avg_price else None
        tp_touch = tp_price is not None and trigger_high >= tp_price
        full_tp_close_ok = tp_price is not None and (not self.cfg.require_close_above_full_tp or c >= tp_price)
        tp_close_confirmed = tp_touch and full_tp_close_ok
        tp_blocks_dca = tp_touch if self.cfg.block_dca_on_tp_touch else tp_close_confirmed

        if tp_touch and tp_price is not None:
            if self.cfg.callback_percent > 0:
                self.trailing_active = True
                self.trailing_max = trigger_high if self.trailing_max is None else max(self.trailing_max, trigger_high)
                trail_stop = self.trailing_max * (1.0 - self.cfg.callback_percent / 100.0)
                if tp_close_confirmed and c <= trail_stop and self.can_place(allow_bar):
                    self.close_all(ts=ts, close=c, kind="FULL_CLOSE_TRAIL")
                    self.reset_cycle = True
            elif tp_close_confirmed and self.can_place(allow_bar):
                self.close_all(ts=ts, close=c, kind="FULL_CLOSE")
                self.reset_cycle = True

        if not tp_blocks_dca and self.pos_size > 0 and not restarted:
            fills = 0
            while (
                self.num_buys < self.cfg.margin_call_limit
                and fills < self.cfg.max_fills_per_bar
                and self.next_level_price is not None
                and trigger_low <= self.next_level_price
                and (not self.cfg.require_close_below_dca_level or c <= self.next_level_price)
                and self.can_place(allow_bar)
            ):
                mult = self.mult_for_next_level()
                if self.cycle_base_qty is None:
                    self.cycle_base_qty = self.base_qty(c)
                qty = self.cycle_base_qty * mult
                if self.pos_cost + qty * c > self.max_cost(c):
                    break
                trigger_level = self.next_level_price
                self.buy(ts=ts, close=c, kind="DCA", qty=qty, tag=f"dl{self.num_buys + 1}")
                self.last_fill_price = trigger_level
                self.next_level_price = self.next_level(trigger_level)
                self.trailing_active = False
                self.trailing_max = None
                fills += 1

        if not tp_blocks_dca and self.pos_size > 0 and self.num_buys > 5 and not restarted:
            sold = 0
            any_sold = False
            while sold < self.cfg.max_sub_sells_per_bar and self.can_place(allow_bar) and self.lots:
                lot = self.lots[-1]
                entry = lot["price"]
                current_exposure_pct = ((self.pos_size * c) / self.equity_for_sizing(c)) * 100.0 if self.equity_for_sizing(c) > 0 else 0.0
                force_be = current_exposure_pct > self.cfg.hard_breakeven_deleverage_pct
                last_lot_tp = entry * (1.0 + self.cfg.sub_sell_tp_percent / 100.0)
                sell_touch_level = entry if force_be else last_lot_tp
                sub_sell_touch = trigger_high >= sell_touch_level
                if force_be:
                    sub_sell_close_ok = c >= entry
                elif self.cfg.sub_sell_close_confirm_mode == "off":
                    sub_sell_close_ok = True
                elif self.cfg.sub_sell_close_confirm_mode == "breakeven":
                    sub_sell_close_ok = c >= entry
                else:
                    sub_sell_close_ok = c >= last_lot_tp
                if not (sub_sell_touch and sub_sell_close_ok):
                    break
                any_sold = True
                self.sell_lot(ts=ts, close=c, idx=len(self.lots) - 1, kind="SUB_SELL")
                sold += 1
                if not self.lots or self.pos_size <= 0:
                    self.reset_cycle = True
                    break
            if any_sold and not self.reset_cycle and self.lots:
                self.last_fill_price = self.lots[-1]["price"]
                self.next_level_price = self.next_level(self.last_fill_price)

        total_pnl_pct = self.total_pnl_pct(c)
        if not self.risk_stopped and total_pnl_pct <= -self.cfg.hard_max_total_dd_pct:
            if self.pos_size > 0:
                self.close_all(ts=ts, close=c, kind="HARD_DD_STOP")
                self.hard_dd_stops += 1
            self.reset_cycle = False
            self.risk_stopped = True

        total_pnl = self.total_pnl(c)
        self.min_total_pnl = min(self.min_total_pnl, total_pnl)
        self.min_total_pnl_pct = min(self.min_total_pnl_pct, self.total_pnl_pct(c))
        self.equity_curve.append(
            {
                "ts": ts,
                "iso": iso_ms(ts),
                "close": c,
                "equity": self.equity(c),
                "cash_equity": self.cash_equity,
                "realized_pnl": self.realized_pnl,
                "unrealized_pnl": self.unrealized(c),
                "pos_size": self.pos_size,
                "pos_cost": self.pos_cost,
                "num_buys": self.num_buys,
            }
        )


def max_drawdown_pct(values: list[float]) -> float:
    peak = values[0] if values else 0.0
    max_dd = 0.0
    for v in values:
        peak = max(peak, v)
        if peak > 0:
            max_dd = min(max_dd, (v / peak - 1.0) * 100.0)
    return max_dd


def run(args: argparse.Namespace) -> dict[str, Any]:
    cfg = Config()
    arrays = load_npz(Path(args.npz))
    end_ms = int(arrays["t"][-1])
    start_ms = end_ms - int(args.days) * 24 * 60 * 60 * 1000
    mask = arrays["t"] >= start_ms
    idxs = np.nonzero(mask)[0]
    if len(idxs) == 0:
        raise SystemExit("no bars in requested window")
    i0 = int(idxs[0])
    i1 = int(idxs[-1]) + 1
    emu = PineLongDcaEmu(cfg)
    bar_ms = int(np.median(np.diff(arrays["t"][i0 : min(i0 + 1000, i1)])))
    history_window = max(int(np.ceil(180_000 / bar_ms)) - 1, 0)
    for j, i in enumerate(range(i0, i1)):
        emu.step(
            idx=j,
            ts=int(arrays["t"][i]),
            o=float(arrays["open"][i]),
            h=float(arrays["high"][i]),
            l=float(arrays["low"][i]),
            c=float(arrays["close"][i]),
            bar_ms=bar_ms,
            history_window=history_window,
        )
    equity_values = [r["equity"] for r in emu.equity_curve]
    summary = {
        "config": asdict(cfg),
        "window_days": args.days,
        "bars": len(emu.equity_curve),
        "start_iso": emu.equity_curve[0]["iso"],
        "end_iso": emu.equity_curve[-1]["iso"],
        "start_equity": cfg.initial_capital,
        "end_equity": equity_values[-1],
        "net_pct": (equity_values[-1] / cfg.initial_capital - 1.0) * 100.0,
        "max_equity": max(equity_values),
        "max_drawdown_pct": max_drawdown_pct(equity_values),
        "min_total_pnl_pct": emu.min_total_pnl_pct,
        "realized_pnl": emu.realized_pnl,
        "unrealized_pnl": emu.equity_curve[-1]["unrealized_pnl"],
        "commission_paid": emu.commission_paid,
        "orders": len(emu.trades),
        "first_buys": emu.first_buys,
        "restart_buys": emu.restart_buys,
        "dca_buys": emu.dca_buys,
        "full_tp_closes": emu.full_tp_closes,
        "sub_sells": emu.sub_sells,
        "hard_dd_stops": emu.hard_dd_stops,
        "max_orders_per_bar": emu.max_orders_per_bar,
        "max_orders_per_3min": emu.max_orders_per_window,
        "open_position_qty": emu.pos_size,
        "open_position_cost": emu.pos_cost,
        "open_position_avg": emu.avg_price,
    }
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "config.json").write_text(json.dumps(asdict(cfg), indent=2), encoding="utf-8")
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    with (out_dir / "equity_curve.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(emu.equity_curve[0].keys()))
        writer.writeheader()
        writer.writerows(emu.equity_curve)
    with (out_dir / "orders.csv").open("w", newline="", encoding="utf-8") as fp:
        fields = sorted({k for row in emu.trades for k in row.keys()})
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        writer.writerows(emu.trades)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", required=True)
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out-dir", required=True)
    args = ap.parse_args()
    summary = run(args)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
