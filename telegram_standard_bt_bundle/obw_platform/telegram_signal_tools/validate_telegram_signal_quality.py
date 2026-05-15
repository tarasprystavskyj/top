#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Validate Telegram signal JSONL/CSV and print a compact quality report."""
from __future__ import annotations

import argparse
import json

try:
    from .telegram_signal_schema import quality_report, read_signal_rows
except ImportError:
    from telegram_signal_schema import quality_report, read_signal_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--signals", required=True, help="Telegram signal JSONL or replay-compatible CSV")
    ap.add_argument("--min-valid-ratio", type=float, default=0.95)
    ap.add_argument("--fail-on-invalid", action="store_true")
    ap.add_argument("--fail-below-threshold", action="store_true")
    args = ap.parse_args()

    rows = read_signal_rows(args.signals)
    report = quality_report(rows)
    report["meets_valid_ratio"] = report["valid_ratio"] >= args.min_valid_ratio
    report["ok"] = report["meets_valid_ratio"] and (not args.fail_on_invalid or report["invalid_rows"] == 0)
    print(json.dumps(report, indent=2, ensure_ascii=False))
    if (args.fail_below_threshold and not report["meets_valid_ratio"]) or (args.fail_on_invalid and report["invalid_rows"]):
        raise SystemExit(2)


if __name__ == "__main__":
    main()
