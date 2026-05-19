#!/usr/bin/env python3
"""Run per-channel plain vs V21 DCA comparisons.

The non-leaky selector mode uses only the first chronological slice to choose
profitable base symbols, then evaluates only later OOS signals.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, Iterable, List, Sequence

from telegram_signal_dca_compare import (
    Signal,
    load_price_rows,
    load_v21_policy,
    policy_for_capital_mode,
    read_signals,
    simulate,
    write_csv,
)


def channel_name(raw: str) -> str:
    s = str(raw or "unknown").strip() or "unknown"
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in s)[:80]


def unique_channels(signals: Iterable[Signal]) -> List[str]:
    return sorted({s.source_channel or "unknown" for s in signals})


def split_train_oos(signals: Sequence[Signal], train_frac: float) -> tuple[List[Signal], List[Signal]]:
    ordered = sorted(signals, key=lambda s: s.dt)
    cut = int(len(ordered) * train_frac)
    cut = max(0, min(len(ordered), cut))
    return list(ordered[:cut]), list(ordered[cut:])


def run_variants(
    rows: List[Dict[str, Any]],
    signals: List[Signal],
    *,
    policy: Dict[str, Any],
    counts: List[int],
    out_dir: Path,
    initial_equity: float,
    target_notional: float,
    capital_mode: str,
    entry_mode: str,
) -> List[Dict[str, Any]]:
    summaries: List[Dict[str, Any]] = []
    out_dir.mkdir(parents=True, exist_ok=True)
    for count in counts:
        label = "plain" if count == 0 else f"v21_dca{count}"
        run_policy = policy if capital_mode == "v21_config" else policy_for_capital_mode(
            policy,
            count,
            target_notional,
            capital_mode,
        )
        summary, trades, equity, sym = simulate(
            rows,
            signals,
            initial_equity=initial_equity,
            dca_count=count,
            policy=run_policy,
            tp1_frac=1.0 / 3.0,
            tp2_frac=0.5,
            entry_mode=entry_mode,
        )
        summaries.append({"variant": label, **summary})
        write_csv(out_dir / f"{label}_trades.csv", trades)
        write_csv(out_dir / f"{label}_equity.csv", equity)
        write_csv(out_dir / f"{label}_symbol_summary.csv", sym)
    write_csv(out_dir / "dca_summary.csv", summaries)
    return summaries


def profitable_bases_from_train(
    rows: List[Dict[str, Any]],
    train_signals: List[Signal],
    *,
    policy: Dict[str, Any],
    initial_equity: float,
    min_closed_rows: int,
) -> Dict[str, Any]:
    summary, trades, _equity, sym = simulate(
        rows,
        train_signals,
        initial_equity=initial_equity,
        dca_count=0,
        policy=policy,
        tp1_frac=1.0 / 3.0,
        tp2_frac=0.5,
        entry_mode="close_in_zone",
    )
    bases = [
        str(row["base"])
        for row in sym
        if float(row.get("realized_pnl", 0.0)) > 0.0 and int(row.get("trade_rows", 0)) >= min_closed_rows
    ]
    return {
        "train_summary": summary,
        "train_trade_rows": len(trades),
        "min_closed_rows": min_closed_rows,
        "profitable_bases": sorted(set(bases)),
        "symbol_summary": sym,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals-csv", required=True)
    ap.add_argument("--price-db", required=True)
    ap.add_argument("--v21-config", default="obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--dca-counts", default="0,1,2,3,4,5")
    ap.add_argument("--initial-equity", type=float, default=1000.0)
    ap.add_argument("--target-notional", type=float, default=100.0)
    ap.add_argument("--capital-mode", choices=["v21_config", "same_initial", "same_max"], default="same_max")
    ap.add_argument("--ttl-hours", type=float, default=72.0)
    ap.add_argument("--entry-mode", choices=["first_bar", "close_in_zone", "touch_zone"], default="first_bar")
    ap.add_argument("--side", choices=["both", "long", "short"], default="both")
    ap.add_argument("--train-frac", type=float, default=0.7)
    ap.add_argument("--min-profitable-trade-rows", type=int, default=1)
    ap.add_argument(
        "--causal-profitable-channel",
        default="",
        help="Optional channel to evaluate with train-selected profitable bases on OOS only.",
    )
    args = ap.parse_args()

    counts = [int(x.strip()) for x in args.dca_counts.split(",") if x.strip()]
    rows = load_price_rows(args.price_db)
    signals = read_signals(args.signals_csv, args.ttl_hours, args.side)
    policy = load_v21_policy(args.v21_config, max(counts))
    out_root = Path(args.out_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    all_summaries: List[Dict[str, Any]] = []
    for channel in unique_channels(signals):
        channel_signals = [s for s in signals if (s.source_channel or "unknown") == channel]
        channel_dir = out_root / channel_name(channel) / "all_signals"
        summaries = run_variants(
            rows,
            channel_signals,
            policy=policy,
            counts=counts,
            out_dir=channel_dir,
            initial_equity=args.initial_equity,
            target_notional=args.target_notional,
            capital_mode=args.capital_mode,
            entry_mode=args.entry_mode,
        )
        for row in summaries:
            all_summaries.append(
                {
                    "scope": "all_signals",
                    "source_channel": channel,
                    "input_signals_channel": len(channel_signals),
                    **row,
                }
            )

    causal_channel = args.causal_profitable_channel.strip()
    if causal_channel:
        channel_signals = [s for s in signals if (s.source_channel or "").lower() == causal_channel.lower()]
        train, oos = split_train_oos(channel_signals, args.train_frac)
        selector = profitable_bases_from_train(
            rows,
            train,
            policy=policy,
            initial_equity=args.initial_equity,
            min_closed_rows=args.min_profitable_trade_rows,
        )
        selected = set(selector["profitable_bases"])
        oos_selected = [s for s in oos if s.base in selected]
        causal_dir = out_root / channel_name(causal_channel) / "causal_train_profitable_bases_oos"
        summaries = run_variants(
            rows,
            oos_selected,
            policy=policy,
            counts=counts,
            out_dir=causal_dir,
            initial_equity=args.initial_equity,
            target_notional=args.target_notional,
            capital_mode=args.capital_mode,
            entry_mode=args.entry_mode,
        )
        selector_meta = {
            "source_channel": causal_channel,
            "train_frac": args.train_frac,
            "train_signals": len(train),
            "oos_signals": len(oos),
            "oos_selected_signals": len(oos_selected),
            **selector,
        }
        (causal_dir / "selector_meta.json").write_text(
            json.dumps(selector_meta, indent=2, ensure_ascii=False, default=str),
            encoding="utf-8",
        )
        write_csv(causal_dir / "oos_selected_signals.csv", [s.__dict__ for s in oos_selected])
        for row in summaries:
            all_summaries.append(
                {
                    "scope": "causal_train_profitable_bases_oos",
                    "source_channel": causal_channel,
                    "input_signals_channel": len(channel_signals),
                    "train_signals": len(train),
                    "oos_signals": len(oos),
                    "oos_selected_signals": len(oos_selected),
                    "selected_bases": " ".join(sorted(selected)),
                    **row,
                }
            )

    write_csv(out_root / "channel_dca_comparison_summary.csv", all_summaries)


if __name__ == "__main__":
    main()
