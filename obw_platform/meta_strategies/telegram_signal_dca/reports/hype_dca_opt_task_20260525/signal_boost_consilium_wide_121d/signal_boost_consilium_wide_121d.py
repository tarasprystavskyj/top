#!/usr/bin/env python3
"""Wide bounded research loop for signal-boosted HYPE ie500 DCA.

Research-only. Reads local OHLC/signals, does not place orders, read secrets,
or call network APIs. Outputs stay in this folder by default.
"""
from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

OUT_DIR = Path(__file__).resolve().parent
TASK_ROOT = OUT_DIR.parent
SWEEP_DIR = TASK_ROOT / "signal_dca_variant_sweep_90d"
sys.path.insert(0, str(TASK_ROOT))
sys.path.insert(0, str(SWEEP_DIR))

from tv_hype_ie500_local_backtest import Config, load_npz, max_drawdown_pct  # noqa: E402
from signal_dca_variant_sweep_90d import HOUR_MS, SignalState, VariantEmu, VariantSpec  # noqa: E402


DEFAULT_NPZ = (
    r"C:\python_scripts\top_1_dev_veronica\obw_platform\meta_strategies"
    r"\telegram_signal_dca\reports\binance_430051_hype_v21_loop_20260523"
    r"\binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz"
)
DEFAULT_SIGNALS = TASK_ROOT / "signal_chart_artifact" / "signal_events.csv"
TARGET_NET_PCT = 200.0

DCA_PROFILES: dict[str, tuple[tuple[float, ...], tuple[float, ...]]] = {
    "default": ((0.25, 0.35, 0.55, 3.00, 4.00), (1.0, 1.5, 2.75, 1.5)),
    "early_wide": ((0.35, 0.50, 0.80, 3.00, 4.00), (1.0, 1.45, 2.50, 1.35)),
    "moderate": ((0.30, 0.45, 0.70, 2.50, 3.50), (1.0, 1.35, 2.20, 1.25)),
    "late_heavy": ((0.45, 0.70, 1.05, 2.80, 4.00), (0.9, 1.35, 2.80, 1.6)),
    "shallow_fast": ((0.20, 0.30, 0.45, 2.20, 3.20), (1.0, 1.65, 2.90, 1.6)),
}


class WideSignalBoostEmu(VariantEmu):
    def base_pct_for_now(self) -> float:
        p = self.spec.params
        normal = float(p["normal_base_pct"])
        fresh = float(p["fresh_base_pct"])
        freshness_ms = int(p["freshness_ms"])
        return fresh if self.signals.recent_or_active(self.current_ts, freshness_ms) else normal

    def tp_params(self, ts: int) -> tuple[float, float]:
        p = self.spec.params
        if self.signals.recent_or_active(ts, int(p["tp_freshness_ms"])):
            return float(p["fresh_tp_percent"]), float(p["fresh_callback_percent"])
        return self.cfg.tp_percent, self.cfg.callback_percent


def iso_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def decode_params(row: dict[str, Any]) -> dict[str, Any]:
    return json.loads(str(row["params"]))


def make_cfg(params: dict[str, Any]) -> Config:
    cfg = Config()
    cfg.base_order_pct_eq = float(params["normal_base_pct"])
    cfg.max_position_cost_pct = float(params.get("max_position_cost_pct", cfg.max_position_cost_pct))
    profile = str(params.get("dca_profile", "default"))
    drops, multipliers = DCA_PROFILES[profile]
    cfg.drops_pct = drops
    cfg.multipliers = multipliers
    return cfg


