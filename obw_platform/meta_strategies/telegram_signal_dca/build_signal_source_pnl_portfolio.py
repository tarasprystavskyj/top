#!/usr/bin/env python3
"""Build signal-source PnL charts and a simple ranked $ portfolio."""

from __future__ import annotations

import argparse
import csv
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import matplotlib.dates as mdates
import matplotlib.pyplot as plt


ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TELEGRAM_REPORT = (
    ROOT
    / "obw_platform"
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "all_channels_v21_firstbar_same_max_100_20260519"
)
DEFAULT_BINANCE_REPORTS = [
    ROOT
    / "obw_platform"
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "binance_copy_4728671486012660992_20260519"
    / "backtest",
    ROOT
    / "obw_platform"
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "binance_copy_4751838302089254401_20260519_ttl72_baseline",
    ROOT
    / "obw_platform"
    / "meta_strategies"
    / "telegram_signal_dca"
    / "reports"
    / "binance_copy_4906010685108267264_20260519",
]


def parse_dt(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def safe_float(row: dict[str, str], key: str, default: float = 0.0) -> float:
    try:
        value = row.get(key, "")
        return default if value == "" else float(value)
    except (TypeError, ValueError):
        return default


def max_drawdown_pct(values: list[float]) -> float:
    if not values:
        return 0.0
    peak = values[0]
    worst = 0.0
    for value in values:
        peak = max(peak, value)
        if peak:
            worst = min(worst, (value / peak - 1.0) * 100.0)
    return worst


def monthly_return_pct(times: list[datetime], values: list[float]) -> float:
    if len(times) < 2 or not values or not values[0]:
        return 0.0
    days = max((times[-1] - times[0]).total_seconds() / 86400.0, 1e-9)
    return ((values[-1] / values[0]) - 1.0) * 100.0 * 30.0 / days


def risk_score(monthly_pct: float, mdd_pct: float, signal_count: float) -> float:
    if monthly_pct <= 0:
        return 0.0
    sample_factor = math.sqrt(max(min(signal_count, 100.0), 1.0) / 100.0)
    return monthly_pct / max(abs(mdd_pct), 0.25) * sample_factor


def is_dca_variant(variant: str) -> bool:
    return "dca" in variant.lower()


def load_telegram_sources(report_dir: Path, dca_only: bool) -> list[dict]:
    summary_path = report_dir / "channel_dca_comparison_summary.csv"
    rows = read_csv(summary_path)
    by_source: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in rows:
        by_source[row["source_channel"]].append(row)

    sources = []
    for name, source_rows in sorted(by_source.items()):
        variants = []
        for row in source_rows:
            variant = row["variant"]
            if dca_only and not is_dca_variant(variant):
                continue
            equity_path = report_dir / name / "all_signals" / f"{variant}_equity.csv"
            if not equity_path.exists():
                continue
            points = read_csv(equity_path)
            times = [parse_dt(p["datetime_utc"]) for p in points]
            values = [safe_float(p, "equity_mtm") for p in points]
            if not times or not values:
                continue
            signals = safe_float(row, "input_signals_channel")
            mdd = max_drawdown_pct(values)
            monthly = monthly_return_pct(times, values)
            variants.append(
                {
                    "variant": variant,
                    "times": times,
                    "values": values,
                    "start_equity": values[0],
                    "end_equity": values[-1],
                    "monthly_pct": monthly,
                    "mdd_pct": mdd,
                    "signals": signals,
                    "score": risk_score(monthly, mdd, signals),
                    "source_type": "telegram",
                    "capital_base": values[0],
                }
            )
        if variants:
            best = max(variants, key=lambda v: (v["score"], v["monthly_pct"]))
            best["name"] = f"tg:{name}"
            best["all_variants"] = variants
            sources.append(best)
    return sources


def infer_binance_lead_name(report_dir: Path) -> str:
    parts = [p for p in report_dir.parts if p.startswith("binance_copy_")]
    if not parts:
        return f"binance:{report_dir.name}"
    return "binance:" + parts[-1].replace("binance_copy_", "")


def find_candle_file(candle_dir: Path, trade_id: str) -> Path | None:
    matches = list(candle_dir.glob(f"{trade_id}_*.json"))
    return matches[0] if matches else None


def active_fills_for_trade(row: dict[str, str]) -> list[dict[str, float]]:
    side = row.get("side", "LONG").upper()
    entry = safe_float(row, "entry")
    total_notional = safe_float(row, "notional")
    fills = []
    raw_fills = []
    try:
        raw_fills = json.loads(row.get("fills_json") or "[]")
    except json.JSONDecodeError:
        raw_fills = []
    fill_notional = sum(float(f.get("notional", 0.0)) for f in raw_fills)
    initial_notional = max(total_notional - fill_notional, 0.0)
    entry_dt_key = "entry_utc" if row.get("entry_utc") else "opened_utc"
    entry_ms = int(parse_dt(row[entry_dt_key]).timestamp() * 1000)
    if initial_notional:
        fills.append({"t": entry_ms, "price": entry, "notional": initial_notional})
    for f in raw_fills:
        fills.append(
            {
                "t": int(f.get("t", entry_ms)),
                "price": float(f.get("level", entry)),
                "notional": float(f.get("notional", 0.0)),
            }
        )
    for fill in fills:
        fill["sign"] = 1.0 if side == "LONG" else -1.0
    return fills


def mtm_pnl_for_fills(fills: list[dict[str, float]], now_ms: int, close: float) -> float:
    pnl = 0.0
    for fill in fills:
        if fill["t"] <= now_ms and fill["price"]:
            pnl += fill["notional"] * fill["sign"] * (close / fill["price"] - 1.0)
    return pnl


def max_concurrent_capital(rows: list[dict[str, str]], fallback_notional: float = 100.0) -> float:
    events: list[tuple[datetime, float]] = []
    for row in rows:
        start_key = "entry_utc" if row.get("entry_utc") else "opened_utc"
        end_key = "exit_utc" if row.get("exit_utc") else "closed_utc"
        if not row.get(start_key) or not row.get(end_key):
            continue
        notional = safe_float(row, "notional", fallback_notional) or fallback_notional
        events.append((parse_dt(row[start_key]), notional))
        events.append((parse_dt(row[end_key]), -notional))
    current = 0.0
    peak = fallback_notional
    for _, delta in sorted(events, key=lambda item: item[0]):
        current += delta
        peak = max(peak, current)
    return max(peak, fallback_notional)


def load_binance_sources(report_dirs: list[Path], dca_only: bool) -> list[dict]:
    sources = []
    for report_dir in report_dirs:
        summary_path = report_dir / "summary.json"
        if not summary_path.exists():
            continue
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        variants = []
        candle_dir = report_dir / "candles_1m_after_close"
        if not candle_dir.exists():
            candle_dir = report_dir / "candles_1m"
        for label, metrics in summary.items():
            if dca_only and not is_dca_variant(label):
                continue
            trade_path = report_dir / f"{label}_trades.csv"
            if not trade_path.exists():
                continue
            rows = read_csv(trade_path)
            capital_base = (
                float(metrics.get("equity_start") or 0.0)
                or float(metrics.get("max_concurrent_capital") or 0.0)
                or max_concurrent_capital(rows, float(metrics.get("target_notional", 100.0)))
            )
            events: dict[datetime, float] = defaultdict(float)
            missing_candles = 0
            for row in rows:
                trade_id = row.get("id", "")
                candle_path = find_candle_file(candle_dir, trade_id) if candle_dir.exists() else None
                exit_key = "exit_utc" if row.get("exit_utc") else "closed_utc"
                exit_dt = parse_dt(row[exit_key])
                if candle_path is None:
                    missing_candles += 1
                    events[exit_dt] += safe_float(row, "pnl")
                    continue
                fills = active_fills_for_trade(row)
                try:
                    candles = json.loads(candle_path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    missing_candles += 1
                    events[exit_dt] += safe_float(row, "pnl")
                    continue
                last_pnl = 0.0
                for candle in candles:
                    t_ms = int(candle["t"])
                    dt = datetime.fromtimestamp(t_ms / 1000.0, tz=timezone.utc)
                    pnl = mtm_pnl_for_fills(fills, t_ms, float(candle["close"]))
                    events[dt] += pnl - last_pnl
                    last_pnl = pnl
                events[exit_dt] += safe_float(row, "pnl") - last_pnl
            times = sorted(events)
            values = []
            equity = capital_base
            for t in times:
                equity += events[t]
                values.append(equity)
            if not times:
                continue
            mdd = max_drawdown_pct(values)
            monthly = monthly_return_pct(times, values)
            if missing_candles == len(rows):
                # Fully candle-less Binance reports are realized-step approximations;
                # prefer the backtest summary's own risk/return metrics for ranking.
                mdd = float(metrics.get("max_dd_pct") or mdd)
                monthly = float(metrics.get("net_pct_per_30d") or monthly)
            signals = float(metrics.get("positions") or metrics.get("count") or len(rows))
            variants.append(
                {
                    "variant": label,
                    "times": times,
                    "values": values,
                    "start_equity": capital_base,
                    "end_equity": values[-1],
                    "monthly_pct": monthly,
                    "mdd_pct": mdd,
                    "signals": signals,
                    "score": risk_score(monthly, mdd, signals),
                    "source_type": "binance_copy",
                    "capital_base": capital_base,
                    "missing_candles": missing_candles,
                }
            )
        if variants:
            best = max(variants, key=lambda v: (v["score"], v["monthly_pct"]))
            best["name"] = infer_binance_lead_name(report_dir)
            best["all_variants"] = variants
            sources.append(best)
    return sources


def make_portfolio(sources: list[dict], total_capital: float) -> tuple[list[dict], list[datetime], list[float]]:
    score_sum = sum(s["score"] for s in sources if s["score"] > 0)
    weighted = []
    events: dict[datetime, float] = defaultdict(float)
    for s in sources:
        weight = total_capital * s["score"] / score_sum if score_sum and s["score"] > 0 else 0.0
        s["allocation_usd"] = weight
        s["allocation_pct"] = weight / total_capital * 100.0 if total_capital else 0.0
        prev_value = weight
        for t, equity in zip(s["times"], s["values"]):
            source_value = weight * equity / s["start_equity"] if s["start_equity"] else weight
            events[t] += source_value - prev_value
            prev_value = source_value
        weighted.append(s)
    times = sorted(events)
    values = []
    equity = total_capital
    for t in times:
        equity += events[t]
        values.append(equity)
    return weighted, times, values


def plot_sources(sources: list[dict], out_path: Path) -> None:
    fig, ax = plt.subplots(figsize=(16, 9))
    for s in sources:
        normalized = [(v / s["start_equity"] - 1.0) * 100.0 for v in s["values"]]
        label = f'{s["name"]} {s["variant"]} ({normalized[-1]:+.2f}%)'
        ax.plot(s["times"], normalized, linewidth=1.4, label=label)
    ax.axhline(0, color="#222222", linewidth=0.8)
    ax.set_title("Signal sources MTM PnL, best variant per source")
    ax.set_ylabel("MTM PnL, % on source capital base")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8, ncol=2)
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def plot_portfolio(sources: list[dict], times: list[datetime], values: list[float], out_path: Path) -> None:
    fig, axes = plt.subplots(2, 1, figsize=(16, 10), sharex=True, height_ratios=[3, 1])
    ax = axes[0]
    for s in sources:
        if s["allocation_usd"] <= 0:
            continue
        line = [s["allocation_usd"] * v / s["start_equity"] for v in s["values"]]
        ax.plot(s["times"], line, linewidth=1.0, alpha=0.75, label=f'{s["name"]} ${s["allocation_usd"]:.2f}')
    ax.plot(times, values, color="#111111", linewidth=2.2, label="portfolio")
    ax.set_title("$500 ranked portfolio MTM")
    ax.set_ylabel("Equity, USD")
    ax.grid(True, alpha=0.25)
    ax.legend(fontsize=8, ncol=2)

    peaks = []
    peak = values[0] if values else 0.0
    drawdowns = []
    for v in values:
        peak = max(peak, v)
        peaks.append(peak)
        drawdowns.append((v / peak - 1.0) * 100.0 if peak else 0.0)
    axes[1].fill_between(times, drawdowns, 0, color="#b33a3a", alpha=0.35)
    axes[1].set_ylabel("DD, %")
    axes[1].grid(True, alpha=0.25)
    axes[1].xaxis.set_major_formatter(mdates.DateFormatter("%Y-%m-%d"))
    fig.autofmt_xdate()
    fig.tight_layout()
    fig.savefig(out_path, dpi=150)
    plt.close(fig)


def write_outputs(
    sources: list[dict],
    p_times: list[datetime],
    p_values: list[float],
    out_dir: Path,
    total: float,
    base_order_pct: float,
    dca_only: bool,
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    plot_sources(sources, out_dir / "signal_sources_best_variants_mtm.png")
    plot_portfolio(sources, p_times, p_values, out_dir / "portfolio_500_ranked_mtm_canvas.png")
    with (out_dir / "portfolio_500_ranked_mtm.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["datetime_utc", "portfolio_mtm"])
        for t, v in zip(p_times, p_values):
            w.writerow([t.isoformat().replace("+00:00", "Z"), f"{v:.10f}"])
    with (out_dir / "source_allocations.csv").open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "source",
                "type",
                "variant",
                "signals",
                "score",
                "allocation_usd",
                "allocation_pct",
                "base_order_pct",
                "base_order_usd",
                "monthly_pct",
                "mtm_mdd_pct",
                "end_pct",
                "missing_candles",
            ]
        )
        for s in sorted(sources, key=lambda x: x["allocation_usd"], reverse=True):
            end_pct = (s["end_equity"] / s["start_equity"] - 1.0) * 100.0
            w.writerow(
                [
                    s["name"],
                    s["source_type"],
                    s["variant"],
                    f'{s["signals"]:.0f}',
                    f'{s["score"]:.8f}',
                    f'{s["allocation_usd"]:.6f}',
                    f'{s["allocation_pct"]:.4f}',
                    f"{base_order_pct:.6f}",
                    f'{s["allocation_usd"] * base_order_pct / 100.0:.6f}',
                    f'{s["monthly_pct"]:.6f}',
                    f'{s["mdd_pct"]:.6f}',
                    f"{end_pct:.6f}",
                    s.get("missing_candles", 0),
                ]
            )
    p_mdd = max_drawdown_pct(p_values)
    p_monthly = monthly_return_pct(p_times, p_values)
    p_end = p_values[-1] if p_values else total
    lines = [
        "# Signal Source MTM Portfolio",
        "",
        f"- Total capital: ${total:.2f}",
        f"- Mode: {'DCA only, one directional leg per signal' if dca_only else 'best available variant per source'}",
        f"- Base DCA order: {base_order_pct:.2f}% of delegated source capital",
        f"- Portfolio final MTM: ${p_end:.2f} ({(p_end / total - 1.0) * 100.0:+.2f}%)",
        f"- Portfolio MTM max drawdown: {p_mdd:.2f}%",
        f"- Portfolio extrapolated return per 30d: {p_monthly:+.2f}%",
        "",
        "## Allocation",
        "",
        "| Source | Variant | Allocation | Base order | Score | 30d % | MTM MDD % | End % | Signals |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for s in sorted(sources, key=lambda x: x["allocation_usd"], reverse=True):
        end_pct = (s["end_equity"] / s["start_equity"] - 1.0) * 100.0
        base_order = s["allocation_usd"] * base_order_pct / 100.0
        lines.append(
            f'| {s["name"]} | {s["variant"]} | ${s["allocation_usd"]:.2f} '
            f'({s["allocation_pct"]:.1f}%) | ${base_order:.2f} | {s["score"]:.4f} | '
            f'{s["monthly_pct"]:+.2f}% | {s["mdd_pct"]:.2f}% | {end_pct:+.2f}% | {s["signals"]:.0f} |'
        )
    lines.extend(
        [
            "",
            "## Files",
            "",
            "- `signal_sources_best_variants_mtm.png`",
            "- `portfolio_500_ranked_mtm_canvas.png`",
            "- `portfolio_500_ranked_mtm.csv`",
            "- `source_allocations.csv`",
            "",
            "Note: Telegram curves use existing MTM equity CSV. Binance-copy curves reconstruct MTM from saved 1m candles and trade fills where available.",
            "Sizing note: historical curves are scaled linearly to delegated capital; `base_order_usd` is the intended live/paper DCA initial order size.",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--telegram-report", type=Path, default=DEFAULT_TELEGRAM_REPORT)
    parser.add_argument("--out-dir", type=Path, default=ROOT / "reports" / "signal_source_portfolio_20260519")
    parser.add_argument("--capital", type=float, default=500.0)
    parser.add_argument("--base-order-pct", type=float, default=1.5)
    parser.add_argument("--dca-only", action="store_true")
    args = parser.parse_args()

    sources = load_telegram_sources(args.telegram_report, args.dca_only)
    sources.extend(load_binance_sources(DEFAULT_BINANCE_REPORTS, args.dca_only))
    sources = sorted(sources, key=lambda s: s["name"])
    weighted, p_times, p_values = make_portfolio(sources, args.capital)
    write_outputs(weighted, p_times, p_values, args.out_dir, args.capital, args.base_order_pct, args.dca_only)
    print(f"Wrote {args.out_dir}")
    print(f"Sources: {len(weighted)}")
    print(f"Portfolio final: {p_values[-1]:.2f} mdd={max_drawdown_pct(p_values):.2f}%")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
