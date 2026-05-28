#!/usr/bin/env python3
"""One-wave HYPE consilium for daily-trend DCA overlays.

This is a research runner only. It replays Binance copy-trading closed
positions, keeps the lead side authoritative, and decides only how many DCA
legs are allowed from trend/rebound context known before each entry.
"""
from __future__ import annotations

import argparse
import csv
import copy
import json
import math
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Sequence

import numpy as np
import yaml

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compare_binance_copy_positions_dca import read_positions, simulate_position  # noqa: E402
from telegram_signal_dca_compare import load_v21_policy, max_drawdown, policy_for_capital_mode  # noqa: E402


DEFAULT_REPORT_DIR = (
    Path("obw_platform")
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "binance_430051_hype_v21_loop_20260523"
)
DEFAULT_POSITIONS = DEFAULT_REPORT_DIR / "wave_002" / "position_refresh" / "position_history_normalized.csv"
DEFAULT_NPZ = DEFAULT_REPORT_DIR / "binance_4300516091842181632_hype_universe_1m_20250524_20260524.npz"
DEFAULT_CONFIG = Path("obw_platform") / "configs" / "V21_strict_trend_stable_live_static9p38.yaml"

HYPE_VARIANT_CHANGES: Dict[str, Dict[str, float]] = {
    "baseline": {},
    "long_low_exposure": {
        "strategy_params_long.baseOrderPctEq": 0.9,
        "strategy_params_long.maxLongInvestPct": 1.25,
        "strategy_params_long.drop1": 0.45,
        "strategy_params_long.mult2": 1.1,
    },
}


@dataclass(frozen=True)
class Candidate:
    name: str
    dca_count_when_on: int
    dca_count_when_off: int
    ma_days: int
    slope_days: int
    lookback_days: int
    rebound_min: float
    require_ma_up: bool = True
    require_rebound: bool = True


def parse_dt(raw: str) -> datetime:
    s = raw.replace("Z", "+00:00")
    d = datetime.fromisoformat(s)
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc)


def dt_from_s(ts: int) -> datetime:
    return datetime.fromtimestamp(int(ts), tz=timezone.utc)


def load_hype_rows(npz_path: Path) -> List[Dict[str, float]]:
    z = np.load(npz_path, allow_pickle=True)
    symbols = [str(x).upper() for x in z["symbols"].tolist()]
    if len(symbols) != 1 or not symbols[0].startswith("HYPE/"):
        raise SystemExit(f"Expected a single HYPE symbol in {npz_path}, got {symbols}")
    rows: List[Dict[str, float]] = []
    for ts, o, h, lo, c in zip(z["timestamp_s"], z["open"], z["high"], z["low"], z["close"]):
        rows.append({"t": int(ts) * 1000, "open": float(o), "high": float(h), "low": float(lo), "close": float(c)})
    return rows


def set_dotted(obj: Dict[str, Any], dotted: str, value: Any) -> None:
    cur = obj
    parts = dotted.split(".")
    for key in parts[:-1]:
        cur = cur.setdefault(key, {})
    cur[parts[-1]] = value


def write_variant_config(base_config: Path, variant_name: str, out_dir: Path) -> Path:
    if variant_name not in HYPE_VARIANT_CHANGES:
        raise SystemExit(f"Unknown variant {variant_name!r}; choices: {', '.join(sorted(HYPE_VARIANT_CHANGES))}")
    cfg = yaml.safe_load(base_config.read_text(encoding="utf-8"))
    cfg = copy.deepcopy(cfg)
    cfg.setdefault("research_notes", {})
    cfg["research_notes"]["hype_daily_trend_dca_consilium"] = {
        "paper_backtest_only": True,
        "source_base_config": str(base_config),
        "variant_name": variant_name,
        "variant_changes": HYPE_VARIANT_CHANGES[variant_name],
    }
    for key, value in HYPE_VARIANT_CHANGES[variant_name].items():
        set_dotted(cfg, key, value)
    cfg_dir = out_dir / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    path = cfg_dir / f"{variant_name}.yaml"
    path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")
    return path


def slice_rows(rows: Sequence[Dict[str, float]], start_ms: int, end_ms: int) -> List[Dict[str, float]]:
    return [r for r in rows if start_ms <= int(r["t"]) <= end_ms]


def ms(d: datetime) -> int:
    return int(d.timestamp() * 1000)


