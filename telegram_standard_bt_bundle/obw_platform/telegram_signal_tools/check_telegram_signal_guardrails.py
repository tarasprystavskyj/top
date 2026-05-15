#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lightweight no-live guardrail check for Telegram signal reports."""
import argparse
import csv
import json
from pathlib import Path
from typing import Any, Dict, Optional


def _f(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(v)
    except Exception:
        return None


def load_summary(path: str) -> Dict[str, Any]:
    p = Path(path)
    if p.suffix.lower() == ".json":
        return json.loads(p.read_text(encoding="utf-8"))
    with p.open("r", newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else {}


def drawdown_fraction(summary: Dict[str, Any]) -> Optional[float]:
    dd = _f(summary.get("mtm_max_dd_pct") or summary.get("max_drawdown_mtm") or summary.get("max_dd_mtm") or summary.get("max_dd"))
    if dd is not None and dd < -1.0:
        return dd / 100.0
    return dd


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--summary", required=True)
    ap.add_argument("--min-trades", type=int, default=100)
    ap.add_argument("--min-profit-factor", type=float, default=1.3)
    ap.add_argument("--max-mtm-dd-pct", type=float, default=-0.20)
    args = ap.parse_args()
    s = load_summary(args.summary)
    trades = int(_f(s.get("trades") or s.get("n_trades") or s.get("closed_trades")) or 0)
    pf = _f(s.get("profit_factor") or s.get("pf"))
    dd = drawdown_fraction(s)
    failures = []
    if trades < args.min_trades:
        failures.append(f"trades {trades} < {args.min_trades}")
    if pf is None or pf < args.min_profit_factor:
        failures.append(f"profit_factor {pf} < {args.min_profit_factor}")
    if dd is None or dd < args.max_mtm_dd_pct:
        failures.append(f"mtm_dd {dd} < {args.max_mtm_dd_pct}")
    print(json.dumps({"ok_for_live": not failures, "trades": trades, "profit_factor": pf, "mtm_dd": dd, "failures": failures}, indent=2, ensure_ascii=False))
    if failures:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
