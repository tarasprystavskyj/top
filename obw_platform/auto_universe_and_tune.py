
#!/usr/bin/env python3
# auto_universe_and_tune_fixed.py
# - Default --driver points to a BACKTESTER script (backtester_core_speed3.py), not a strategy module.
# - If user passes a driver that looks like a strategy file (e.g., contains "strategies/"),
#   we warn and ignore it unless --force-driver is set.
# - Everything else remains as in your workflow: build universe -> run RAYS (and optional grid).

import argparse, subprocess, sys, shlex, os

def run(cmd: str) -> None:
    print("\\n>>>", cmd)
    res = subprocess.run(cmd, shell=True)
    if res.returncode != 0:
        print(f"[ERR] command failed with code {res.returncode}")
        sys.exit(res.returncode)

def resolve_driver(driver: str, force: bool) -> str:
    # Heuristic: if the driver path contains "strategies/" it's almost certainly a strategy class file,
    # not an executable backtester. In that case, prefer a real backtester script.
    if ("strategies/" in driver.replace("\\\\", "/")) and not force:
        print(f"[WARN] The provided --driver looks like a strategy module: {driver}")
        print("[WARN] A backtester script is required here. Falling back to 'backtester_core_speed3.py'.")
        return "backtester_core_speed3.py"
    # If the given driver doesn't exist locally, keep it (runner may resolve it relative to CWD).
    return driver

def main():
    ap = argparse.ArgumentParser(description="Pipeline: build universe from trades -> RAYS sweeps -> optional GRID refine")
    # Universe build
    ap.add_argument("--trades", default="trades.csv", help="CSV with trades to derive the universe")
    ap.add_argument("--universe-out", dest="universe_out", default="universe_prof_v2.txt", help="Output file with symbols")
    ap.add_argument("--min-trades", dest="min_trades", type=int, default=8)
    ap.add_argument("--min-pf", dest="min_pf", type=float, default=1.05)
    ap.add_argument("--top-k", dest="top_k", type=int, default=80)

    # Backtest base
    ap.add_argument("--cfg", required=True, help="Base YAML for backtester")
    ap.add_argument("--limit-bars", dest="limit_bars", type=int, default=5000)
    ap.add_argument("--prefix", default="t5k_auto")
    ap.add_argument("--plots", default=None, help="Plots output dir (forwarded)")

    # Rays value lists
    ap.add_argument("--adx-values", dest="adx_values", default="15,20,25")
    ap.add_argument("--atr-values", dest="atr_values", default="0.020,0.022,0.024")
    ap.add_argument("--mom-values", dest="mom_values", default="0.020,0.022,0.024")
    ap.add_argument("--topn-values", dest="topn_values", default="8,10,12")

    # TP/SL (forwarded to driver if supported)
    ap.add_argument("--tp-values",  dest="tp_values", default=None, help="Optional TP values (comma-list) to forward")
    ap.add_argument("--sl-values",  dest="sl_values", default=None, help="Optional SL values (comma-list) to forward")

    # Final refine
    ap.add_argument("--grid", action="store_true", help="Run additional refine passes for all key params")

    # Runner/driver knobs
    ap.add_argument("--runner", default="grid_runner_ultrafast_3.py", help="Rays/Grid runner script")
    ap.add_argument("--driver", default="backtester_core_speed3_veto_universe.py", help="**Backtester** script called by the runner (NOT a strategy file)")
    ap.add_argument("--force-driver", action="store_true", help="Force using the provided --driver even if it looks like a strategy module")

    args = ap.parse_args()

    # 1) Build universe from trades
    run(f"{shlex.quote(sys.executable)} gen_universe_from_trades.py "
        f"--trades {shlex.quote(args.trades)} --out {shlex.quote(args.universe_out)} "
        f"--min-trades {args.min_trades} --min-pf {args.min_pf} --top-k {args.top_k}")

    # Resolve driver (avoid passing a strategy file by mistake)
    driver = resolve_driver(args.driver, args.force_driver)

    # Common base for runner
    base = f"--cfg {shlex.quote(args.cfg)} --limit-bars {args.limit_bars} " \
           f"--symbols-file {shlex.quote(args.universe_out)} "
    if args.plots:
        base += f"--plots {shlex.quote(args.plots)} "
    base += f"--driver {shlex.quote(driver)} "

    # Helper to form a rays command
    def rays(param_path: str, values_csv: str, tag: str) -> None:
        if not values_csv:
            return
        # Always include value '0' to test the configuration without this filter.
        vals = [v.strip() for v in values_csv.split(',') if v.strip()]
        if '0' not in vals:
            vals.insert(0, '0')
        values_csv = ','.join(vals)
        cmd = (
            f"{shlex.quote(sys.executable)} {shlex.quote(args.runner)} "
            f"--mode rays {base} "
            f"--out-prefix {shlex.quote(args.prefix)}_{tag} "
            f"--param {shlex.quote(param_path)} "
            f"--values {shlex.quote(values_csv)}"
        )
        if args.tp_values: cmd += f" --tp {shlex.quote(args.tp_values)}"
        if args.sl_values: cmd += f" --sl {shlex.quote(args.sl_values)}"
        run(cmd)

    # 2) RAYS passes
    rays("strategy_params.adx_threshold", args.adx_values, "rays_adx")
    rays("min_atr_ratio",                args.atr_values, "rays_atr")
    rays("min_momentum_sum",             args.mom_values, "rays_mom")
    rays("top-n",                        args.topn_values, "rays_topn")

    # 3) Optional "grid" — do a second sweep pass for all params (acts as local refine)
    if args.grid:
        rays("strategy_params.adx_threshold", args.adx_values, "grid_adx")
        rays("min_atr_ratio",                args.atr_values, "grid_atr")
        rays("min_momentum_sum",             args.mom_values, "grid_mom")
        rays("top-n",                        args.topn_values, "grid_topn")

    print("\\n[done] Universe built and tuning passes finished. Check tmp_cfgs/*.yaml and your results/plots.")

if __name__ == "__main__":
    main()
