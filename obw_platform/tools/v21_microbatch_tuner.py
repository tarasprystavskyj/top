#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import csv
import json
import subprocess
import sys
import time
from pathlib import Path

import yaml


def deep_set(d, key, val):
    cur = d
    parts = key.split(".")
    for p in parts[:-1]:
        cur = cur.setdefault(p, {})
    cur[parts[-1]] = val


def deep_get(d, key):
    cur = d
    for p in key.split("."):
        cur = cur[p]
    return cur


def score(row):
    if row.get("error"):
        return -1e30
    if int(row.get("margin_calls") or 0) > 0:
        return -1e18
    mtm = float(row["mtm_pnl"])
    dd = abs(float(row["mdd_mtm_pct"]))
    realized_dd = abs(float(row.get("mdd_realized_pct") or 0.0))
    # Prioritize investable stability: MTM first, then DD discipline.
    return mtm * 500.0 - dd * 90.0 - realized_dd * 20.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--npz", required=True)
    ap.add_argument("--symbol", required=True)
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--time-from", default="")
    ap.add_argument("--time-to", default="")
    ap.add_argument("--max-seconds", type=float, default=3600.0)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    tmp_dir = out_dir / "tmp_cfgs"
    tmp_dir.mkdir(exist_ok=True)
    csv_path = out_dir / "micro_tuner_log.csv"
    best_path = out_dir / "best.yaml"
    summary_path = out_dir / "summary.json"

    base = yaml.safe_load(open(args.cfg, "r", encoding="utf-8"))
    candidates = [
        ("baseline", None, None),
        ("long_tp", "strategy_params_long.tpPercent", [-0.06, -0.04, -0.02, 0.0, 0.02, 0.04]),
        ("short_tp", "strategy_params_short.tpPercent", [-0.05, -0.03, -0.015, 0.0, 0.015, 0.03]),
        ("long_subtp", "strategy_params_long.subSellTPPercent", [-0.10, -0.06, -0.03, 0.0, 0.03, 0.06]),
        ("short_subtp", "strategy_params_short.subSellTPPercent", [-0.08, -0.05, -0.025, 0.0, 0.025, 0.05]),
        ("long_cb", "strategy_params_long.callbackPercent", [-0.025, -0.015, -0.0075, 0.0, 0.0075, 0.015]),
        ("short_cb", "strategy_params_short.callbackPercent", [-0.03, -0.02, -0.01, 0.0, 0.01, 0.02]),
        ("long_space", "strategy_params_long.linearDropPercent", [-0.015, -0.0075, 0.0, 0.0075, 0.015]),
        ("short_space", "strategy_params_short.linearRisePercent", [-0.04, -0.025, -0.0125, 0.0, 0.0125, 0.025]),
        ("long_cap", "strategy_params_long.maxLongInvestPct", [-0.25, -0.10, 0.0, 0.10]),
        ("short_cap", "strategy_params_short.maxShortInvestPct", [-0.35, -0.20, -0.10, 0.0, 0.10]),
    ]

    fields = [
        "idx", "stage", "param", "value", "score", "mtm_pnl", "mtm_pct",
        "realized", "unrealized", "mdd_mtm_pct", "mdd_realized_pct",
        "trades", "margin_calls", "cfg_path", "error",
    ]
    if not csv_path.exists():
        with csv_path.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writeheader()

    best_cfg = copy.deepcopy(base)
    best_row = None
    idx = 0
    start = time.time()

    def run_cfg(stage, param, value, cfg):
        nonlocal idx, best_row, best_cfg
        idx += 1
        cfg_path = tmp_dir / f"cand_{idx:04d}_{stage}.yaml"
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        cmd = [
            sys.executable, "obw_platform/backtester_dual_long_short_fast_pack_v2.py",
            "--cfg", str(cfg_path),
            "--npz", args.npz,
            "--symbol", args.symbol,
        ]
        if args.time_from:
            cmd += ["--time-from", args.time_from]
        if args.time_to:
            cmd += ["--time-to", args.time_to]
        row = {
            "idx": idx, "stage": stage, "param": param or "", "value": value if value is not None else "",
            "cfg_path": str(cfg_path), "error": "",
        }
        try:
            p = subprocess.run(cmd, text=True, capture_output=True, timeout=180)
            if p.returncode != 0:
                row["error"] = (p.stderr or p.stdout).strip()[:500]
            else:
                r = json.loads(p.stdout)
                row.update({
                    "mtm_pnl": r.get("total_pnl_mtm"),
                    "mtm_pct": r.get("return_mtm_pct_on_start"),
                    "realized": r.get("realized_pnl_total"),
                    "unrealized": r.get("unrealized_pnl_total"),
                    "mdd_mtm_pct": r.get("mdd_mtm_%"),
                    "mdd_realized_pct": r.get("mdd_realized_%"),
                    "trades": r.get("trades_total"),
                    "margin_calls": r.get("margin_call_events_total"),
                })
        except Exception as e:
            row["error"] = repr(e)
        row["score"] = score(row)
        with csv_path.open("a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fields).writerow({k: row.get(k, "") for k in fields})
        if best_row is None or row["score"] > best_row["score"]:
            best_row = dict(row)
            best_cfg = copy.deepcopy(cfg)
            best_path.write_text(yaml.safe_dump(best_cfg, sort_keys=False), encoding="utf-8")
            summary_path.write_text(json.dumps({"best": best_row, "elapsed_sec": time.time() - start}, indent=2), encoding="utf-8")
            print("[best]", json.dumps(best_row, ensure_ascii=False), flush=True)
        else:
            print("[row]", json.dumps(row, ensure_ascii=False), flush=True)

    run_cfg("baseline", None, None, best_cfg)
    for stage, param, deltas in candidates[1:]:
        cur = deep_get(best_cfg, param)
        values = sorted(set([cur] + [round(float(cur) + d, 10) for d in deltas]))
        for v in values:
            if time.time() - start >= args.max_seconds:
                summary_path.write_text(json.dumps({"best": best_row, "elapsed_sec": time.time() - start, "stopped": "max_seconds"}, indent=2), encoding="utf-8")
                return
            cfg = copy.deepcopy(best_cfg)
            deep_set(cfg, param, v)
            run_cfg(stage, param, v, cfg)

    summary_path.write_text(json.dumps({"best": best_row, "elapsed_sec": time.time() - start, "stopped": "complete"}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
