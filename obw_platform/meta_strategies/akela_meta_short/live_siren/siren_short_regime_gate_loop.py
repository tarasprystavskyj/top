#!/usr/bin/env python3
"""Research loop for SIREN dual-with-short-regime-gate.

Runs bounded strategy-level experiments using the regime-gated short subclass.
This script writes only to report folders and temporary YAML files.
"""

from __future__ import annotations

import json
import subprocess
import sys
import time
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[4]
BASE_CFG = ROOT / "obw_platform/meta_strategies/akela_meta_short/live_siren/V21_siren_bingx_live_s0.yaml"
NPZ = ROOT / "DB/siren_bingx_1m_1y.npz"
BT = ROOT / "obw_platform/backtester_dual_long_short_fast_pack_v2.py"
REPORT_ROOT = ROOT / "_reports/akela_meta_short/siren_short_regime_gate"
SYMBOL = "SIREN/USDT:USDT"


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def deep_set(d: dict, dotted: str, value) -> None:
    cur = d
    parts = dotted.split(".")
    for part in parts[:-1]:
        cur = cur.setdefault(part, {})
    cur[parts[-1]] = value


def extract_json(text: str) -> dict:
    start = text.rfind("\n{")
    if start >= 0:
        start += 1
    else:
        start = text.find("{")
    if start < 0:
        return {"error": "no_json", "raw_tail": text[-1200:]}
    return json.loads(text[start:])


def score(metrics: dict) -> float:
    if metrics.get("error") or int(metrics.get("rc") or 0) != 0:
        return -1e30
    mc = int(metrics.get("margin_call_events_total") or 0)
    bars_mc = int(metrics.get("bars_in_margin_call") or 0)
    ret = float(metrics.get("return_mtm_pct_on_start") or -9999.0)
    mdd = abs(float(metrics.get("mdd_mtm_%") or 0.0))
    tail = abs(float(metrics.get("terminal_unrealized_to_realized_ratio") or 0.0))
    return ret * 1000.0 - mc * 1_000_000.0 - bars_mc * 100.0 - mdd * 50.0 - tail * 1000.0


def compact(m: dict) -> dict:
    keys = [
        "name",
        "window",
        "rc",
        "score",
        "return_mtm_pct_on_start",
        "total_pnl_mtm",
        "realized_pnl_total",
        "unrealized_pnl_total",
        "terminal_unrealized_to_realized_ratio",
        "mdd_mtm_%",
        "margin_call_events_total",
        "bars_in_margin_call",
        "trades_total",
        "trades_long",
        "trades_short",
        "elapsed_sec",
        "cfg",
        "log",
    ]
    return {k: m.get(k) for k in keys if k in m}


def build_cfg(base: dict, updates: dict) -> dict:
    cfg = deepcopy(base)
    cfg["live"] = False
    cfg["cache_db"] = ""
    cfg["strategy_class_long"] = "strategies.cryptomine_pack_dual_regime_gate.CryptomineLongPackRegimeGate"
    cfg["strategy_class_short"] = "strategies.cryptomine_pack_dual_regime_gate.CryptomineShortPackRegimeGate"
    cfg.setdefault("backtest", {}).setdefault("slippage", {})["enabled"] = True
    cfg["backtest"]["slippage"]["mode"] = "static"
    cfg["backtest"]["slippage"]["static_bp"] = 6.32
    cfg["backtest"]["slippage"]["note"] = "temporary SIREN short-regime-gate research, live p95 provisional"
    cfg["portfolio"]["slippage_per_side"] = 6.32 / 10000.0
    sp = cfg.setdefault("strategy_params_short", {})
    sp.setdefault("shortGateEnabled", True)
    sp.setdefault("shortGateEntryStrengthMin", 65.0)
    sp.setdefault("shortGateDcaStrengthMin", 65.0)
    for key, value in updates.items():
        deep_set(cfg, key, value)
    return cfg


