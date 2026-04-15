#!/usr/bin/env python3
"""
Wrapper around:
- bingx_export_futures_trade_history_debug_v3.py
- match_trades_tv_bingx.py

It takes a TradingView backtest CSV, infers:
- symbol
- start/end window

Then it:
1) downloads the same BingX exchange window for the same symbol,
2) writes all outputs into a subfolder under _reports,
3) prepares a matcher-friendly BingX CSV,
4) optionally runs match_trades_tv_bingx.py automatically.

Typical usage:

python extract_bingx_window_from_tv.py \
  --tv "C_-_SHORT_-_MA_driven_BINGX_ENAUSDT.P_2026-04-15_68ffd.csv" \
  --run-match

Optional:
  --export-script bingx_export_futures_trade_history_debug_v3.py
  --match-script match_trades_tv_bingx.py
  --env-file .env
  --insecure
  --verbose
  --debug-http
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Optional, Tuple

import pandas as pd


TV_REQUIRED_COLS = {
    "Date and time",
    "Type",
    "Signal",
    "Price USDT",
    "Size (qty)",
    "Net P&L USDT",
}


def safe_stem(text: str) -> str:
    out = []
    for ch in text:
        if ch.isalnum() or ch in ("-", "_", "."):
            out.append(ch)
        else:
            out.append("_")
    return "".join(out).strip("_") or "run"


def find_existing_file(candidates: list[Path]) -> Optional[Path]:
    for p in candidates:
        if p.exists():
            return p.resolve()
    return None


def parse_tv_csv(path: Path) -> pd.DataFrame:
    last_exc = None
    for enc in ("utf-8-sig", "utf-8", "cp1251", "cp1252", "latin1"):
        try:
            df = pd.read_csv(path, encoding=enc)
            missing = sorted(TV_REQUIRED_COLS - set(df.columns))
            if missing:
                raise ValueError(f"TV CSV missing columns: {missing}")
            df["dt"] = pd.to_datetime(df["Date and time"], errors="coerce")
            if df["dt"].isna().any():
                raise ValueError("TV CSV contains unparseable timestamps")
            return df
        except Exception as exc:
            last_exc = exc
    raise RuntimeError(f"Failed to parse TV CSV {path}: {last_exc}")


def infer_bingx_symbol_from_tv_filename(tv_path: Path) -> str:
    name = tv_path.name.upper()

    # Examples:
    # ...BINGX_ENAUSDT.P...
    # ...BINGX_COMPUSDT.P...
    # ...BTCUSDT.P...
    m = re.search(r'BINGX_([A-Z0-9]+)(USDT|USDC)\.P', name)
    if not m:
        m = re.search(r'([A-Z0-9]+)(USDT|USDC)\.P', name)
    if not m:
        raise RuntimeError(
            "Could not infer symbol from TV filename. Expected something like "
            "'...BINGX_ENAUSDT.P...'"
        )
    base, quote = m.group(1), m.group(2)
    return f"{base}-{quote}"


def infer_window(df: pd.DataFrame) -> Tuple[pd.Timestamp, pd.Timestamp]:
    start = df["dt"].min()
    end = df["dt"].max()
    if pd.isna(start) or pd.isna(end):
        raise RuntimeError("Could not infer time window from TV CSV")
    return start, end


def resolve_script(user_value: Optional[str], fallback_names: list[str]) -> Path:
    if user_value:
        p = Path(user_value).expanduser()
        if not p.exists():
            raise FileNotFoundError(f"Script not found: {p}")
        return p.resolve()

    here = Path.cwd().resolve()
    script_dir = Path(__file__).resolve().parent
    candidates = []
    for name in fallback_names:
        candidates.extend([
            here / name,
            script_dir / name,
            Path("/mnt/data") / name,
        ])
    found = find_existing_file(candidates)
    if not found:
        raise FileNotFoundError(
            "Could not locate required helper script. Tried: " + ", ".join(str(c) for c in candidates)
        )
    return found


def build_match_ready_bingx_csv(src_csv: Path, dst_csv: Path) -> None:
    df = pd.read_csv(src_csv, encoding="utf-8-sig")
    if "Час виконання" not in df.columns:
        raise RuntimeError("Exported BingX CSV does not contain 'Час виконання'")

    # Convert YYYY-MM-DD HH:MM:SS -> dd/mm/yy HH:MM for the existing matcher parser
    dt = pd.to_datetime(df["Час виконання"], errors="coerce")
    if dt.isna().any():
        raise RuntimeError("Could not parse exported BingX 'Час виконання' for match preparation")

    df["Час виконання"] = dt.dt.strftime("%d/%m/%y %H:%M")
    dst_csv.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(dst_csv, index=False, encoding="utf-8-sig")


def run_subprocess(cmd: list[str]) -> None:
    print("[run]", " ".join(str(x) for x in cmd))
    subprocess.run(cmd, check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description="Fetch BingX exchange window from a TV backtest CSV and optionally run matching")
    ap.add_argument("--tv", required=True, help="TradingView backtest CSV path")
    ap.add_argument("--tz", default="Europe/Kyiv", help="Timezone used for export script")
    ap.add_argument("--out-root", default="_reports/tv_window_fetch", help="Root folder for per-run outputs")
    ap.add_argument("--label", default=None, help="Optional custom label/subfolder name")
    ap.add_argument("--env-file", default=None, help="Optional .env path for BingX credentials")
    ap.add_argument("--export-script", default=None, help="Path to bingx_export_futures_trade_history_debug_v3.py")
    ap.add_argument("--match-script", default=None, help="Path to match_trades_tv_bingx.py")
    ap.add_argument("--run-match", action="store_true", help="Run matching immediately after export")
    ap.add_argument("--insecure", action="store_true", help="Pass through to export script")
    ap.add_argument("--ca-bundle", default=None, help="Pass through to export script")
    ap.add_argument("--verbose", action="store_true", help="Pass through to export script")
    ap.add_argument("--debug-http", action="store_true", help="Pass through to export script")
    args = ap.parse_args()

    tv_path = Path(args.tv).expanduser().resolve()
    if not tv_path.exists():
        raise FileNotFoundError(f"TV file not found: {tv_path}")

    export_script = resolve_script(args.export_script, [
        "bingx_export_futures_trade_history_debug_v3.py",
        "bingx_export_futures_trade_history_debug.py",
        "bingx_export_futures_trade_history_v2.py",
        "bingx_export_futures_trade_history.py",
    ])

    match_script = resolve_script(args.match_script, [
        "match_trades_tv_bingx.py",
        "match_trades_tv_bingx(2).py",
        "match_trades_tv_bingx(1).py",
    ])

    tv_df = parse_tv_csv(tv_path)
    symbol = infer_bingx_symbol_from_tv_filename(tv_path)
    start_dt, end_dt = infer_window(tv_df)

    label = args.label or safe_stem(tv_path.stem)
    run_dir = Path(args.out_root).expanduser().resolve() / label
    run_dir.mkdir(parents=True, exist_ok=True)

    export_csv = run_dir / f"{safe_stem(symbol)}_trade_history.csv"
    export_fills = run_dir / f"{safe_stem(symbol)}_fills.csv"
    export_orders = run_dir / f"{safe_stem(symbol)}_orders.csv"
    export_json = run_dir / f"{safe_stem(symbol)}_joined.json"
    match_ready_csv = run_dir / f"{safe_stem(symbol)}_trade_history_for_match.csv"

    print("[info] tv:", tv_path)
    print("[info] export_script:", export_script)
    print("[info] match_script:", match_script)
    print("[info] inferred symbol:", symbol)
    print("[info] inferred window:", start_dt.strftime("%Y-%m-%d %H:%M:%S"), "->", end_dt.strftime("%Y-%m-%d %H:%M:%S"))
    print("[info] output dir:", run_dir)

    export_cmd = [
        sys.executable,
        str(export_script),
        "--symbol", symbol,
        "--start", start_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "--end", end_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "--tz", args.tz,
        "--out-dir", str(run_dir),
        "--out", export_csv.name,
        "--fills-csv", export_fills.name,
        "--orders-csv", export_orders.name,
        "--raw-json", export_json.name,
    ]
    if args.env_file:
        export_cmd.extend(["--env-file", args.env_file])
    if args.insecure:
        export_cmd.append("--insecure")
    if args.ca_bundle:
        export_cmd.extend(["--ca-bundle", args.ca_bundle])
    if args.verbose:
        export_cmd.append("--verbose")
    if args.debug_http:
        export_cmd.append("--debug-http")

    run_subprocess(export_cmd)

    build_match_ready_bingx_csv(export_csv, match_ready_csv)
    print("[info] prepared match-ready csv:", match_ready_csv)

    if args.run_match:
        db_path = run_dir / "comparison_results.sqlite"
        match_cmd = [
            sys.executable,
            str(match_script),
            "--tv", str(tv_path),
            "--bingx", str(match_ready_csv),
            "--db", str(db_path),
            "--out-dir", str(run_dir),
            "--label", label,
        ]
        run_subprocess(match_cmd)

    print("[done] run_dir =", run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
