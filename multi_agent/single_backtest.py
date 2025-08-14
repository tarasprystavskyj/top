#!/usr/bin/env python3
import argparse
import importlib.util
import sys
import yaml
import os

def load_strategy_module(path):
    spec = importlib.util.spec_from_file_location("strategy", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["strategy"] = mod
    spec.loader.exec_module(mod)
    return mod

def main():
    parser = argparse.ArgumentParser(description="Run single backtest")
    parser.add_argument("--config", required=True, help="YAML config path")
    parser.add_argument("--cache-db", required=True, help="Combined cache DB file")
    parser.add_argument("--results-db", required=True, help="Output results DB")
    parser.add_argument("--strategy", required=True, help="Path to strategy .py file")
    parser.add_argument("--trades-csv", default="trades.csv", help="CSV to save trades")
    args = parser.parse_args()

    # Load config
    with open(args.config, "r") as f:
        config = yaml.safe_load(f)

    # Inject overrides
    config["cache_db"] = args.cache_db
    config["results_db"] = args.results_db
    config["trades_csv"] = args.trades_csv

    # Load strategy script
    strategy = load_strategy_module(args.strategy)

    if not hasattr(strategy, "run_backtest"):
        print(f"ERROR: Strategy file {args.strategy} has no run_backtest(config) function.")
        sys.exit(1)

    # Run
    print(f"Running backtest for {args.config} ...")
    summary = strategy.run_backtest(config)

    print("\n=== BACKTEST SUMMARY ===")
    for k, v in summary.items():
        print(f"{k}: {v}")

if __name__ == "__main__":
    main()