def run_backtest(session: Path, base: dict, name: str, updates: dict, window: int, timeout_s: int) -> dict:
    cfg = build_cfg(base, updates)
    cfg_path = session / "cfg" / f"{name}_lb{window or 'full'}.yaml"
    log_path = session / "logs" / f"{name}_lb{window or 'full'}.log"
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    cmd = [
        sys.executable,
        str(BT),
        "--cfg",
        str(cfg_path),
        "--npz",
        str(NPZ),
        "--symbol",
        SYMBOL,
    ]
    if window:
        cmd += ["--limit-bars", str(window)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        text = proc.stdout
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") + f"\nTIMEOUT after {timeout_s}s\n"
        rc = 124
    log_path.write_text("$ " + " ".join(cmd) + "\n" + text, encoding="utf-8")
    try:
        metrics = extract_json(text)
    except Exception as exc:
        metrics = {"error": repr(exc), "raw_tail": text[-2000:]}
    metrics.update({"name": name, "window": window or "full", "rc": rc, "cfg": str(cfg_path), "log": str(log_path)})
    metrics["score"] = score(metrics)
    return compact(metrics)


def run_cfg_path(session: Path, name: str, cfg_path: Path, window: int, timeout_s: int) -> dict:
    log_path = session / "logs" / f"{name}_lb{window or 'full'}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        str(BT),
        "--cfg",
        str(cfg_path),
        "--npz",
        str(NPZ),
        "--symbol",
        SYMBOL,
    ]
    if window:
        cmd += ["--limit-bars", str(window)]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
        )
        text = proc.stdout
        rc = proc.returncode
    except subprocess.TimeoutExpired as exc:
        text = (exc.stdout or "") + f"\nTIMEOUT after {timeout_s}s\n"
        rc = 124
    log_path.write_text("$ " + " ".join(cmd) + "\n" + text, encoding="utf-8")
    try:
        metrics = extract_json(text)
    except Exception as exc:
        metrics = {"error": repr(exc), "raw_tail": text[-2000:]}
    metrics.update({"name": name, "window": window or "full", "rc": rc, "cfg": str(cfg_path), "log": str(log_path)})
    metrics["score"] = score(metrics)
    return compact(metrics)


