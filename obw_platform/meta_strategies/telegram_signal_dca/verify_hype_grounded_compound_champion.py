#!/usr/bin/env python3
"""Verify and report the canonical HYPE grounded compound research champion.

Research/paper only. Uses local closed positions and local HYPE 1m NPZ.
No live orders, secrets, sessions, or network APIs.
"""
from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, List

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from compare_binance_copy_positions_dca import read_positions  # noqa: E402
from run_hype_dca_parameter_search import (  # noqa: E402
    CANONICAL_RESEARCH_CHAMPION,
    DEFAULT_NPZ,
    DEFAULT_POSITIONS,
    DEFAULT_REPORT_DIR,
    STRICT_FILL_MODE,
    Candidate,
    annotate_trade_equity_metrics,
    grounding_stats,
    simulate_candidate_rows,
    summarize,
    write_csv,
)


INITIAL_EQUITY = 500.0
MIN_TRADE_MTM_PCT = -50.0


def champion(slippage_mult: float = 1.0) -> Candidate:
    return Candidate(
        name=CANONICAL_RESEARCH_CHAMPION,
        target_notional=500.0,
        base_frac=0.16,
        steps_pct=(0.25, 0.35, 0.55),
        add_weights=(0.8, 1.2, 2.2),
        slippage=0.0009380229915652661 * slippage_mult,
    )


def high_notional_illusion() -> Candidate:
    return replace(champion(), name="high_notional_illusion_t1200_same_shape", target_notional=1200.0)