def run_one(arrays: dict[str, np.ndarray], signals: SignalState, spec: VariantSpec, days: int) -> dict[str, Any]:
    cfg = make_cfg(spec.params)
    emu = WideSignalBoostEmu(cfg, signals, spec)
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
    params_json = json.dumps(spec.params, sort_keys=True)
    return {
        "variant": spec.variant,
        "params": params_json,
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


def spec(params: dict[str, Any], notes: str) -> VariantSpec:
    return VariantSpec("signal_boost_tp_wide", params, notes)


def focused_specs() -> list[VariantSpec]:
    out: list[VariantSpec] = []
    champion = {
        "normal_base_pct": 10.0,
        "fresh_base_pct": 28.0,
        "freshness_ms": 72 * HOUR_MS,
        "tp_freshness_ms": 12 * HOUR_MS,
        "fresh_tp_percent": 1.6,
        "fresh_callback_percent": 0.35,
        "max_position_cost_pct": 100.0,
        "dca_profile": "default",
    }
    out.append(spec(champion, "Prior signal_boost_consilium_121d champion control row."))

    for tp in [1.4, 1.6, 1.8, 2.0, 2.2]:
        for cb in [0.25, 0.35, 0.45, 0.55]:
            out.append(spec({**champion, "fresh_tp_percent": tp, "fresh_callback_percent": cb}, "Champion neighborhood: extended TP/callback."))
    for normal in [4.0, 6.0, 8.0, 10.0, 12.0, 14.0]:
        for fresh in [24.0, 28.0, 32.0, 36.0, 40.0]:
            if fresh <= normal:
                continue
            out.append(spec({**champion, "normal_base_pct": normal, "fresh_base_pct": fresh}, "Champion neighborhood: base sizing."))
    for sig_h in [12, 24, 48, 72, 120, 168]:
        for tp_h in [6, 12, 24, 48, 72, 120, 168]:
            out.append(
                spec(
                    {**champion, "freshness_ms": sig_h * HOUR_MS, "tp_freshness_ms": tp_h * HOUR_MS},
                    "Champion neighborhood: signal and TP freshness windows.",
                )
            )
    for cap in [70.0, 80.0, 90.0, 100.0, 110.0, 120.0]:
        out.append(spec({**champion, "max_position_cost_pct": cap}, "Champion neighborhood: max position cost cap."))
    for profile in DCA_PROFILES:
        out.append(spec({**champion, "dca_profile": profile}, "Champion neighborhood: DCA drop/multiplier profile."))
    return out


def random_spec(rng: random.Random) -> VariantSpec:
    normal = rng.choice([4.0, 6.0, 8.0, 10.0, 12.0, 14.0])
    fresh = rng.choice([24.0, 26.0, 28.0, 30.0, 32.0, 34.0, 36.0, 40.0])
    if fresh <= normal:
        fresh = normal + rng.choice([8.0, 12.0, 16.0])
    params = {
        "normal_base_pct": normal,
        "fresh_base_pct": fresh,
        "freshness_ms": rng.choice([12, 24, 36, 48, 72, 96, 120, 168]) * HOUR_MS,
        "tp_freshness_ms": rng.choice([6, 12, 24, 36, 48, 72, 120, 168]) * HOUR_MS,
        "fresh_tp_percent": rng.choice([1.0, 1.2, 1.4, 1.6, 1.8, 2.0, 2.2, 2.4]),
        "fresh_callback_percent": rng.choice([0.15, 0.20, 0.25, 0.35, 0.45, 0.55, 0.70]),
        "max_position_cost_pct": rng.choice([80.0, 90.0, 100.0, 110.0, 120.0]),
        "dca_profile": rng.choice(list(DCA_PROFILES)),
    }
    return spec(params, "Random wide candidate: sizing, TP, cost cap, and DCA profile.")


def unique_specs(specs: list[VariantSpec], max_candidates: int) -> list[VariantSpec]:
    seen: set[str] = set()
    out: list[VariantSpec] = []
    for item in specs:
        key = json.dumps(item.params, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        out.append(item)
        if len(out) >= max_candidates:
            break
    return out


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def dd_filtered(rows: list[dict[str, Any]], min_net: float | None = None) -> dict[str, Any] | None:
    candidates = rows
    if min_net is not None:
        candidates = [r for r in rows if float(r["net_pct"]) >= min_net]
    if not candidates:
        return None
    return max(candidates, key=lambda r: (float(r["max_dd_pct"]), float(r["net_pct"])))


def write_status(path: Path, rows: list[dict[str, Any]], wave: int, target_net_pct: float, done: bool = False) -> None:
    best = max(rows, key=lambda r: float(r["net_pct"]))
    best_dd_any = dd_filtered(rows)
    best_dd_profitable = dd_filtered(rows, min_net=100.0)
    payload = {
        "updated_utc": iso_now(),
        "done": done,
        "rows": len(rows),
        "wave": wave,
        "target_net_pct": target_net_pct,
        "goal_hit": float(best["net_pct"]) >= target_net_pct,
        "best_net_pct": best["net_pct"],
        "best_max_dd_pct": best["max_dd_pct"],
        "best_params": best["params"],
        "best_dd_any_net_pct": best_dd_any["net_pct"] if best_dd_any else None,
        "best_dd_any_max_dd_pct": best_dd_any["max_dd_pct"] if best_dd_any else None,
        "best_dd_any_params": best_dd_any["params"] if best_dd_any else None,
        "best_dd_net_ge_100_pct": best_dd_profitable["net_pct"] if best_dd_profitable else None,
        "best_dd_net_ge_100_max_dd_pct": best_dd_profitable["max_dd_pct"] if best_dd_profitable else None,
        "best_dd_net_ge_100_params": best_dd_profitable["params"] if best_dd_profitable else None,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def summarize_by_param(rows: list[dict[str, Any]]) -> list[str]:
    notes = [
        "- `normal_base_pct`: baseline cycle size outside signal freshness; lower values reduce exposure in unconfirmed periods but can leave less compounding.",
        "- `fresh_base_pct`: cycle size during fresh/active signal periods; prior rows show it is the main net accelerator and also a DD amplifier once it reaches the high 20s.",
        "- `freshness_ms`: controls how long signal-boosted sizing remains active; longer windows lifted 121d net in prior runs but clustered drawdown near -32%.",
        "- `tp_freshness_ms`: controls how long the alternate TP/trailing regime remains active; prior champion preferred a shorter 12h TP window than its 72h sizing window.",
        "- `fresh_tp_percent`: higher fresh TP captured larger signal moves; 1.6% beat 1.2% in the 121d champion, so this loop extends above 1.6%.",
        "- `fresh_callback_percent`: wider trailing lets winners breathe but can give back more; 0.35% was best previously, so this loop tests 0.25%-0.70%.",
        "- `max_position_cost_pct`: lower caps should reduce underwater exposure and DD, while caps above 100% test whether the 200% target needs more leverage-like allocation.",
        "- `dca_profile`: wider early drops should reduce premature averaging; shallow/heavier profiles test whether faster rescue improves compounding enough to justify DD.",
    ]
    if not rows:
        return notes
    best = max(rows, key=lambda r: float(r["net_pct"]))
    p = decode_params(best)
    notes.append(
        f"- Current best observed mix: normal {p['normal_base_pct']}%, fresh {p['fresh_base_pct']}%, "
        f"sizing freshness {int(p['freshness_ms']) // HOUR_MS}h, TP freshness {int(p['tp_freshness_ms']) // HOUR_MS}h, "
        f"fresh TP {p['fresh_tp_percent']}%, callback {p['fresh_callback_percent']}%, "
        f"cost cap {p.get('max_position_cost_pct', 100.0)}%, DCA `{p.get('dca_profile', 'default')}`."
    )
    return notes


def write_report(out_dir: Path, rows: list[dict[str, Any]], target_net_pct: float) -> None:
    rows_sorted = sorted(rows, key=lambda r: float(r["net_pct"]), reverse=True)
    best = rows_sorted[0]
    best_dd_any = dd_filtered(rows)
    best_dd_profitable = dd_filtered(rows, min_net=100.0)
    lines = [
        "# Signal Boost Consilium Wide Loop 121d",
        "",
        "Research-only local loop. No live orders, no secrets, no network, no private scraping.",
        "",
        f"- Goal: `{target_net_pct:.2f}%` net over 121 days.",
        f"- Rows tested: `{len(rows)}`.",
        f"- Best net: `{float(best['net_pct']):.6f}%`.",
        f"- Best max DD: `{float(best['max_dd_pct']):.6f}%`.",
        f"- Best params: `{best['params']}`.",
        f"- Target hit: `{'yes' if float(best['net_pct']) >= target_net_pct else 'no'}`.",
        "",
        "## Parameter Notes",
        "",
        *summarize_by_param(rows),
        "",
        "## DD-Filtered Views",
        "",
    ]
    if best_dd_any:
        lines.append(
            f"- Lowest drawdown overall: net `{float(best_dd_any['net_pct']):.6f}%`, "
            f"DD `{float(best_dd_any['max_dd_pct']):.6f}%`, params `{best_dd_any['params']}`."
        )
    if best_dd_profitable:
        lines.append(
            f"- Lowest drawdown with net >= 100%: net `{float(best_dd_profitable['net_pct']):.6f}%`, "
            f"DD `{float(best_dd_profitable['max_dd_pct']):.6f}%`, params `{best_dd_profitable['params']}`."
        )
    lines.extend(
        [
            "",
            "## Top 25 By Net",
            "",
            "| rank | net pct | max DD pct | orders | first | dca | full TP | open cost | params |",
            "|---:|---:|---:|---:|---:|---:|---:|---:|---|",
        ]
    )
    for i, row in enumerate(rows_sorted[:25], start=1):
        lines.append(
            f"| {i} | {float(row['net_pct']):.6f} | {float(row['max_dd_pct']):.6f} | "
            f"{int(row['orders'])} | {int(row['first_buys'])} | {int(row['dca_buys'])} | "
            f"{int(row['full_tp_closes'])} | {float(row['open_position_cost']):.6f} | `{row['params']}` |"
        )
    lines.extend(
        [
            "",
            "## Method Limits",
            "",
            "- This is the local Python Pine-style emulator, not exact TradingView broker-emulator parity.",
            "- Binance copy `avgCost` / `avgClosePrice` are not used as fills.",
            "- The signal overlay changes sizing and TP/trailing only; it does not modify production Pine files.",
        ]
    )
    out_dir.joinpath("REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--npz", default=DEFAULT_NPZ)
    ap.add_argument("--signals", default=str(DEFAULT_SIGNALS))
    ap.add_argument("--out-dir", default=str(OUT_DIR))
    ap.add_argument("--days", type=int, default=121)
    ap.add_argument("--target-net-pct", type=float, default=TARGET_NET_PCT)
    ap.add_argument("--max-candidates", type=int, default=240)
    ap.add_argument("--random-candidates", type=int, default=120)
    ap.add_argument("--seed", type=int, default=20260525)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    arrays = load_npz(Path(args.npz))
    signals = SignalState(Path(args.signals))
    rng = random.Random(args.seed)
    candidate_pool = focused_specs() + [random_spec(rng) for _ in range(args.random_candidates)]
    candidates = unique_specs(candidate_pool, args.max_candidates)

    rows: list[dict[str, Any]] = []
    status_path = out_dir / "STATUS.json"
    write_status(status_path, [{"net_pct": -100.0, "max_dd_pct": -100.0, "params": "{}"}], 0, args.target_net_pct)

    for idx, item in enumerate(candidates, start=1):
        row = run_one(arrays, signals, item, args.days)
        row["wave"] = 0 if idx <= len(focused_specs()) else 1
        row["candidate_index"] = idx
        rows.append(row)
        write_status(status_path, rows, int(row["wave"]), args.target_net_pct)
        if idx % 5 == 0 or float(row["net_pct"]) >= args.target_net_pct:
            write_csv(out_dir / "summary.csv", rows)
            (out_dir / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
            write_report(out_dir, rows, args.target_net_pct)
        if float(max(rows, key=lambda r: float(r["net_pct"]))["net_pct"]) >= args.target_net_pct:
            break

    write_csv(out_dir / "summary.csv", rows)
    (out_dir / "summary.json").write_text(json.dumps(rows, indent=2), encoding="utf-8")
    write_report(out_dir, rows, args.target_net_pct)
    write_status(status_path, rows, 1, args.target_net_pct, done=True)
    print(status_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    main()
