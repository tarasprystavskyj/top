#!/usr/bin/env python3
"""Paper/backtest-only V21 signal loop for Binance copy lead 4300516091842181632.

This loop follows the lead position side directly. The paper-live signal source
for this lead is Binance's current open positions endpoint, not closed-position
reversal or contrarian-on-close logic. Historical closed positions are used only
for backtest/tuning labels. No live orders are placed.
"""
from __future__ import annotations

import argparse
import copy
import csv
import json
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Tuple

import yaml


ROOT = Path(__file__).resolve().parents[3]
MODULE_DIR = ROOT / "obw_platform" / "meta_strategies" / "telegram_signal_dca"
REPORT_ROOT = MODULE_DIR / "reports"
DEFAULT_LEAD = "4300516091842181632"
SOURCE_URL = "https://www.binance.com/uk-UA/copy-trading/lead-details/4300516091842181632?timeRange=7D"
DEFAULT_OUT = REPORT_ROOT / "binance_430051_hype_v21_loop_20260523"
DEFAULT_CFG = ROOT / "obw_platform" / "configs" / "V21_strict_trend_stable_live_static9p38.yaml"
PHILOSOPHY = REPORT_ROOT / "CONSILIUM_LOOP_PHILOSOPHY_20260523.md"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_dt(raw: Any) -> datetime:
    dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def safe_slug(raw: str) -> str:
    return "".join(ch if ch.isalnum() or ch in ("-", "_") else "_" for ch in raw)[:120]


def run_cmd(cmd: List[str], log_path: Path, timeout_sec: float = 0.0) -> Dict[str, Any]:
    started = utc_now()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n[%s] $ %s\n" % (started, " ".join(cmd)))
        log.flush()
        proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=log, stderr=subprocess.STDOUT, text=True)
        try:
            rc = proc.wait(timeout=timeout_sec if timeout_sec > 0 else None)
            timed_out = False
        except subprocess.TimeoutExpired:
            proc.terminate()
            try:
                rc = proc.wait(timeout=30)
            except subprocess.TimeoutExpired:
                proc.kill()
                rc = proc.wait()
            timed_out = True
    return {
        "cmd": cmd,
        "started_at": started,
        "finished_at": utc_now(),
        "returncode": rc,
        "timed_out": timed_out,
        "log": str(log_path),
    }


def read_positions(path: Path) -> List[Dict[str, str]]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open("r", encoding="utf-8", newline="") as fp:
        return [
            dict(row)
            for row in csv.DictReader(fp)
            if str(row.get("symbol") or "").strip()
            and str(row.get("side") or "").upper() in {"LONG", "SHORT"}
            and row.get("opened_utc")
            and row.get("closed_utc")
        ]


def normalize_symbol(raw: str) -> str:
    s = str(raw).upper().strip()
    if "/" in s:
        return s.split("/", 1)[0] + "USDT"
    if not s.endswith("USDT"):
        return s + "USDT"
    return s


def detect_universe(rows: Iterable[Dict[str, str]], force_symbols: Iterable[str]) -> List[str]:
    out = {normalize_symbol(s) for s in force_symbols if str(s).strip()}
    for row in rows:
        out.add(normalize_symbol(str(row.get("symbol") or "")))
    return sorted(x for x in out if x and x != "USDT")