def run_case(
    positions: List[Any],
    arrays: Dict[str, Any],
    candidate: Candidate,
    *,
    label: str,
    position_sizing_mode: str,
    initial_equity: float,
    fill_mode: str,
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    rows = simulate_candidate_rows(
        positions,
        candidate,
        arrays,
        fill_mode=fill_mode,
        initial_equity=initial_equity,
        position_sizing_mode=position_sizing_mode,
    )
    annotate_trade_equity_metrics(rows, initial_equity)
    summary = summarize(rows, initial_equity)
    summary.update(
        {
            "label": label,
            "candidate": candidate.name,
            "initial_equity": initial_equity,
            "initial_max_target_notional": candidate.target_notional,
            "position_sizing_mode": position_sizing_mode,
            "fill_mode": fill_mode,
            **grounding_stats(rows, fill_mode=fill_mode, min_trade_mtm_pct=MIN_TRADE_MTM_PCT),
        }
    )
    return rows, summary


def assert_canonical(summary: Dict[str, Any]) -> None:
    if summary["label"] != "grounded_compound_champion":
        raise AssertionError("canonical summary label mismatch")
    if summary["fill_mode"] != STRICT_FILL_MODE or not bool(summary["strict_fill_ok"]):
        raise AssertionError("canonical champion must use strict close_beyond_skip_boundary fill mode")
    if int(summary["notional_gt_equity_before_count"]) != 0:
        raise AssertionError("canonical champion has trades with notional > equity_before")
    if float(summary["min_trade_mtm_pct_equity"]) < MIN_TRADE_MTM_PCT:
        raise AssertionError("canonical champion breaches min_trade_mtm_pct_equity gate")
    if not bool(summary["grounded_compound_gate_ok"]):
        raise AssertionError("canonical grounded compound gate failed")


def main() -> None:
    ap = argparse.ArgumentParser(description="Verify canonical HYPE grounded compound champion.")
    ap.add_argument("--positions-csv", default=str(DEFAULT_POSITIONS))
    ap.add_argument("--npz", default=str(DEFAULT_NPZ))
    ap.add_argument(
        "--out-dir",
        default=str(DEFAULT_REPORT_DIR / "grounded_compound_champion_canonical_20260524"),
    )
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    positions = read_positions(Path(args.positions_csv))
    from run_hype_dca_parameter_search import load_npz_arrays  # local import keeps public surface small

    arrays = load_npz_arrays(Path(args.npz))

    summaries: List[Dict[str, Any]] = []

    rows, summary = run_case(
        positions,
        arrays,
        champion(),
        label="grounded_compound_champion",
        position_sizing_mode="compound",
        initial_equity=INITIAL_EQUITY,
        fill_mode=STRICT_FILL_MODE,
    )
    assert_canonical(summary)
    write_csv(out_dir / "trades_grounded_compound_champion.csv", rows)
    summaries.append(summary)

    static_rows, static_summary = run_case(
        positions,
        arrays,
        champion(),
        label="static_500_cap",
        position_sizing_mode="fixed",
        initial_equity=INITIAL_EQUITY,
        fill_mode=STRICT_FILL_MODE,
    )
    write_csv(out_dir / "trades_static_500_cap.csv", static_rows)
    summaries.append(static_summary)

    illusion_rows, illusion_summary = run_case(
        positions,
        arrays,
        high_notional_illusion(),
        label="high_notional_illusion",
        position_sizing_mode="fixed",
        initial_equity=INITIAL_EQUITY,
        fill_mode=STRICT_FILL_MODE,
    )
    write_csv(out_dir / "trades_high_notional_illusion.csv", illusion_rows)
    summaries.append(illusion_summary)

    stress: List[Dict[str, Any]] = []
    for mult in (1.0, 2.0, 3.0):
        _, s = run_case(
            positions,
            arrays,
            champion(slippage_mult=mult),
            label="grounded_compound_champion",
            position_sizing_mode="compound",
            initial_equity=INITIAL_EQUITY,
            fill_mode=STRICT_FILL_MODE,
        )
        assert_canonical(s)
        s["slippage_mult"] = mult
        stress.append(s)

    write_csv(out_dir / "summary.csv", summaries)
    write_csv(out_dir / "stress_slippage.csv", stress)
    (out_dir / "journal.json").write_text(
        json.dumps({"summary": summaries, "stress_slippage": stress}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    champ = summary
    static = static_summary
    illusion = illusion_summary
    md = [
        "# HYPE Grounded Compound Champion Canonical Research Report",
        "",
        "Research/paper-loop only. No live promotion is implied.",
        "",
        "## Canonical",
        "",
        f"- Name: `{CANONICAL_RESEARCH_CHAMPION}`",
        f"- Label: `grounded_compound_champion`",
        f"- Initial equity: `${INITIAL_EQUITY:.2f}`",
        "- Initial max target notional/cap: `$500.00`",
        f"- Strict fill mode: `{STRICT_FILL_MODE}`",
        "- Compound rule: effective target notional grows only from realized equity and is capped at `equity_before`.",
        "",
        "## Results",
        "",
        f"- Finish equity: `${champ['equity_end']:.2f}`",
        f"- Profit: `${float(champ['equity_end']) - INITIAL_EQUITY:.2f}`",
        f"- Net: `{champ['net_pct']:.2f}%`",
        f"- Max MTM DD: `{champ['max_mtm_dd_pct']:.2f}%`",
        f"- Min trade MTM from starting `$500`: `{champ['min_trade_mtm_pct_equity']:.2f}%`",
        f"- PF: `{champ['pf']:.2f}`",
        f"- Avg/max notional: `${champ['avg_notional']:.2f}` / `${champ['max_notional']:.2f}`",
        f"- `notional > equity_before` cases: `{champ['notional_gt_equity_before_count']}`",
        f"- Max effective target notional at end: `${champ['max_notional']:.2f}`",
        "",
        "## Slippage Stress",
        "",
        "| mult | finish | net % | max MTM DD % | min trade MTM % eq | notional>equity |",
        "|---:|---:|---:|---:|---:|---:|",
    ]
    for s in stress:
        md.append(
            f"| {s['slippage_mult']:.0f}x | ${s['equity_end']:.2f} | {s['net_pct']:.2f} | "
            f"{s['max_mtm_dd_pct']:.2f} | {s['min_trade_mtm_pct_equity']:.2f} | "
            f"{s['notional_gt_equity_before_count']} |"
        )
    md.extend(
        [
            "",
            "## Distinctions",
            "",
            f"- `static_500_cap`: finish `${static['equity_end']:.2f}`, net `{static['net_pct']:.2f}%`.",
            f"- `grounded_compound_champion`: finish `${champ['equity_end']:.2f}`, net `{champ['net_pct']:.2f}%`.",
            f"- `high_notional_illusion`: finish `${illusion['equity_end']:.2f}`, net `{illusion['net_pct']:.2f}%`; rejected for research promotion because it starts above the `$500` cap.",
            "",
            "## Gates",
            "",
            "- PASS: strict fill mode is `close_beyond_skip_boundary`.",
            "- PASS: no trade exceeds `equity_before` beyond floating tolerance.",
            "- PASS: min trade MTM is above the `-50%` initial-equity gate.",
            "- BLOCKED FOR LIVE: this is still closed-position research replay, not paper/live execution.",
        ]
    )
    (out_dir / "REPORT.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(json.dumps({"out_dir": str(out_dir), "champion": champ, "stress": stress}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
