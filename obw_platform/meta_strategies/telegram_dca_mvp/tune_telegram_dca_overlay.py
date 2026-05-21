#!/usr/bin/env python3
"""Night sweep for Telegram DCA MVP overlay parameters.

Research-only orchestration. It calls run_telegram_dca_mvp_npz.py with local
NPZ/CSV inputs and never touches live/order execution.
"""
from __future__ import annotations

import argparse
import csv
import itertools
import json
import subprocess
import sys
import time
from pathlib import Path


def score(row: dict) -> float:
    pnl = float(row.get("mtm_pnl_pct") or 0.0)
    mdd = abs(float(row.get("mtm_mdd_pct") or 0.0))
    trades = int(row.get("trades") or 0)
    if trades < 10:
        return -1e9
    return pnl * 100.0 - mdd * 35.0


def run_one(args, combo: dict, out_dir: Path) -> dict:
    run_dir = out_dir / f"run_{combo['idx']:04d}"
    initial = float(args.max_notional) / max(float(combo["total_mult"]), 1e-12)
    cmd = [
        sys.executable,
        "obw_platform/meta_strategies/telegram_dca_mvp/run_telegram_dca_mvp_npz.py",
        "--npz",
        args.npz,
        "--signals-csv",
        combo["signals_csv"],
        "--events",
        args.events,
        "--cfg",
        combo["cfg"],
        "--out-dir",
        str(run_dir),
        "--start-equity",
        str(args.start_equity),
        "--initial-notional",
        f"{initial:.8f}",
        "--meta-dca-adds",
        str(combo["adds"]),
        "--meta-dca-total-notional-mult",
        str(combo["total_mult"]),
        "--entry-mode",
        combo["entry_mode"],
        "--signal-ttl-hours",
        str(combo["ttl_hours"]),
        "--exit-at-tp",
        str(combo["exit_at_tp"]),
        "--tp-margin-weights",
        combo["tp_weights"],
        "--load-only-signal-symbols",
    ]
    if combo["ignore_lower_exits"]:
        cmd.append("--ignore-lower-exits")
    t0 = time.time()
    row = dict(combo)
    row["initial_notional"] = initial
    row["max_notional"] = args.max_notional
    row["run_dir"] = str(run_dir)
    row["error"] = ""
    try:
        proc = subprocess.run(cmd, cwd=args.workdir, text=True, capture_output=True, timeout=args.per_run_timeout)
        row["elapsed_sec"] = time.time() - t0
        if proc.returncode != 0:
            row["error"] = (proc.stderr or proc.stdout).strip()[:1000]
        else:
            summary = json.loads(proc.stdout)
            row.update(summary)
    except Exception as exc:
        row["elapsed_sec"] = time.time() - t0
        row["error"] = repr(exc)
    row["score"] = score(row)
    return row


def write_csv(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    keys: list[str] = []
    for row in rows:
        for key in row:
            if key not in keys:
                keys.append(key)
    with path.open("w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=keys)
        writer.writeheader()
        writer.writerows(rows)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", default=".")
    ap.add_argument("--npz", default="DB/telegram_signals_1m_event_windows_720h_bingx.npz")
    ap.add_argument("--events", default="DB/telegram_signal_standard_bt/telegram_channel_exit_events.csv")
    ap.add_argument("--out-dir", default="obw_platform/meta_strategies/telegram_dca_mvp/reports/night_tune_dca_overlay_20260520")
    ap.add_argument("--max-seconds", type=float, default=8 * 3600)
    ap.add_argument("--per-run-timeout", type=float, default=900)
    ap.add_argument("--start-equity", type=float, default=1000.0)
    ap.add_argument("--max-notional", type=float, default=100.0)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    configs = [
        "obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml",
        "obw_platform/configs/cfg_pack_dual_full_ena_maker_dca.yaml",
    ]
    signal_sets = [
        "obw_platform/meta_strategies/telegram_dca_mvp/reports/telegram_signals_raw_edge_min5.csv",
        "obw_platform/meta_strategies/telegram_dca_mvp/reports/telegram_signals_raw_and_dca_edge_min5.csv",
        "obw_platform/meta_strategies/telegram_dca_mvp/reports/causal_profitable_symbols_20260519/oos30_train_positive_anyn_signals.csv",
    ]
    combos = []
    for cfg, sigs, adds, total_mult, exit_at_tp, ttl_hours, ignore_lower in itertools.product(
        configs,
        signal_sets,
        [3, 5, 10],
        [1.5, 2.0, 2.5, 3.0],
        [2, 3],
        [72.0, 168.0],
        [True],
    ):
        combos.append(
            {
                "cfg": cfg,
                "signals_csv": sigs,
                "adds": adds,
                "total_mult": total_mult,
                "exit_at_tp": exit_at_tp,
                "ttl_hours": ttl_hours,
                "ignore_lower_exits": ignore_lower,
                "entry_mode": "close_in_zone",
                "tp_weights": "edge_in_zone",
            }
        )
    rows: list[dict] = []
    best: dict | None = None
    start = time.time()
    for idx, combo in enumerate(combos, 1):
        if time.time() - start >= args.max_seconds:
            break
        combo = dict(combo)
        combo["idx"] = idx
        row = run_one(args, combo, out_dir)
        rows.append(row)
        if best is None or float(row["score"]) > float(best["score"]):
            best = row
            (out_dir / "best.json").write_text(json.dumps(best, indent=2, ensure_ascii=False), encoding="utf-8")
        write_csv(out_dir / "tune_results.csv", rows)
        print(json.dumps({"idx": idx, "score": row["score"], "pnl": row.get("mtm_pnl_pct"), "mdd": row.get("mtm_mdd_pct"), "error": row.get("error", "")[:120]}, ensure_ascii=False), flush=True)
    summary = {"elapsed_sec": time.time() - start, "runs": len(rows), "best": best}
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2, ensure_ascii=False), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
