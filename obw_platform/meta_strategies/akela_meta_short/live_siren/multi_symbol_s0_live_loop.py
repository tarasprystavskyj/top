#!/usr/bin/env python3
"""Sequential S0 telemetry live loop for candidate symbols.

This coordinator creates temporary per-symbol configs from the SIREN S0
template and runs the existing live runner plus watchdog one symbol at a time.
It is intentionally conservative: no basket exposure, isolated live dirs, and
progression to the next symbol only after STOP_NEW_ORDERS with no live
positions or after the fill target is reached and positions are flat.
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
OBW = ROOT / "obw_platform"
BASE_CFG = OBW / "meta_strategies/akela_meta_short/live_siren/V21_siren_bingx_live_s0.yaml"
WATCHDOG = OBW / "meta_strategies/akela_meta_short/live_siren/siren_telemetry_watchdog.py"
RUNNER = OBW / "bt_live_paper_runner_separated_universe_4.py"
REPORT_ROOT = ROOT / "_reports/akela_meta_short/multi_symbol_s0_live"
LIVE_ROOT = OBW / "_reports/_live"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(symbol: str) -> str:
    return (
        symbol.replace("/USDT:USDT", "")
        .replace("/", "_")
        .replace(":", "_")
        .replace("-", "_")
        .lower()
    )


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def read_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def count_positions(live_dir: Path) -> int:
    positions = read_json(live_dir / "live_positions.json", {})
    if isinstance(positions, dict):
        return len(positions)
    return 0


def build_symbol_cfg(symbol: str, session_dir: Path, live_dir: Path, args) -> tuple[Path, Path]:
    cfg = yaml.safe_load(BASE_CFG.read_text(encoding="utf-8")) or {}
    sym_slug = slug(symbol)
    cfg["symbol"] = symbol
    cfg["live"] = True
    cfg["rollback_label"] = f"v21_{sym_slug}_s0_micro_telemetry"
    cfg.setdefault("runner", {})["mode_label"] = "S0_MICRO_TELEMETRY_LIVE"
    telemetry = cfg.setdefault("runner", {}).setdefault("s0_micro_telemetry", {})
    telemetry["enabled"] = True
    telemetry["symbol"] = symbol
    telemetry["fill_observation_target"] = int(args.fill_target)
    telemetry["approved_notional_cap_usdt"] = float(args.notional_cap)
    telemetry["approved_loss_budget_usdt"] = float(args.loss_budget)
    telemetry["spread_stop_bp"] = float(args.spread_stop_bp)
    telemetry["single_fill_adverse_slippage_stop_bp"] = float(args.single_fill_stop_bp)
    floor = telemetry.setdefault("dynamic_min_order_floor", {})
    floor["enabled"] = True
    floor["configured_min_order_usdt"] = float(args.min_order_usdt)
    floor["buffer"] = float(args.min_order_buffer)
    for side_key in ("strategy_params_long", "strategy_params_short"):
        cfg.setdefault(side_key, {})["minOrderUSDT"] = float(args.min_order_usdt)
        cfg[side_key]["liveStartTime"] = 0.0
    cfg.setdefault("universe", {})["allow"] = [symbol]
    cfg["symbols_whitelist"] = [symbol]
    cfg.setdefault("runner", {})["debug_console"] = False
    cfg.setdefault("runner", {})["debug_payload"] = False
    cfg["runner"].setdefault("console", {})["debug"] = False
    cfg["runner"]["console"]["debug_payload"] = False
    cfg["runner"]["console"]["error_summary"] = True

    cfg_path = session_dir / "cfg" / f"V21_{sym_slug}_s0_micro_telemetry.yaml"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")

    universe_path = session_dir / "universe" / f"universe_{sym_slug}.txt"
    universe_path.parent.mkdir(parents=True, exist_ok=True)
    universe_path.write_text(symbol + "\n", encoding="utf-8")

    (live_dir / "CONFIG_PATH.txt").parent.mkdir(parents=True, exist_ok=True)
    (live_dir / "CONFIG_PATH.txt").write_text(str(cfg_path) + "\n", encoding="utf-8")
    return cfg_path, universe_path


def terminate(proc: subprocess.Popen | None, timeout: float = 20.0) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return
        time.sleep(0.5)
    proc.kill()


def start_symbol(symbol: str, session_dir: Path, args) -> dict:
    sym_slug = slug(symbol)
    live_dir = LIVE_ROOT / f"bingx_{sym_slug}_v21_s0_{utc_stamp()}"
    cfg_path, universe_path = build_symbol_cfg(symbol, session_dir, live_dir, args)
    log_dir = session_dir / "logs" / sym_slug
    log_dir.mkdir(parents=True, exist_ok=True)
    runner_log = (log_dir / "runner.log").open("a", encoding="utf-8")
    watchdog_log = (log_dir / "watchdog.log").open("a", encoding="utf-8")

    runner_cmd = [
        sys.executable,
        str(RUNNER),
        "--mode",
        "live",
        "--cfg",
        str(cfg_path),
        "--exchange",
        "bingx",
        "--symbol-format",
        "usdtm",
        "--results-dir",
        str(live_dir),
        "--session-db",
        "session.sqlite",
        "--cache-out",
        "combined_cache_session.db",
        "--universe-file",
        str(universe_path),
        "--allow-symbols",
        symbol,
        "--poll-sec",
        str(args.poll_sec),
        "--bar-delay-sec",
        str(args.bar_delay_sec),
        "--limit_klines",
        str(args.limit_klines),
    ]
    if args.runner_debug:
        runner_cmd.append("--debug")
    watchdog_cmd = [
        sys.executable,
        str(WATCHDOG),
        "--cfg",
        str(cfg_path),
        "--live-dir",
        str(live_dir),
        "--interval-sec",
        str(args.watchdog_interval_sec),
    ]
    runner_log.write(f"[{now_iso()}] $ {' '.join(runner_cmd)}\n")
    watchdog_log.write(f"[{now_iso()}] $ {' '.join(watchdog_cmd)}\n")
    runner_log.flush()
    watchdog_log.flush()
    runner = subprocess.Popen(runner_cmd, cwd=str(OBW), stdout=runner_log, stderr=subprocess.STDOUT, text=True)
    watchdog = subprocess.Popen(watchdog_cmd, cwd=str(ROOT), stdout=watchdog_log, stderr=subprocess.STDOUT, text=True)
    return {
        "symbol": symbol,
        "live_dir": str(live_dir),
        "cfg": str(cfg_path),
        "universe": str(universe_path),
        "runner_pid": runner.pid,
        "watchdog_pid": watchdog.pid,
        "runner": runner,
        "watchdog": watchdog,
        "runner_log_handle": runner_log,
        "watchdog_log_handle": watchdog_log,
        "started_at": now_iso(),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="Sequential S0 micro-live telemetry loop")
    ap.add_argument("--symbols", required=True, help="Comma-separated symbols, e.g. AVAX/USDT:USDT,XRP/USDT:USDT")
    ap.add_argument("--fill-target", type=int, default=30)
    ap.add_argument("--notional-cap", type=float, default=18.0)
    ap.add_argument("--loss-budget", type=float, default=3.0)
    ap.add_argument("--min-order-usdt", type=float, default=2.02)
    ap.add_argument("--min-order-buffer", type=float, default=1.10)
    ap.add_argument("--spread-stop-bp", type=float, default=50.0)
    ap.add_argument("--single-fill-stop-bp", type=float, default=100.0)
    ap.add_argument("--poll-sec", type=int, default=2)
    ap.add_argument("--bar-delay-sec", type=int, default=1)
    ap.add_argument("--limit-klines", type=int, default=360)
    ap.add_argument("--watchdog-interval-sec", type=float, default=5.0)
    ap.add_argument("--max-symbol-minutes", type=float, default=180.0)
    ap.add_argument("--idle-after-stop-sec", type=float, default=20.0)
    ap.add_argument("--runner-debug", action="store_true")
    args = ap.parse_args()

    symbols = [s.strip() for s in args.symbols.split(",") if s.strip()]
    if not symbols:
        raise SystemExit("No symbols provided")
    session_dir = REPORT_ROOT / utc_stamp()
    session_dir.mkdir(parents=True, exist_ok=False)
    state_path = session_dir / "state.json"
    state = {
        "schema": "multi_symbol_s0_live_loop_v1",
        "started_at": now_iso(),
        "symbols": symbols,
        "session_dir": str(session_dir),
        "runs": [],
    }
    write_json(state_path, state)

    for symbol in symbols:
        run = start_symbol(symbol, session_dir, args)
        public_run = {k: v for k, v in run.items() if not k.endswith("_handle") and k not in {"runner", "watchdog"}}
        state["current"] = public_run
        state["runs"].append(public_run)
        write_json(state_path, state)
        live_dir = Path(run["live_dir"])
        started = time.time()
        stop_seen_at = None
        try:
            while True:
                status = read_json(live_dir / "WATCHDOG_STATUS.json", {})
                fills = int(status.get("fills_open") or 0)
                positions_count = count_positions(live_dir)
                stop_exists = (live_dir / "STOP_NEW_ORDERS").exists()
                runner_rc = run["runner"].poll()
                watchdog_rc = run["watchdog"].poll()
                public_run.update(
                    {
                        "last_update_at": now_iso(),
                        "fills_open": fills,
                        "positions_count": positions_count,
                        "stop_new_orders": bool(stop_exists or status.get("stop_new_orders")),
                        "runner_rc": runner_rc,
                        "watchdog_rc": watchdog_rc,
                        "status": status,
                    }
                )
                write_json(state_path, state)
                if stop_exists and positions_count == 0:
                    if stop_seen_at is None:
                        stop_seen_at = time.time()
                    if time.time() - stop_seen_at >= float(args.idle_after_stop_sec):
                        public_run["finish_reason"] = "stop_new_orders_flat"
                        break
                else:
                    stop_seen_at = None
                if fills >= int(args.fill_target) and positions_count == 0:
                    (live_dir / "STOP_NEW_ORDERS").write_text(
                        f"fill target reached by coordinator: {fills} >= {args.fill_target}\n",
                        encoding="utf-8",
                    )
                    public_run["finish_reason"] = "fill_target_flat"
                    break
                if time.time() - started > float(args.max_symbol_minutes) * 60.0:
                    (live_dir / "STOP_NEW_ORDERS").write_text(
                        f"max symbol runtime reached by coordinator: {args.max_symbol_minutes}m\n",
                        encoding="utf-8",
                    )
                    if positions_count == 0:
                        public_run["finish_reason"] = "max_runtime_flat"
                        break
                if runner_rc is not None:
                    public_run["finish_reason"] = f"runner_exit_{runner_rc}"
                    break
                if watchdog_rc is not None:
                    public_run["finish_reason"] = f"watchdog_exit_{watchdog_rc}"
                    break
                time.sleep(max(2.0, float(args.watchdog_interval_sec)))
        finally:
            terminate(run["watchdog"])
            terminate(run["runner"])
            run["runner_log_handle"].close()
            run["watchdog_log_handle"].close()
            public_run["finished_at"] = now_iso()
            public_run["final_status"] = read_json(live_dir / "WATCHDOG_STATUS.json", {})
            write_json(state_path, state)

    state.pop("current", None)
    state["finished_at"] = now_iso()
    write_json(state_path, state)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