def write_latest(session: Path, rows: list[dict], phase: str) -> None:
    ordered = sorted(rows, key=lambda r: float(r.get("score") or -1e30), reverse=True)
    lines = [
        "# SIREN Short Regime Gate Research",
        "",
        "production_ready=false",
        "",
        f"Session: `{session}`",
        f"Phase: `{phase}`",
        "",
        "## Current Best",
    ]
    if ordered:
        b = ordered[0]
        lines += [
            f"- `{b.get('name')}` window `{b.get('window')}`",
            f"- margin calls: `{b.get('margin_call_events_total')}` bars: `{b.get('bars_in_margin_call')}`",
            f"- MTM return: `{b.get('return_mtm_pct_on_start')}`",
            f"- tail ratio: `{b.get('terminal_unrealized_to_realized_ratio')}`",
            f"- cfg: `{b.get('cfg')}`",
        ]
    lines += [
        "",
        "## Results",
        "",
        "| name | window | mc | mc bars | ret mtm % | mdd % | tail | score |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for r in ordered[:30]:
        lines.append(
            f"| {r.get('name')} | {r.get('window')} | {r.get('margin_call_events_total')} | "
            f"{r.get('bars_in_margin_call')} | {r.get('return_mtm_pct_on_start')} | "
            f"{r.get('mdd_mtm_%')} | {r.get('terminal_unrealized_to_realized_ratio')} | {r.get('score')} |"
        )
    (session / "latest.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    session = REPORT_ROOT / utc_stamp()
    session.mkdir(parents=True, exist_ok=False)
    base = yaml.safe_load(BASE_CFG.read_text(encoding="utf-8"))
    rows: list[dict] = []
    variants = {
        "gate65_close50_loss2": {
            "strategy_params_short.shortGateEntryStrengthMin": 65.0,
            "strategy_params_short.shortGateDcaStrengthMin": 65.0,
            "strategy_params_short.shortGateCloseStrengthMin": 50.0,
            "strategy_params_short.shortGateCloseUnrealPct": 2.0,
            "strategy_params_short.shortGateCloseMinFills": 1,
            "strategy_params_short.equityForSizingUSDT": 80,
            "strategy_params_short.baseOrderPctEq": 0.35,
            "strategy_params_short.maxShortInvestPct": 0.20,
            "strategy_params_short.minShortInvestPct": 0.05,
            "strategy_params_short.marginCallLimit": 20,
            "strategy_params_short.maxFillsPerBar": 1,
            "strategy_params_long.maxLongInvestPct": 1.5,
        },
        "gate75_close60_loss1": {
            "strategy_params_short.shortGateEntryStrengthMin": 75.0,
            "strategy_params_short.shortGateDcaStrengthMin": 75.0,
            "strategy_params_short.shortGateCloseStrengthMin": 60.0,
            "strategy_params_short.shortGateCloseUnrealPct": 1.0,
            "strategy_params_short.shortGateCloseMinFills": 1,
            "strategy_params_short.equityForSizingUSDT": 80,
            "strategy_params_short.baseOrderPctEq": 0.30,
            "strategy_params_short.maxShortInvestPct": 0.15,
            "strategy_params_short.minShortInvestPct": 0.03,
            "strategy_params_short.marginCallLimit": 16,
            "strategy_params_short.maxFillsPerBar": 1,
            "strategy_params_long.maxLongInvestPct": 1.5,
        },
        "gate85_close75_force": {
            "strategy_params_short.shortGateEntryStrengthMin": 85.0,
            "strategy_params_short.shortGateDcaStrengthMin": 85.0,
            "strategy_params_short.shortGateCloseStrengthMin": 75.0,
            "strategy_params_short.shortGateCloseUnrealPct": 0.0,
            "strategy_params_short.shortGateCloseMinFills": 1,
            "strategy_params_short.equityForSizingUSDT": 60,
            "strategy_params_short.baseOrderPctEq": 0.25,
            "strategy_params_short.maxShortInvestPct": 0.10,
            "strategy_params_short.minShortInvestPct": 0.02,
            "strategy_params_short.marginCallLimit": 12,
            "strategy_params_short.maxFillsPerBar": 1,
            "strategy_params_long.maxLongInvestPct": 1.5,
        },
        "daily_gate70_wide": {
            "strategy_params_short.trendMaTf": "D",
            "strategy_params_short.trendMaLen": 8,
            "strategy_params_short.trendSlopeBars": 2,
            "strategy_params_short.trendSlopeLongBoundPct": 0.20,
            "strategy_params_short.trendSlopeShortBoundPct": -0.80,
            "strategy_params_short.shortGateEntryStrengthMin": 70.0,
            "strategy_params_short.shortGateDcaStrengthMin": 75.0,
            "strategy_params_short.shortGateCloseStrengthMin": 65.0,
            "strategy_params_short.shortGateCloseUnrealPct": 1.0,
            "strategy_params_short.equityForSizingUSDT": 80,
            "strategy_params_short.baseOrderPctEq": 0.30,
            "strategy_params_short.maxShortInvestPct": 0.15,
            "strategy_params_short.marginCallLimit": 16,
            "strategy_params_short.maxFillsPerBar": 1,
            "strategy_params_long.maxLongInvestPct": 1.5,
        },
        "weekly_gate80_close70": {
            "strategy_params_short.trendMaTf": "W",
            "strategy_params_short.trendMaLen": 8,
            "strategy_params_short.trendSlopeBars": 1,
            "strategy_params_short.trendSlopeLongBoundPct": 0.20,
            "strategy_params_short.trendSlopeShortBoundPct": -0.80,
            "strategy_params_short.shortGateEntryStrengthMin": 80.0,
            "strategy_params_short.shortGateDcaStrengthMin": 85.0,
            "strategy_params_short.shortGateCloseStrengthMin": 70.0,
            "strategy_params_short.shortGateCloseUnrealPct": 1.0,
            "strategy_params_short.equityForSizingUSDT": 80,
            "strategy_params_short.baseOrderPctEq": 0.30,
            "strategy_params_short.maxShortInvestPct": 0.15,
            "strategy_params_short.marginCallLimit": 16,
            "strategy_params_short.maxFillsPerBar": 1,
            "strategy_params_long.maxLongInvestPct": 1.5,
        },
    }
    windows = [50_000, 150_000]
    for window in windows:
        for name, updates in variants.items():
            rows.append(run_backtest(session, base, name, updates, window, timeout_s=240))
            (session / "results.json").write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
            write_latest(session, rows, phase=f"window_{window}")

    clean = [r for r in rows if r.get("window") == 150_000 and int(r.get("margin_call_events_total") or 0) == 0]
    shortlist = clean[:]
    if not shortlist:
        shortlist = sorted([r for r in rows if r.get("window") == 150_000], key=lambda r: float(r.get("score") or -1e30), reverse=True)[:2]
    for item in shortlist[:3]:
        cfg = Path(str(item["cfg"]))
        name = f"{item['name']}_full"
        rows.append(run_cfg_path(session, name, cfg, 0, timeout_s=900))
        (session / "results.json").write_text(json.dumps(rows, indent=2, sort_keys=True), encoding="utf-8")
        write_latest(session, rows, phase="full_confirm")
        time.sleep(1)

    write_latest(session, rows, phase="finished")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
