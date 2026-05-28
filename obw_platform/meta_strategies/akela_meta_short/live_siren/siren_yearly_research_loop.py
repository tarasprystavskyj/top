#!/usr/bin/env python3
"""Offline SIREN yearly research loop.

Collects yearly SIREN data, builds temporary backtest configs from live
telemetry slippage, runs backtests, and starts a bounded tuner. This script does
not modify production YAML and does not interact with live trading.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
LIVE_DIR = ROOT / "obw_platform/_reports/_live/bingx_siren_v21_s0"
BASE_CFG = ROOT / "obw_platform/meta_strategies/akela_meta_short/live_siren/V21_siren_bingx_live_s0.yaml"
UNIVERSE = ROOT / "obw_platform/universe/universe_siren_live.txt"
REPORT_ROOT = ROOT / "_reports/akela_meta_short/siren_yearly_research"
NPZ_1D = ROOT / "DB/siren_bingx_1d_365b.npz"
NPZ_1M = ROOT / "DB/siren_bingx_1m_1y.npz"
SYMBOL = "SIREN/USDT:USDT"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def run_cmd(cmd: list[str], log_path: Path, state: dict, step: str) -> int:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    state.update({"step": step, "step_started_at": utc_now(), "last_cmd": cmd})
    write_json(Path(state["state_path"]), state)
    with log_path.open("a", encoding="utf-8") as log:
        log.write(f"\n[{utc_now()}] $ {' '.join(cmd)}\n")
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, text=True)
        rc = proc.wait()
        log.write(f"[{utc_now()}] rc={rc}\n")
    state.setdefault("steps", []).append({"step": step, "rc": rc, "finished_at": utc_now(), "log": str(log_path)})
    write_json(Path(state["state_path"]), state)
    return rc


def load_live_slippage() -> dict:
    cal_path = LIVE_DIR / "live_slippage_calibration.json"
    if cal_path.exists():
        payload = json.loads(cal_path.read_text(encoding="utf-8"))
        fit = payload.get("fit") or {}
    else:
        fit = {}
    db_path = LIVE_DIR / "session.sqlite"
    if db_path.exists():
        with sqlite3.connect(str(db_path)) as con:
            rows = con.execute(
                "select strategy_side, order_action, order_direction, actual_adverse_bp, snapshot_spread_bp "
                "from slippage_observations"
            ).fetchall()
        vals = [float(r[3]) for r in rows if r[3] is not None]
        if vals:
            vals_sorted = sorted(vals)
            idx95 = min(len(vals_sorted) - 1, int(round((len(vals_sorted) - 1) * 0.95)))
            groups: dict[str, list[float]] = {}
            for side, action, direction, adverse, _spread in rows:
                if adverse is None:
                    continue
                groups.setdefault(f"{side}|{action}|{direction}", []).append(float(adverse))
            fit.update(
                {
                    "n": len(vals),
                    "mean_adverse_bp": sum(vals) / len(vals),
                    "median_adverse_bp": vals_sorted[len(vals_sorted) // 2],
                    "p95_adverse_bp": vals_sorted[idx95],
                    "max_adverse_bp": max(vals),
                    "by_group": {
                        k: {
                            "n": len(v),
                            "mean_adverse_bp": sum(v) / len(v),
                            "max_adverse_bp": max(v),
                        }
                        for k, v in sorted(groups.items())
                    },
                }
            )
    n = int(fit.get("n") or 0)
    static_bp = float(fit.get("static_suggestion_bp") or fit.get("mean_nonnegative_adverse_bp") or fit.get("mean_adverse_bp") or 3.968746124271661)
    p95_bp = float(fit.get("p95_adverse_bp") or fit.get("p95_abs_bp") or fit.get("max_adverse_bp") or static_bp)
    return {
        "symbol": SYMBOL,
        "ts_utc": utc_now(),
        "status": "final_candidate" if n >= 30 else "provisional_not_enough_for_final",
        "min_recommended_n": 30,
        "fit": fit,
        "suggested_static_bp": static_bp,
        "suggested_stress_bp": max(p95_bp, static_bp),
    }


def build_cfgs(session_dir: Path, model: dict) -> dict[str, Path]:
    base = yaml.safe_load(BASE_CFG.read_text(encoding="utf-8"))
    cfg_dir = session_dir / "cfg"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    out = {}
    variants = {
        "base_yaml": None,
        "live_static": float(model["suggested_static_bp"]),
        "live_p95": float(model["suggested_stress_bp"]),
    }
    for name, static_bp in variants.items():
        cfg = json.loads(json.dumps(base))
        cfg["live"] = False
        cfg["cache_db"] = ""
        cfg.setdefault("backtest", {}).setdefault("slippage", {})["enabled"] = True
        cfg["backtest"]["slippage"]["mode"] = "static"
        if static_bp is not None:
            cfg["backtest"]["slippage"]["static_bp"] = static_bp
            cfg["backtest"]["slippage"]["note"] = f"temporary SIREN research config from live telemetry: {name}"
            cfg["portfolio"]["slippage_per_side"] = static_bp / 10000.0
        path = cfg_dir / f"V21_siren_{name}.yaml"
        path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
        out[name] = path
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Run offline SIREN yearly data/backtest/tune loop")
    ap.add_argument("--max-cycles", type=int, default=int(os.environ.get("SIREN_RESEARCH_MAX_CYCLES", "1")))
    ap.add_argument("--sleep-seconds", type=float, default=float(os.environ.get("SIREN_RESEARCH_SLEEP_SECONDS", "300")))
    ap.add_argument("--tune-seconds", type=float, default=float(os.environ.get("SIREN_RESEARCH_TUNE_SECONDS", "3600")))
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("SIREN_RESEARCH_JOBS", "1")))
    args = ap.parse_args()

    session_dir = REPORT_ROOT / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    session_dir.mkdir(parents=True, exist_ok=False)
    state = {
        "schema": "siren_yearly_research_loop_v1",
        "started_at": utc_now(),
        "session_dir": str(session_dir),
        "state_path": str(session_dir / "state.json"),
        "cycles": [],
    }
    write_json(Path(state["state_path"]), state)

    for cycle in range(1, args.max_cycles + 1):
        state["cycle"] = cycle
        cycle_dir = session_dir / f"cycle_{cycle:03d}"
        cycle_dir.mkdir(parents=True, exist_ok=True)
        log_dir = cycle_dir / "logs"

        model = load_live_slippage()
        write_json(cycle_dir / "siren_slippage_model.json", model)
        cfgs = build_cfgs(cycle_dir, model)

        run_cmd(
            [
                sys.executable,
                "obw_platform/scripts/fetch_backfill_ohlcv_npz_from_now_v1.py",
                "-i",
                str(UNIVERSE),
                "-t",
                "1d",
                "--back-bars",
                "365",
                "--exchange",
                "bingx",
                "--ccxt-symbol-format",
                "usdtm",
                "--npz-out",
                str(NPZ_1D),
                "--npz-only",
                "--feature-set",
                "none",
            ],
            log_dir / "fetch_1d.log",
            state,
            "fetch_1d_365b",
        )

        run_cmd(
            [
                sys.executable,
                "obw_platform/scripts/fetch_backfill_ohlcv_npz_from_now_v1.py",
                "-i",
                str(UNIVERSE),
                "-t",
                "1m",
                "--back-bars",
                "525600",
                "--exchange",
                "bingx",
                "--ccxt-symbol-format",
                "usdtm",
                "--npz-out",
                str(NPZ_1M),
                "--npz-only",
                "--feature-set",
                "none",
            ],
            log_dir / "fetch_1m.log",
            state,
            "fetch_1m_1y",
        )

        backtests = {}
        for name, cfg_path in cfgs.items():
            bt_dir = cycle_dir / "backtests" / name
            rc = run_cmd(
                [
                    sys.executable,
                    "obw_platform/backtester_dual_long_short_fast_pack_v2.py",
                    "--cfg",
                    str(cfg_path),
                    "--npz",
                    str(NPZ_1M),
                    "--symbol",
                    SYMBOL,
                    "--plots",
                    str(bt_dir),
                    "--export-curves",
                    str(bt_dir / "curves.csv"),
                ],
                log_dir / f"backtest_{name}.log",
                state,
                f"backtest_{name}",
            )
            backtests[name] = {"rc": rc, "dir": str(bt_dir), "cfg": str(cfg_path)}

        tuner_rc = run_cmd(
            [
                sys.executable,
                "obw_platform/auto_tuner_dual_fast_pack.py",
                "--cfg",
                str(cfgs["live_p95"]),
                "--npz",
                str(NPZ_1M),
                "--symbol",
                SYMBOL,
                "--plan",
                "obw_platform/tuner_plans/tuner_plan_V21_live_candidates_1m_1y.py",
                "--prefix",
                "siren_live_p95_1m_1y",
                "--jobs",
                str(args.jobs),
                "--min-trades",
                "50",
                "--score-mode",
                "mtm",
                "--max-seconds",
                str(args.tune_seconds),
                "--debug",
            ],
            log_dir / "tuner_live_p95.log",
            state,
            "tuner_live_p95",
        )

        cycle_summary = {
            "cycle": cycle,
            "finished_at": utc_now(),
            "slippage_model": str(cycle_dir / "siren_slippage_model.json"),
            "npz_1d": str(NPZ_1D),
            "npz_1m": str(NPZ_1M),
            "backtests": backtests,
            "tuner_rc": tuner_rc,
        }
        state["cycles"].append(cycle_summary)
        write_json(cycle_dir / "cycle_summary.json", cycle_summary)
        write_json(Path(state["state_path"]), state)
        if cycle < args.max_cycles:
            time.sleep(args.sleep_seconds)

    state["finished_at"] = utc_now()
    state["step"] = "finished"
    write_json(Path(state["state_path"]), state)


if __name__ == "__main__":
    main()
