#!/usr/bin/env python3
"""Bounded local consilium loop for signal-boosted HYPE ie500 DCA.

Research-only. Uses local OHLC/signals, does not place orders, read secrets, or
call network APIs. The goal is to search for parameter sets moving toward
+200% over 121 days while tracking drawdown.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

ROOT = Path(__file__).resolve().parent
SWEEP_DIR = ROOT / "signal_dca_variant_sweep_90d"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SWEEP_DIR))

from tv_hype_ie500_local_backtest import Config, max_drawdown_pct, load_npz  # noqa: E402
from signal_dca_variant_sweep_90d import HOUR_MS, SignalState, VariantEmu, VariantSpec  # noqa: E402


class SignalBoostTpEmu(VariantEmu):
    def base_pct_for_now(self) -> float:
        p = self.spec.params
        normal = float(p["normal_base_pct"])
        fresh = float(p["fresh_base_pct"])
        freshness_ms = int(p["freshness_ms"])
        return fresh if self.signals.recent_or_active(self.current_ts, freshness_ms) else normal

    def tp_params(self, ts: int) -> tuple[float, float]:
        p = self.spec.params
        freshness_ms = int(p["tp_freshness_ms"])
        if self.signals.recent_or_active(ts, freshness_ms):
            return float(p["fresh_tp_percent"]), float(p["fresh_callback_percent"])
        return self.cfg.tp_percent, self.cfg.callback_percent


def iso_ms(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def run_one(arrays: dict[str, np.ndarray], signals: SignalState, spec: VariantSpec, days: int) -> dict[str, Any]:
    cfg = Config()
    cfg.base_order_pct_eq = float(spec.params["normal_base_pct"])
    emu = SignalBoostTpEmu(cfg, signals, spec)
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
    return {
        "variant": spec.variant,
        "params": json.dumps(spec.params, sort_keys=True),
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


def initial_grid() -> list[VariantSpec]:
    specs: list[VariantSpec] = []
    for normal in [6.0, 8.0, 10.0, 12.0]:
        for fresh in [16.0, 20.0, 24.0, 28.0, 32.0]:
            if fresh <= normal:
                continue
            for sig_h in [2, 6, 24, 72]:
                specs.append(
                    VariantSpec(
                        "signal_boost_tp",
                        {
                            "normal_base_pct": normal,
                            "fresh_base_pct": fresh,
                            "freshness_ms": sig_h * HOUR_MS,
                            "tp_freshness_ms": 24 * HOUR_MS,
                            "fresh_tp_percent": 1.2,
                            "fresh_callback_percent": 0.25,
                        },
                        "Lower normal base; signal-recency boosts base sizing; signal-aware TP fixed to current best.",
                    )
                )
    return specs


def random_spec(rng: random.Random) -> VariantSpec:
    normal = rng.choice([4.0, 6.0, 8.0, 10.0, 12.0, 14.0])
    fresh = rng.choice([14.0, 16.0, 18.0, 20.0, 22.0, 24.0, 28.0, 32.0, 36.0])
    if fresh <= normal:
        fresh = normal + rng.choice([4.0, 8.0, 12.0])
    sig_h = rng.choice([0.5, 1, 2, 4, 6, 12, 24, 48, 72])
    tp_h = rng.choice([6, 12, 24, 48, 72])
    tp = rng.choice([0.8, 1.0, 1.2, 1.4, 1.6])
    cb = rng.choice([0.15, 0.20, 0.25, 0.35])
    return VariantSpec(
        "signal_boost_tp",
        {
            "normal_base_pct": normal,
            "fresh_base_pct": fresh,
            "freshness_ms": int(sig_h * HOUR_MS),
            "tp_freshness_ms": int(tp_h * HOUR_MS),
            "fresh_tp_percent": tp,
            "fresh_callback_percent": cb,
        },
        "Random consilium candidate: signal sizing boost plus signal-aware TP.",
    )


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def write_report(out_dir: Path, rows: list[dict[str, Any]], target_net_pct: float) -> None:
    rows_sorted = sorted(rows, key=lambda r: float(r["net_pct"]), reverse=True)
    best = rows_sorted[0]
    lines = [
        "# Signal Boost Consilium Loop 121d",
        "",
        "Research-only local loop. No live orders, no secrets, no network.",
        "",
        f"- Goal: `{target_net_pct:.2f}%` net over 121 days.",
        f"- Best net: `{float(best['net_pct']):.6f}%`",
        f"- Best max DD: `{float(best['max_dd_pct']):.6f}%`",
        f"- Best params: `{best['params']}`",
        "",
        "## Top 20",
        "",
        "| rank | net pct | max DD pct | orders | first | dca | full TP | params |",
        "|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, row in enumerate(rows_sorted[:20], start=1):
        lines.append(
            f"| {i} | {float(row['net_pct']):.6f} | {float(row['max_dd_pct']):.6f} | "
            f"{int(row['orders'])} | {int(row['first_buys'])} | {int(row['dca_buys'])} | "
            f"{int(row['full_tp_closes'])} | `{row['params']}` |"
        )
    out_dir.joinpath("REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=r"C:\python_scripts\top_1_dev_veronica\obw_platform\meta_strategies\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz")
    ap.add_argument("--signals", default=str(ROOT / "signal_chart_artifact" / "signal_events.csv"))
    ap.add_argument("--out-dir", default=str(ROOT / "signal_boost_consilium_121d"))
    ap.add_argument("--days", type=int, default=121)
    ap.add_argument("--target-net-pct", type=float, default=200.0)
    ap.add_argument("--max-waves", type=int, default=6)
    ap.add_argument("--wave-candidates", type=int, default=24)
    ap.add_argument("--seed", type=int, default=20260525)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    arrays = load_npz(Path(args.npz))
    signals = SignalState(Path(args.signals))
    rng = random.Random(args.seed)
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    candidates = initial_grid()
    status_path = out_dir / "STATUS.json"

    def evaluate(spec: VariantSpec, wave: int) -> None:
        key = json.dumps(spec.params, sort_keys=True)
        if key in seen:
            return
        seen.add(key)
        row = run_one(arrays, signals, spec, args.days)
        row["wave"] = wave
        rows.append(row)
        best = max(rows, key=lambda r: float(r["net_pct"]))
        write_csv(out_dir / "summary.csv", rows)
        (out_dir / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
        status_path.write_text(
            json.dumps(
                {
                    "updated_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "rows": len(rows),
                    "wave": wave,
                    "target_net_pct": args.target_net_pct,
                    "goal_hit": float(best["net_pct"]) >= args.target_net_pct,
                    "best_net_pct": best["net_pct"],
                    "best_max_dd_pct": best["max_dd_pct"],
                    "best_params": best["params"],
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        write_report(out_dir, rows, args.target_net_pct)

    for spec in candidates:
        evaluate(spec, 0)
        if max(rows, key=lambda r: float(r["net_pct"]))["net_pct"] >= args.target_net_pct:
            print(status_path.read_text(encoding="utf-8"))
            return
    for wave in range(1, args.max_waves + 1):
        for _ in range(args.wave_candidates):
            evaluate(random_spec(rng), wave)
            if max(rows, key=lambda r: float(r["net_pct"]))["net_pct"] >= args.target_net_pct:
                print(status_path.read_text(encoding="utf-8"))
                return
    print(status_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