def daily_bars(rows: Sequence[Dict[str, float]]) -> List[Dict[str, Any]]:
    by_day: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        day = dt_from_s(int(r["t"]) // 1000).strftime("%Y-%m-%d")
        cur = by_day.get(day)
        if cur is None:
            by_day[day] = {
                "day": day,
                "open": float(r["open"]),
                "high": float(r["high"]),
                "low": float(r["low"]),
                "close": float(r["close"]),
                "last_t": int(r["t"]),
            }
        else:
            cur["high"] = max(float(cur["high"]), float(r["high"]))
            cur["low"] = min(float(cur["low"]), float(r["low"]))
            if int(r["t"]) >= int(cur["last_t"]):
                cur["close"] = float(r["close"])
                cur["last_t"] = int(r["t"])
    return [by_day[k] for k in sorted(by_day)]


def rebound_after_fall(closes: Sequence[float]) -> float:
    """Return current retracement from a prior peak-to-later-trough fall."""
    if len(closes) < 3:
        return 0.0
    best = 0.0
    current = float(closes[-1])
    for peak_i in range(0, len(closes) - 2):
        peak = float(closes[peak_i])
        trough_slice = closes[peak_i + 1 : -1]
        if not trough_slice:
            continue
        trough = float(min(trough_slice))
        drop = peak - trough
        if drop <= 0:
            continue
        best = max(best, (current - trough) / drop)
    return best


def build_daily_states(
    bars: Sequence[Dict[str, Any]],
    *,
    ma_days: int,
    slope_days: int,
    lookback_days: int,
    rebound_min: float,
) -> Dict[str, Dict[str, Any]]:
    closes = [float(b["close"]) for b in bars]
    out: Dict[str, Dict[str, Any]] = {}
    for i, b in enumerate(bars):
        if i + 1 < max(ma_days, slope_days + 1, 3):
            out[str(b["day"])] = {
                "ready": False,
                "risk_on": False,
                "close": closes[i],
                "ma": math.nan,
                "ma_slope_pct": math.nan,
                "rebound_ratio": 0.0,
                "reason": "insufficient daily history",
            }
            continue
        ma_now = sum(closes[i - ma_days + 1 : i + 1]) / ma_days
        prev_ma = sum(closes[i - slope_days - ma_days + 1 : i - slope_days + 1]) / ma_days if i - slope_days - ma_days + 1 >= 0 else ma_now
        slope_pct = 100.0 * (ma_now - prev_ma) / max(abs(prev_ma), 1e-12)
        window = closes[max(0, i - lookback_days + 1) : i + 1]
        rebound = rebound_after_fall(window)
        risk_on = closes[i] > ma_now and slope_pct > 0.0 and rebound >= rebound_min
        reason = "risk_on" if risk_on else "missing "
        if not risk_on:
            missing = []
            if closes[i] <= ma_now:
                missing.append("close<=ma")
            if slope_pct <= 0:
                missing.append("ma_slope<=0")
            if rebound < rebound_min:
                missing.append("rebound<min")
            reason = ",".join(missing)
        out[str(b["day"])] = {
            "ready": True,
            "risk_on": risk_on,
            "close": closes[i],
            "ma": ma_now,
            "ma_slope_pct": slope_pct,
            "rebound_ratio": rebound,
            "reason": reason,
        }
    return out


def state_for_open(states: Mapping[str, Dict[str, Any]], opened: datetime) -> Dict[str, Any]:
    keys = [k for k in states.keys() if k < opened.strftime("%Y-%m-%d")]
    if not keys:
        return {"ready": False, "risk_on": False, "reason": "no completed daily bar before entry"}
    return dict(states[max(keys)])


def summarize(rows: Sequence[Dict[str, Any]], initial_equity: float) -> Dict[str, Any]:
    equity = initial_equity
    curve = [equity]
    mtm_curve = [equity]
    pnl_values = []
    for row in rows:
        mtm_curve.append(equity + min(float(row.get("min_mtm", 0.0)), 0.0))
        pnl = float(row["pnl"])
        pnl_values.append(pnl)
        equity += pnl
        curve.append(equity)
    wins = sum(1 for x in pnl_values if x > 0)
    gross_profit = sum(x for x in pnl_values if x > 0)
    gross_loss = sum(x for x in pnl_values if x < 0)
    return {
        "trades": len(rows),
        "equity_start": initial_equity,
        "equity_end": equity,
        "net_pnl": equity - initial_equity,
        "net_pct": 100.0 * (equity - initial_equity) / max(initial_equity, 1e-12),
        "max_dd_pct": 100.0 * max_drawdown(curve),
        "max_mtm_dd_pct": 100.0 * max_drawdown(mtm_curve),
        "min_trade_mtm_pct_equity": 100.0
        * min((float(r.get("min_mtm", 0.0)) for r in rows), default=0.0)
        / max(initial_equity, 1e-12),
        "win_rate_pct": 100.0 * wins / max(1, len(rows)),
        "pf": gross_profit / abs(gross_loss) if gross_loss < 0 else 0.0,
        "gross_profit": gross_profit,
        "gross_loss": gross_loss,
        "avg_dca_fills": sum(float(r["dca_fills"]) for r in rows) / max(1, len(rows)),
        "avg_notional": sum(float(r["notional"]) for r in rows) / max(1, len(rows)),
        "max_notional": max((float(r["notional"]) for r in rows), default=0.0),
        "min_order_ok": all(str(r.get("min_order_ok", "True")) == "True" for r in rows),
        "risk_on_entries": sum(1 for r in rows if str(r.get("trend_risk_on")) == "True"),
        "ready_entries": sum(1 for r in rows if str(r.get("trend_ready")) == "True"),
    }


def write_csv(path: Path, rows: Sequence[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)


def candidate_set() -> List[Candidate]:
    return [
        Candidate("plain_no_dca", 0, 0, 7, 1, 14, 0.51, require_ma_up=False, require_rebound=False),
        Candidate("dca3_always_current_best", 3, 3, 7, 1, 14, 0.51, require_ma_up=False, require_rebound=False),
        Candidate("ma5_rebound51_dca3_else_plain", 3, 0, 5, 1, 14, 0.51),
        Candidate("ma7_rebound51_dca3_else_plain", 3, 0, 7, 1, 14, 0.51),
        Candidate("ma10_rebound51_dca3_else_plain", 3, 0, 10, 2, 21, 0.51),
        Candidate("ma14_rebound51_dca3_else_plain", 3, 0, 14, 2, 28, 0.51),
        Candidate("ma7_rebound51_dca3_else_dca1", 3, 1, 7, 1, 14, 0.51),
        Candidate("rebound51_only_dca3_else_plain", 3, 0, 7, 1, 14, 0.51, require_ma_up=False, require_rebound=True),
        Candidate("ma7_only_dca3_else_plain", 3, 0, 7, 1, 14, 0.51, require_ma_up=True, require_rebound=False),
    ]


def effective_dca(candidate: Candidate, state: Mapping[str, Any]) -> int:
    ready = bool(state.get("ready"))
    ma_ok = bool(state.get("risk_on"))
    rebound_ok = float(state.get("rebound_ratio", 0.0) or 0.0) >= candidate.rebound_min
    if not ready and (candidate.require_ma_up or candidate.require_rebound):
        return candidate.dca_count_when_off
    if candidate.require_ma_up and candidate.require_rebound:
        return candidate.dca_count_when_on if ma_ok else candidate.dca_count_when_off
    if candidate.require_ma_up:
        close = float(state.get("close", math.nan))
        ma = float(state.get("ma", math.nan))
        slope = float(state.get("ma_slope_pct", math.nan))
        return candidate.dca_count_when_on if close > ma and slope > 0.0 else candidate.dca_count_when_off
    if candidate.require_rebound:
        return candidate.dca_count_when_on if rebound_ok else candidate.dca_count_when_off
    return candidate.dca_count_when_on


def run_candidate(
    candidate: Candidate,
    positions: Sequence[Any],
    all_rows: Sequence[Dict[str, float]],
    base_policy: Dict[str, Any],
    *,
    target_notional: float,
    initial_equity: float,
    fill_mode: str,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    max_count = max(candidate.dca_count_when_on, candidate.dca_count_when_off)
    policies = {
        i: policy_for_capital_mode(base_policy, i, target_notional, "same_max")
        for i in range(max_count + 1)
    }
    bars = daily_bars(all_rows)
    states = build_daily_states(
        bars,
        ma_days=candidate.ma_days,
        slope_days=candidate.slope_days,
        lookback_days=candidate.lookback_days,
        rebound_min=candidate.rebound_min,
    )
    out: List[Dict[str, Any]] = []
    for pos in positions:
        candles = slice_rows(all_rows, ms(pos.opened), ms(pos.closed))
        if not candles:
            continue
        state = state_for_open(states, pos.opened)
        dca_count = effective_dca(candidate, state)
        row = simulate_position(pos, candles, policy=policies[dca_count], dca_count=dca_count, fill_mode=fill_mode)
        row["min_mtm_pct_equity"] = 100.0 * float(row.get("min_mtm", 0.0)) / max(initial_equity, 1e-12)
        row.update(
            {
                "candidate": candidate.name,
                "effective_dca_count": dca_count,
                "trend_ready": bool(state.get("ready")),
                "trend_risk_on": bool(state.get("risk_on")),
                "trend_reason": str(state.get("reason", "")),
                "trend_close": state.get("close", ""),
                "trend_ma": state.get("ma", ""),
                "trend_ma_slope_pct": state.get("ma_slope_pct", ""),
                "trend_rebound_ratio": state.get("rebound_ratio", ""),
            }
        )
        out.append(row)
    summary = summarize(out, initial_equity)
    summary.update(
        {
            "candidate": candidate.name,
            "dca_on": candidate.dca_count_when_on,
            "dca_off": candidate.dca_count_when_off,
            "ma_days": candidate.ma_days,
            "slope_days": candidate.slope_days,
            "lookback_days": candidate.lookback_days,
            "rebound_min": candidate.rebound_min,
            "require_ma_up": candidate.require_ma_up,
            "require_rebound": candidate.require_rebound,
        }
    )
    return out, summary


def write_equity_chart(path: Path, candidate_rows: Mapping[str, Sequence[Dict[str, Any]]], initial_equity: float) -> None:
    try:
        import matplotlib.pyplot as plt
    except Exception:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fig, ax = plt.subplots(figsize=(12, 6))
    for name, rows in candidate_rows.items():
        equity = initial_equity
        xs = []
        ys = []
        for row in rows:
            equity += float(row["pnl"])
            xs.append(parse_dt(str(row["closed_utc"])))
            ys.append(equity)
        ax.plot(xs, ys, label=name, linewidth=1.4)
    ax.set_title("HYPE daily-trend consilium equity")
    ax.set_xlabel("closed time UTC")
    ax.set_ylabel("equity")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8)
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)


def hard_gate(summary: Mapping[str, Any], baseline: Mapping[str, Any]) -> tuple[str, str]:
    if int(summary["trades"]) < 30:
        return "reject", "too few trades"
    if float(summary["min_trade_mtm_pct_equity"]) < -50.0:
        return "reject", "min trade MTM breaches -50% initial equity gate"
    if not bool(summary.get("min_order_ok", True)):
        return "reject", "minimum order below $2"
    if float(summary["max_mtm_dd_pct"]) < min(float(baseline["max_mtm_dd_pct"]) - 1.0, -5.0):
        return "reject", "drawdown worsened beyond gate"
    if float(summary["net_pct"]) <= float(baseline["net_pct"]) and float(summary["max_mtm_dd_pct"]) < float(baseline["max_mtm_dd_pct"]):
        return "reject", "lower return with worse drawdown"
    if float(summary["net_pct"]) >= float(baseline["net_pct"]) and float(summary["max_mtm_dd_pct"]) >= float(baseline["max_mtm_dd_pct"]):
        return "research_candidate", "beats current dca3 on return and drawdown; not a live promotion"
    return "keep_for_review", "does not dominate current dca3"


def main() -> None:
    ap = argparse.ArgumentParser(description="Run one HYPE daily-trend DCA consilium wave.")
    ap.add_argument("--positions-csv", default=str(DEFAULT_POSITIONS))
    ap.add_argument("--npz", default=str(DEFAULT_NPZ))
    ap.add_argument("--v21-config", default=str(DEFAULT_CONFIG))
    ap.add_argument("--base-variant", default="long_low_exposure", choices=sorted(HYPE_VARIANT_CHANGES))
    ap.add_argument("--out-dir", default=str(DEFAULT_REPORT_DIR / "daily_trend_rebound_consilium_wave_001"))
    ap.add_argument("--target-notional", type=float, default=100.0)
    ap.add_argument("--initial-equity", type=float, default=500.0)
    ap.add_argument(
        "--fill-mode",
        default="close_beyond_skip_boundary",
        choices=("touch", "touch_skip_boundary", "close_beyond", "close_beyond_skip_boundary"),
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    positions = read_positions(Path(args.positions_csv))
    all_rows = load_hype_rows(Path(args.npz))
    effective_config = write_variant_config(Path(args.v21_config), args.base_variant, out_dir)
    policy = load_v21_policy(str(effective_config), 3)

    results: Dict[str, Dict[str, Any]] = {}
    all_candidate_rows: Dict[str, List[Dict[str, Any]]] = {}
    for candidate in candidate_set():
        rows, summary = run_candidate(
            candidate,
            positions,
            all_rows,
            policy,
            target_notional=args.target_notional,
            initial_equity=args.initial_equity,
            fill_mode=args.fill_mode,
        )
        results[candidate.name] = summary
        all_candidate_rows[candidate.name] = rows
        write_csv(out_dir / "variants" / candidate.name / "trades.csv", rows)

    baseline = results["dca3_always_current_best"]
    for summary in results.values():
        verdict, reason = hard_gate(summary, baseline)
        summary["verdict"] = verdict
        summary["verdict_reason"] = reason

    ranked = sorted(results.values(), key=lambda s: (float(s["net_pct"]), float(s["max_dd_pct"])), reverse=True)
    write_csv(out_dir / "candidate_summary.csv", ranked)
    (out_dir / "journal.json").write_text(
        json.dumps(
            {
                "wave": 1,
                "source": "HYPE Binance copy closed positions",
                "consilium_source": "doc_2026-05-21_15-12-55.claude process adapted to deterministic local runner",
                "guardrails": [
                    "paper/backtest only",
                    "lead side remains authoritative",
                    "trend state uses completed daily bars before entry",
                    "trend decides only DCA depth, not entry side or exit",
                    "current dca3 is the promotion baseline",
                ],
                "inputs": {
                    "positions_csv": str(Path(args.positions_csv)),
                    "npz": str(Path(args.npz)),
                    "v21_config": str(Path(args.v21_config)),
                    "effective_variant_config": str(effective_config),
                    "base_variant": args.base_variant,
                    "target_notional": args.target_notional,
                    "initial_equity": args.initial_equity,
                    "fill_mode": args.fill_mode,
                },
                "baseline": baseline,
                "ranked": ranked,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    write_equity_chart(out_dir / "equity_curves.png", all_candidate_rows, args.initial_equity)

    md = [
        "# HYPE Daily Trend/Rebound DCA Consilium Wave 001",
        "",
        "This is a paper/backtest-only one-wave consilium. It keeps Binance copy side and close unchanged; daily trend/rebound only controls DCA depth.",
        f"Base V21 variant: `{args.base_variant}` (`configs/{args.base_variant}.yaml`).",
        f"Initial equity: {args.initial_equity:.2f}. Target notional: {args.target_notional:.2f} planned max position notional, not account equity.",
        f"Fill mode: `{args.fill_mode}`. Promotion requires min trade MTM >= -50% of initial equity.",
        "",
        "| rank | candidate | net % | max MTM DD % | min trade MTM % eq | PF | win % | avg/max notional | avg fills | risk-on entries | verdict |",
        "|---:|---|---:|---:|---:|---:|---:|---:|---:|---:|---|",
    ]
    for i, row in enumerate(ranked, 1):
        md.append(
            f"| {i} | {row['candidate']} | {row['net_pct']:.2f} | {row['max_mtm_dd_pct']:.2f} | "
            f"{row['min_trade_mtm_pct_equity']:.2f} | {row['pf']:.2f} | {row['win_rate_pct']:.1f} | "
            f"{row['avg_notional']:.1f}/{row['max_notional']:.1f} | {row['avg_dca_fills']:.2f} | "
            f"{row['risk_on_entries']} | {row['verdict']} |"
        )
    md.extend(
        [
            "",
            "Research advancement rule: a candidate must dominate current `dca3_always_current_best` on both return and MTM drawdown. This report does not promote live deployment.",
            "",
            "Files:",
            "- `candidate_summary.csv`",
            "- `journal.json`",
            "- `equity_curves.png`",
            "- `variants/<candidate>/trades.csv`",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), "top": ranked[:3]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
