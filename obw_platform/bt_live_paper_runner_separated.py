#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import argparse

from runners.common import load_yaml_or_json
from runners.paper_api_runner import run_paper_api
from runners.live_runner import run_live

def run_backtest(cfg_path: str, limit_bars: int = 0):
    bt_entry = os.path.join(os.getcwd(), "backtester_core.py")
    if not os.path.exists(bt_entry):
        raise SystemExit("backtester_core.py not found. Run from the backtester repo root.")
    cmd = [sys.executable, bt_entry, "--cfg", cfg_path]
    if limit_bars:
        cmd += ["--limit-bars", str(int(limit_bars))]
    print("[backtest] exec:", " ".join(cmd))
    os.execvp(sys.executable, cmd)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["backtest", "paper", "live"], required=True)
    ap.add_argument("--paper-source", choices=["db", "api"], default="db")
    ap.add_argument("--orders-db", default="")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--iterations", type=int, default=1)

    ap.add_argument("--cfg", required=True, help="YAML/JSON config with strategy_class and params")
    ap.add_argument("--db", help="Cache DB path for PAPER (db)")
    ap.add_argument("--results-dir", default="runner_results")
    ap.add_argument("--limit-bars", type=int, default=0)

    # Session & cache-out
    ap.add_argument("--session-db", default="")
    ap.add_argument("--cache-out", default="")
    ap.add_argument("--hour-cache", choices=["off","save","load"], default="off",
                    help="Current-bar cache: save (write) or load (read) features from cache_out for speed-up")

    # LIVE / PAPER-API params
    ap.add_argument("--env-file", default="")
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--symbol-format", default="usdtm")
    ap.add_argument("--poll-sec", type=int, default=15)
    ap.add_argument("--bar-delay-sec", type=int, default=10)
    ap.add_argument("--limit_klines", type=int, default=180)
    ap.add_argument("--debug", action="store_true")
    ap.add_argument("--heat-report", action="store_true", default=False,
                    help="Print nearest-to-entry (heat) when no entries open on a bar.")

    args = ap.parse_args()
    cfg = load_yaml_or_json(args.cfg)

    if args.mode == "backtest":
        return run_backtest(args.cfg, args.limit_bars if args.limit_bars > 0 else None)

    if args.mode == "paper":
        if args.paper_source == "api":
            return run_paper_api(cfg, args)
        else:
            if not args.db:
                raise SystemExit("--db is required for PAPER mode when --paper-source=db")
            raise SystemExit("paper --paper-source=db is not implemented in this thin CLI; use your original script.")

    # LIVE
    return run_live(cfg, args)

if __name__ == "__main__":
    main()