def side_counts(rows: Iterable[Dict[str, str]]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for row in rows:
        side = str(row.get("side") or "").upper()
        counts[side] = counts.get(side, 0) + 1
    return counts


def window_for(rows: List[Dict[str, str]], warmup_days: int) -> Tuple[datetime, datetime, datetime, datetime]:
    now = datetime.now(timezone.utc)
    annual_start = now - timedelta(days=365)
    if rows:
        pos_start = min(parse_dt(r["opened_utc"]) for r in rows)
        pos_end = max(parse_dt(r["closed_utc"]) for r in rows)
        start = min(annual_start, pos_start - timedelta(days=warmup_days))
        end = max(pos_end + timedelta(days=4), now)
        if end > now:
            end = now
        return start, end, pos_start, pos_end
    return annual_start, now, annual_start, now


def read_philosophy_notes() -> List[str]:
    if not PHILOSOPHY.exists():
        return ["missing CONSILIUM_LOOP_PHILOSOPHY_20260523.md"]
    text = PHILOSOPHY.read_text(encoding="utf-8", errors="replace")
    notes = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ") and any(key in s.lower() for key in ("one wave", "entry", "trend", "risk", "promote")):
            notes.append(s[2:])
    return notes[:10]


def set_path(obj: Dict[str, Any], dotted: str, value: Any) -> None:
    cur = obj
    parts = dotted.split(".")
    for key in parts[:-1]:
        cur = cur.setdefault(key, {})
    cur[parts[-1]] = value


def make_variants(base_cfg: Path, out_dir: Path, long_only: bool) -> List[Dict[str, Any]]:
    base = yaml.safe_load(base_cfg.read_text(encoding="utf-8"))
    base.setdefault("research_notes", {})
    base["research_notes"]["binance_430051_hype_signal_loop"] = {
        "paper_backtest_only": True,
        "source_url": SOURCE_URL,
        "paper_live_signal_source": "Binance current open positions tab/endpoint",
        "backtest_label_source": "lead side from Binance public closed position history",
        "entry_side": "direct lead side; never contrarian-on-close",
        "single_leg": True,
        "long_only_assumption": bool(long_only),
        "trend_reaction": "disabled for entries; consilium trend is diagnostics/warmup only",
    }
    specs = [
        ("baseline", {}),
        ("long_conservative_wide_grid", {
            "strategy_params_long.drop1": 0.55,
            "strategy_params_long.drop2": 0.75,
            "strategy_params_long.linearDropPercent": 0.16,
            "strategy_params_long.mult2": 1.0,
        }),
        ("long_balanced_hype_grid", {
            "strategy_params_long.drop1": 0.35,
            "strategy_params_long.drop2": 0.55,
            "strategy_params_long.linearDropPercent": 0.10,
            "strategy_params_long.mult2": 1.25,
        }),
        ("long_aggressive_second_leg", {
            "strategy_params_long.drop1": 0.25,
            "strategy_params_long.drop2": 0.40,
            "strategy_params_long.linearDropPercent": 0.07,
            "strategy_params_long.mult2": 1.8,
        }),
        ("long_low_exposure", {
            "strategy_params_long.baseOrderPctEq": 0.9,
            "strategy_params_long.maxLongInvestPct": 1.25,
            "strategy_params_long.drop1": 0.45,
            "strategy_params_long.mult2": 1.1,
        }),
        ("long_high_tp", {
            "strategy_params_long.tpPercent": 0.42,
            "strategy_params_long.subSellTPPercent": 0.72,
            "strategy_params_long.callbackPercent": 0.08,
            "strategy_params_long.drop1": 0.35,
        }),
    ]
    cfg_dir = out_dir / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    variants = []
    for name, changes in specs:
        cfg = copy.deepcopy(base)
        for key, value in changes.items():
            set_path(cfg, key, value)
        cfg_path = cfg_dir / ("%s.yaml" % safe_slug(name))
        cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")
        variants.append({"name": name, "cfg": str(cfg_path), "changes": changes})
    return variants


def best_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {"best_label": "", "score": 0.0, "summary": {}}
    data = json.loads(path.read_text(encoding="utf-8"))
    best_label = ""
    best_score = -1e30
    for label, row in data.items():
        score = float(row.get("net_pct_per_30d") or row.get("net_pct") or -1e30)
        score -= abs(float(row.get("max_dd_pct") or 0.0)) * 0.03
        if score > best_score:
            best_label = label
            best_score = score
    return {"best_label": best_label, "score": best_score, "summary": data.get(best_label, {})}


def write_status(out_dir: Path, status: Dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "STATUS.json").write_text(json.dumps(status, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# Binance 430051 HYPE V21 Loop",
        "",
        "Paper/backtest-only. No live orders. No `.env` secrets are read or printed.",
        "",
        "- Updated: `%s`" % status.get("updated_at", utc_now()),
        "- tmux session: `%s`" % status.get("tmux_session", ""),
        "- Lead: `%s`" % status.get("lead_id", ""),
        "- Source URL: `%s`" % status.get("source_url", ""),
        "- Current phase: `%s`" % status.get("phase", ""),
        "- Positions: `%s`" % status.get("positions_status", ""),
        "- Positions count: `%s`" % status.get("positions_count", ""),
        "- Detected symbols: `%s`" % ", ".join(status.get("symbols", [])),
        "- Detected sides: `%s`" % json.dumps(status.get("side_counts", {}), sort_keys=True),
        "- Long-only assumption: `%s`" % status.get("long_only", ""),
        "- Position window: `%s` .. `%s`" % (status.get("position_start_utc", ""), status.get("position_end_utc", "")),
        "- HYPE annual/window NPZ: `%s`" % status.get("npz_path", ""),
        "- NPZ status: `%s`" % status.get("npz_status", ""),
        "- V21 tuning started: `%s`" % status.get("tuning_started", False),
        "",
        "## Files",
        "",
        "- Positions CSV: `%s`" % status.get("positions_csv", ""),
        "- Universe file: `%s`" % status.get("universe_file", ""),
        "- Windows file: `%s`" % status.get("windows_file", ""),
        "",
        "## Strategy Contract",
        "",
        "- Entry is single-leg only.",
        "- Paper-live signal source is Binance current open positions tab/endpoint.",
        "- Entry side follows this lead's direct open-position `side`; no contrarian-on-close.",
        "- Historical closed positions are used only for backtest/tuning labels.",
        "- Trend does not flip, suppress, or resize entries.",
        "- Consilium handles warmup/trend diagnostics and risk-gated candidate evaluation.",
        "",
        "## Consilium Notes",
        "",
    ]
    for note in status.get("philosophy", []):
        lines.append("- %s" % note)
    lines.extend(["", "## Variants", "", "| variant | best label | score | status |", "|---|---|---:|---|"])
    for item in status.get("variants", []):
        lines.append(
            "| %s | %s | %.6f | %s |"
            % (item.get("name", ""), item.get("best_label", ""), float(item.get("score", 0.0)), item.get("status", ""))
        )
    if status.get("blocker"):
        lines.extend(["", "## Blocker", "", str(status["blocker"])])
    (out_dir / "STATUS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    ap = argparse.ArgumentParser(description="Run paper/backtest Binance 430051 HYPE V21 loop.")
    ap.add_argument("--lead-id", default=DEFAULT_LEAD)
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--tmux-session", default="binance_430051_hype_v21_loop")
    ap.add_argument("--v21-config", default=str(DEFAULT_CFG))
    ap.add_argument("--time-range", default="365D")
    ap.add_argument("--page-size", type=int, default=100)
    ap.add_argument("--max-pages", type=int, default=10)
    ap.add_argument("--warmup-days", type=int, default=21)
    ap.add_argument("--force-symbols", default="HYPEUSDT")
    ap.add_argument("--npz-exchange", default="binanceusdm")
    ap.add_argument("--timeframe", default="1m")
    ap.add_argument("--target-notional", type=float, default=100.0)
    ap.add_argument("--initial-equity", type=float, default=100.0)
    ap.add_argument("--sleep-sec", type=float, default=0.10)
    ap.add_argument("--loop-sleep-sec", type=float, default=21600.0)
    ap.add_argument("--max-iterations", type=int, default=0)
    ap.add_argument("--step-timeout-sec", type=float, default=0.0)
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    status: Dict[str, Any] = {
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "tmux_session": args.tmux_session,
        "lead_id": args.lead_id,
        "source_url": SOURCE_URL,
        "paper_backtest_only": True,
        "phase": "starting",
        "philosophy": read_philosophy_notes(),
        "variants": [],
    }
    write_status(out_dir, status)

    wave = 1
    while True:
        wave_dir = out_dir / ("wave_%03d" % wave)
        collect_dir = wave_dir / "position_refresh"
        positions_csv = collect_dir / "position_history_normalized.csv"
        status.update({"updated_at": utc_now(), "phase": "collecting_public_position_history", "positions_csv": str(positions_csv)})
        write_status(out_dir, status)

        refresh = run_cmd([
            sys.executable,
            str(MODULE_DIR / "binance_copy_contrarian_on_close.py"),
            "--portfolio-id",
            args.lead_id,
            "--time-range",
            args.time_range,
            "--page-size",
            str(args.page_size),
            "--max-pages",
            str(args.max_pages),
            "--out-dir",
            str(collect_dir),
            "--ttl-hours",
            "72",
            "--target-notional",
            str(args.target_notional),
            "--dca-counts",
            "0,1,2,3",
            "--sleep-sec",
            str(args.sleep_sec),
        ], wave_dir / "refresh_positions.log", timeout_sec=args.step_timeout_sec)

        rows = read_positions(positions_csv)
        symbols = detect_universe(rows, [s.strip() for s in args.force_symbols.split(",")])
        sides = side_counts(rows)
        long_only = bool(rows) and set(sides.keys()) == {"LONG"}
        start, end, pos_start, pos_end = window_for(rows, args.warmup_days)
        universe_file = out_dir / "universe_430051_hype.txt"
        universe_file.write_text("\n".join(symbols) + "\n", encoding="utf-8")
        windows_file = out_dir / "windows.json"
        windows = {
            "lead_id": args.lead_id,
            "source_url": SOURCE_URL,
            "position_start_utc": pos_start.isoformat().replace("+00:00", "Z"),
            "position_end_utc": pos_end.isoformat().replace("+00:00", "Z"),
            "warmup_days": args.warmup_days,
            "window_start_utc": start.isoformat().replace("+00:00", "Z"),
            "window_end_utc": end.isoformat().replace("+00:00", "Z"),
            "symbols": symbols,
            "side_counts": sides,
        }
        windows_file.write_text(json.dumps(windows, indent=2), encoding="utf-8")

        npz_path = out_dir / ("binance_%s_hype_universe_%s_%s_%s.npz" % (
            args.lead_id,
            args.timeframe,
            start.strftime("%Y%m%d"),
            end.strftime("%Y%m%d"),
        ))
        status.update({
            "updated_at": utc_now(),
            "phase": "fetching_hype_annual_npz",
            "positions_status": "ok" if refresh["returncode"] == 0 and rows else "blocked_or_empty",
            "positions_count": len(rows),
            "symbols": symbols,
            "side_counts": sides,
            "long_only": long_only,
            "position_start_utc": windows["position_start_utc"],
            "position_end_utc": windows["position_end_utc"],
            "window_start_utc": windows["window_start_utc"],
            "window_end_utc": windows["window_end_utc"],
            "universe_file": str(universe_file),
            "windows_file": str(windows_file),
            "npz_path": str(npz_path),
            "npz_status": "pending",
            "tuning_started": False,
        })
        if not rows:
            status["blocker"] = "Binance public position history returned no usable closed positions for this lead. HYPE NPZ will still be attempted from forced universe."
        write_status(out_dir, status)

        fetch = run_cmd([
            sys.executable,
            "obw_platform/telegram_signal_tools/fetch_futures_ohlcv_npz_v1.py",
            "--exchange",
            args.npz_exchange,
            "--universe-file",
            str(universe_file),
            "--out",
            str(npz_path),
            "--timeframe",
            args.timeframe,
            "--bars",
            "525600",
            "--since-utc",
            start.isoformat().replace("+00:00", "Z"),
            "--until-utc",
            end.isoformat().replace("+00:00", "Z"),
            "--min-bars",
            "1000",
            "--max-empty",
            "5",
            "--sleep-sec",
            str(args.sleep_sec),
        ], wave_dir / "fetch_npz.log", timeout_sec=args.step_timeout_sec)
        status.update({
            "updated_at": utc_now(),
            "npz_status": "ok" if fetch["returncode"] == 0 and npz_path.exists() else "failed",
        })
        write_status(out_dir, status)

        if rows:
            status.update({"updated_at": utc_now(), "phase": "v21_signal_side_tuning", "tuning_started": True})
            write_status(out_dir, status)
            variants = []
            for variant in make_variants(Path(args.v21_config), wave_dir, long_only):
                variant_dir = wave_dir / "variants" / safe_slug(variant["name"])
                result = run_cmd([
                    sys.executable,
                    str(MODULE_DIR / "compare_binance_copy_positions_dca.py"),
                    "--positions-csv",
                    str(positions_csv),
                    "--v21-config",
                    variant["cfg"],
                    "--out-dir",
                    str(variant_dir),
                    "--target-notional",
                    str(args.target_notional),
                    "--initial-equity",
                    str(args.initial_equity),
                    "--dca-counts",
                    "0,1,2,3",
                    "--sleep-sec",
                    str(args.sleep_sec),
                ], variant_dir / "compare.log", timeout_sec=args.step_timeout_sec)
                best = best_summary(variant_dir / "summary.json")
                variants.append({
                    "name": variant["name"],
                    "cfg": variant["cfg"],
                    "status": "ok" if result["returncode"] == 0 else "failed",
                    "best_label": best["best_label"],
                    "score": best["score"],
                    "out_dir": str(variant_dir),
                })
            status.update({
                "updated_at": utc_now(),
                "phase": "sleeping_between_waves",
                "variants": sorted(variants, key=lambda x: float(x["score"]), reverse=True),
            })
            write_status(out_dir, status)
        else:
            status.update({"updated_at": utc_now(), "phase": "blocked_no_positions", "tuning_started": False})
            write_status(out_dir, status)

        if args.max_iterations and wave >= args.max_iterations:
            break
        wave += 1
        time.sleep(max(1.0, args.loop_sleep_sec))


if __name__ == "__main__":
    main()
