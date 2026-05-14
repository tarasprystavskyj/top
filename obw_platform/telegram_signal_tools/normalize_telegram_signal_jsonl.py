#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Normalize paper Telegram signal JSONL into replay-compatible CSV.

Input rows are produced by telegram_signal_listener_paper.py:
  ts_utc,symbol,side,entry_low,entry_high,tp,sl,raw_text,...

Output rows match TelegramSignalReplayStrategy:
  message_idx,dt_utc,symbol,side,leverage,entry_a,entry_b,sl,tp1,tp2,tp3,
  raw_text,entry_low,entry_high
"""
import argparse
import csv
from pathlib import Path

try:
    from .telegram_signal_schema import REPLAY_FIELDS, normalize_rows, read_signal_rows
except ImportError:
    from telegram_signal_schema import REPLAY_FIELDS, normalize_rows, read_signal_rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--jsonl", required=True, help="Paper listener JSONL")
    ap.add_argument("--out-csv", required=True, help="Replay-compatible CSV")
    args = ap.parse_args()

    rows = read_signal_rows(args.jsonl)
    normalized = normalize_rows(rows)
    Path(args.out_csv).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=REPLAY_FIELDS)
        w.writeheader()
        w.writerows(normalized)
    print({"input_rows": len(rows), "output_rows": len(normalized), "out_csv": args.out_csv})


if __name__ == "__main__":
    main()
