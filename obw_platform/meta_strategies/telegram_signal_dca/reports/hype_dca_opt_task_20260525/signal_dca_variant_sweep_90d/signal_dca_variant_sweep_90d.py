#!/usr/bin/env python3
"""Research-only Telegram/Binance-signal overlays for HYPE ie500 DCA.

Runs bounded local 90d sweeps against the copied Python emulator. It reads only
local OHLC/signals and does not place orders, use secrets, or access network.
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tv_hype_ie500_local_backtest import Config, PineLongDcaEmu, iso_ms, load_npz, max_drawdown_pct  # noqa: E402

MINUTE_MS = 60_000
HOUR_MS = 60 * MINUTE_MS


@dataclass(frozen=True)
class VariantSpec:
    variant: str
    params: dict[str, Any]
    notes: str


class SignalState:
    def __init__(self, csv_path: Path) -> None:
        intervals: list[tuple[int, int]] = []
        opens: dict[str, int] = {}
        with csv_path.open("r", newline="", encoding="utf-8") as fp:
            for row in csv.DictReader(fp):
                if row.get("symbol") != "HYPEUSDT" or row.get("side") != "LONG":
                    continue
                trade_no = str(row["trade_no"])
                ts = int(row["event_ms"])
                if row["event"] == "OPEN":
                    opens[trade_no] = ts
                elif row["event"] == "CLOSE" and trade_no in opens:
                    intervals.append((opens[trade_no], ts))
        self.intervals = sorted(intervals)
        self.opens = np.array([o for o, _ in self.intervals], dtype=np.int64)
        self.closes = np.array([c for _, c in self.intervals], dtype=np.int64)

    def is_active(self, ts: int) -> bool:
        i = int(np.searchsorted(self.opens, ts, side="right")) - 1
        return i >= 0 and ts <= int(self.closes[i])

    def last_open_age_ms(self, ts: int) -> int | None:
        i = int(np.searchsorted(self.opens, ts, side="right")) - 1
        if i < 0:
            return None
        return ts - int(self.opens[i])

    def recent_or_active(self, ts: int, freshness_ms: int) -> bool:
        if self.is_active(ts):
            return True
        age = self.last_open_age_ms(ts)
        return age is not None and 0 <= age <= freshness_ms

    def score(self, ts: int, decay_ms: int) -> float:
        if self.is_active(ts):
            return 1.0
        age = self.last_open_age_ms(ts)
        if age is None or age < 0:
            return 0.0
        return math.exp(-age / decay_ms) if decay_ms > 0 else 0.0


class VariantEmu(PineLongDcaEmu):
    def __init__(self, cfg: Config, signals: SignalState, spec: VariantSpec) -> None:
        super().__init__(cfg)
        self.signals = signals
        self.spec = spec
        self.current_ts = 0
        self.current_close = 0.0

    def signal_recent(self, ts: int, fallback_ms: int | None = None) -> bool:
        freshness_ms = int(self.spec.params.get("freshness_ms", fallback_ms or 0))
        return self.signals.recent_or_active(ts, freshness_ms)

    def base_pct_for_now(self) -> float:
        p = self.spec.params
        if self.spec.variant == "sizing_boost":
            normal = float(p["normal_base_pct"])
            fresh = float(p["fresh_base_pct"])
            return fresh if self.signal_recent(self.current_ts) else normal
        if self.spec.variant == "score_blend":
            normal = float(p["normal_base_pct"])
            boost = float(p["boost_pct"])
            cap = float(p["cap_pct"])
            decay_ms = int(p["decay_ms"])
            return min(cap, normal + boost * self.signals.score(self.current_ts, decay_ms))
        return self.cfg.base_order_pct_eq

    def base_qty(self, close: float) -> float:
        return (self.equity_for_sizing(close) * self.base_pct_for_now() / 100.0) / close

    def dca_allowed(self, ts: int, c: float) -> bool:
        if self.spec.variant != "dca_permission":
            return True
        p = self.spec.params
        if self.signal_recent(ts):
            return True
        # Permissive local trend proxy: allow adds when price is not below the
        # previous fill by more than the next scheduled drop level.
        if p.get("use_proxy", False) and self.next_level_price is not None:
            return c >= self.next_level_price
        return False

    def tp_params(self, ts: int) -> tuple[float, float]:
        if self.spec.variant != "signal_aware_tp" or not self.signal_recent(ts):
            return self.cfg.tp_percent, self.cfg.callback_percent
        return float(self.spec.params["fresh_tp_percent"]), float(self.spec.params["fresh_callback_percent"])

    def entry_allowed(self, ts: int) -> bool:
        if self.spec.variant != "entry_gate":
            return True
        return self.signal_recent(ts)

    def step(self, *, idx: int, ts: int, o: float, h: float, l: float, c: float, bar_ms: int, history_window: int) -> None:
        self.current_ts = ts
        self.current_close = c
        if idx > 0:
            self.recent_bar_fills.append(self.orders_this_bar)
            if len(self.recent_bar_fills) > history_window:
                self.recent_bar_fills.pop(0)
            self.orders_this_bar = 0
        self.max_orders_per_window = max(self.max_orders_per_window, self.recent_orders())

        import datetime as _dt

        anchor = int(_dt.datetime(self.cfg.anchor_year, 1, 1, tzinfo=_dt.timezone.utc).timestamp() * 1000)
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
            if self.can_place(allow_bar) and self.entry_allowed(ts):
                qty = self.base_qty(c)
                if qty * c <= self.max_cost(c):
                    self.cycle_base_qty = qty
                    self.buy(ts=ts, close=c, kind="RESTART", qty=qty, tag="dl1")
                    self.last_fill_price = c
                    self.next_level_price = self.next_level(c)
                    restarted = True

        if self.pos_size == 0 and not self.reset_cycle and not restarted:
            if self.can_place(allow_bar) and self.entry_allowed(ts):
                qty = self.base_qty(c)
                if qty * c <= self.max_cost(c):
                    self.cycle_base_qty = qty
                    self.buy(ts=ts, close=c, kind="FIRST", qty=qty, tag="dl1")
                    self.last_fill_price = c
                    self.next_level_price = self.next_level(c)

        tp_percent, callback_percent = self.tp_params(ts)
        tp_price = self.avg_price * (1.0 + tp_percent / 100.0) if self.avg_price else None
        tp_touch = tp_price is not None and trigger_high >= tp_price
        full_tp_close_ok = tp_price is not None and (not self.cfg.require_close_above_full_tp or c >= tp_price)
        tp_close_confirmed = tp_touch and full_tp_close_ok
        tp_blocks_dca = tp_touch if self.cfg.block_dca_on_tp_touch else tp_close_confirmed

        if tp_touch and tp_price is not None:
            if callback_percent > 0:
                self.trailing_active = True
                self.trailing_max = trigger_high if self.trailing_max is None else max(self.trailing_max, trigger_high)
                trail_stop = self.trailing_max * (1.0 - callback_percent / 100.0)
                if tp_close_confirmed and c <= trail_stop and self.can_place(allow_bar):
                    self.close_all(ts=ts, close=c, kind="FULL_CLOSE_TRAIL")
                    self.reset_cycle = True
            elif tp_close_confirmed and self.can_place(allow_bar):
                self.close_all(ts=ts, close=c, kind="FULL_CLOSE")
                self.reset_cycle = True

        if not tp_blocks_dca and self.pos_size > 0 and not restarted and self.dca_allowed(ts, c):
            fills = 0
            while (
                self.num_buys < self.cfg.margin_call_limit
                and fills < self.cfg.max_fills_per_bar
                and self.next_level_price is not None
                and trigger_low <= self.next_level_price
                and (not self.cfg.require_close_below_dca_level or c <= self.next_level_price)
                and self.can_place(allow_bar)
                and self.dca_allowed(ts, c)
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
                eq = self.equity_for_sizing(c)
                current_exposure_pct = ((self.pos_size * c) / eq) * 100.0 if eq > 0 else 0.0
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


def specs() -> list[VariantSpec]:
    out = [VariantSpec("baseline", {}, "Unmodified local Pine-style emulator row.")]
    for h in [0.5, 2, 6, 24]:
        out.append(VariantSpec("entry_gate", {"freshness_ms": int(h * HOUR_MS)}, "New first/restart buy requires active or recent long signal."))
    for normal in [12.0, 16.0]:
        for fresh in [20.0, 24.0]:
            out.append(VariantSpec("sizing_boost", {"freshness_ms": 6 * HOUR_MS, "normal_base_pct": normal, "fresh_base_pct": fresh}, "Base sizing changes by 6h signal recency; DCA always permitted."))
    for decay_h in [2, 6, 24]:
        for cap in [20.0, 24.0]:
            out.append(VariantSpec("score_blend", {"decay_ms": decay_h * HOUR_MS, "normal_base_pct": 12.0, "boost_pct": 12.0, "cap_pct": cap}, "Base pct = normal + boost * exp-decayed signal-open score; active signal score is 1."))
    for h in [2, 6, 24]:
        out.append(VariantSpec("dca_permission", {"freshness_ms": h * HOUR_MS, "use_proxy": False}, "First buy unchanged; DCA adds require active/recent signal only."))
    out.append(VariantSpec("dca_permission", {"freshness_ms": 6 * HOUR_MS, "use_proxy": True}, "First buy unchanged; DCA adds require signal recency or permissive local price proxy."))
    for h in [2, 6, 24]:
        for tp in [0.8, 1.0, 1.2]:
            for cb in [0.15, 0.25]:
                out.append(VariantSpec("signal_aware_tp", {"freshness_ms": h * HOUR_MS, "fresh_tp_percent": tp, "fresh_callback_percent": cb}, "Uses alternate TP/trailing only while signal is active/recent."))
    return out


def run_one(arrays: dict[str, np.ndarray], signals: SignalState, spec: VariantSpec, days: int) -> dict[str, Any]:
    cfg = Config()
    emu = VariantEmu(cfg, signals, spec)
    end_ms = int(arrays["t"][-1])
    start_ms = end_ms - days * 24 * 60 * 60 * 1000
    idxs = np.nonzero(arrays["t"] >= start_ms)[0]
    if len(idxs) == 0:
        raise SystemExit("no bars in requested window")
    i0 = int(idxs[0])
    i1 = int(idxs[-1]) + 1
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
    params = json.dumps(spec.params, sort_keys=True)
    return {
        "variant": spec.variant,
        "params": params,
        "start_iso": emu.equity_curve[0]["iso"],
        "end_iso": emu.equity_curve[-1]["iso"],
        "equity_start": cfg.initial_capital,
        "equity_end": equity_values[-1],
        "net_pct": (equity_values[-1] / cfg.initial_capital - 1.0) * 100.0,
        "max_dd_pct": max_drawdown_pct(equity_values),
        "min_total_pnl_pct": emu.min_total_pnl_pct,
        "orders": len(emu.trades),
        "first_buys": emu.first_buys,
        "restart_buys": emu.restart_buys,
        "dca_buys": emu.dca_buys,
        "full_tp_closes": emu.full_tp_closes,
        "sub_sells": emu.sub_sells,
        "open_position_cost": emu.pos_cost,
        "open_position_qty": emu.pos_size,
        "commission_paid": emu.commission_paid,
        "notes": spec.notes,
    }


def write_report(out_dir: Path, rows: list[dict[str, Any]], args: argparse.Namespace) -> None:
    baseline = rows[0]
    best_net = max(rows, key=lambda r: float(r["net_pct"]))
    qualified = [
        r for r in rows
        if float(r["net_pct"]) > float(baseline["net_pct"]) and float(r["max_dd_pct"]) >= float(baseline["max_dd_pct"])
    ]
    best_qualified = max(qualified, key=lambda r: float(r["net_pct"])) if qualified else None
    top_rows = sorted(rows, key=lambda r: float(r["net_pct"]), reverse=True)[:12]
    lines = [
        "# HYPE ie500 Signal+DCA Variant Sweep 90d",
        "",
        "Research-only local test. No live orders, no secrets, no network, no private scraping.",
        "",
        "## Inputs",
        "",
        f"- OHLC NPZ: `{args.npz}`",
        f"- Signal CSV: `{args.signals}`",
        f"- Window: {baseline['start_iso']} to {baseline['end_iso']}",
        "- Signals: HYPEUSDT LONG OPEN/CLOSE timestamps only; Binance avgCost/avgClosePrice ignored except as CSV metadata.",
        "- Parity note: variants are Python-emulator approximations, not exact TradingView broker-emulator parity.",
        "",
        "## Baseline",
        "",
        f"- Net: {baseline['net_pct']:.6f}% | Max DD: {baseline['max_dd_pct']:.6f}% | Orders: {baseline['orders']}",
        "",
        "## Best Results",
        "",
        f"- Best net overall: `{best_net['variant']}` {best_net['params']} -> net {best_net['net_pct']:.6f}%, max DD {best_net['max_dd_pct']:.6f}%",
    ]
    if best_qualified:
        lines.append(f"- Best beating baseline without worse drawdown: `{best_qualified['variant']}` {best_qualified['params']} -> net {best_qualified['net_pct']:.6f}%, max DD {best_qualified['max_dd_pct']:.6f}%")
    else:
        lines.append("- No variant beat baseline net while also avoiding worse max drawdown.")
    lines.extend(["", "## Top Rows By Net", "", "| rank | variant | params | net pct | max DD pct | orders | first buys | DCA buys | full TP closes | open cost |", "|---:|---|---|---:|---:|---:|---:|---:|---:|---:|"])
    for rank, row in enumerate(top_rows, 1):
        lines.append(
            f"| {rank} | {row['variant']} | `{row['params']}` | {row['net_pct']:.6f} | {row['max_dd_pct']:.6f} | {row['orders']} | {row['first_buys']} | {row['dca_buys']} | {row['full_tp_closes']} | {row['open_position_cost']:.6f} |"
        )
    lines.extend([
        "",
        "## Full Summary",
        "",
        "| variant | params | equity start | equity end | net pct | max DD pct | min total PnL pct | orders | first buys | DCA buys | full TP closes | open position cost | notes |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ])
    for row in rows:
        lines.append(
            f"| {row['variant']} | `{row['params']}` | {row['equity_start']:.6f} | {row['equity_end']:.6f} | {row['net_pct']:.6f} | {row['max_dd_pct']:.6f} | {row['min_total_pnl_pct']:.6f} | {row['orders']} | {row['first_buys']} | {row['dca_buys']} | {row['full_tp_closes']} | {row['open_position_cost']:.6f} | {row['notes']} |"
        )
    lines.extend([
        "",
        "## Variant Limitations",
        "",
        "- `entry_gate`: gates only new first/restart cycles; existing cycles still manage TP/DCA normally.",
        "- `sizing_boost`: sets the per-cycle base quantity from signal recency at cycle start; later DCA uses the cycle base, matching the emulator's existing cycle-base behavior.",
        "- `score_blend`: uses an exponential decay from the latest signal OPEN, with active signals forced to score 1.0.",
        "- `dca_permission`: signal-only rows do not implement a full MA/trend model; the proxy row is a permissive local price check, not TradingView MA parity.",
        "- `signal_aware_tp`: changes TP/trailing only on bars where the signal is active/recent; this is an approximation because TradingView intrabar state is not reproduced.",
    ])
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=r"C:\python_scripts\top_1_dev_veronica\obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz")
    ap.add_argument("--signals", default=str(ROOT / "signal_chart_artifact" / "signal_events.csv"))
    ap.add_argument("--days", type=int, default=90)
    ap.add_argument("--out-dir", default=str(Path(__file__).resolve().parent))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    arrays = load_npz(Path(args.npz))
    signals = SignalState(Path(args.signals))
    rows = [run_one(arrays, signals, spec, args.days) for spec in specs()]

    fields = list(rows[0].keys())
    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (out_dir / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    write_report(out_dir, rows, args)
    print(json.dumps({"rows": len(rows), "out_dir": str(out_dir), "best": max(rows, key=lambda r: r["net_pct"])}, indent=2))


if __name__ == "__main__":
    main()
