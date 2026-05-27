#!/usr/bin/env python3
"""Run the first Akela basket through the trusted V21 short-leg backtester.

This script is orchestration only. It does not change strategy YAML, exchange
model, fee model, slippage model, liquidation model, or backtest math.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[3]
LANE = Path("obw_platform/meta_strategies/akela_meta_short")
REPORTS = ROOT / LANE / "reports"
RAW_ROOT = ROOT / "_reports" / "akela_meta_short"
CONFIG = Path("obw_platform/configs/V21_freedommoney_bingx_live_candidate_1m_1y.yaml")
BACKTESTER = Path("obw_platform/backtester_dual_long_short_fast_pack_v2.py")

CANDIDATES = [
    {
        "name": "IDOL",
        "symbol": "IDOL/USDT:USDT",
        "npz": "DB/akela_meta_short_1m_1y_idol_bingx.npz",
    },
    {
        "name": "FREEDOMMONEY",
        "symbol": "FREEDOMMONEY/USDT:USDT",
        "npz": "DB/fast_cache_1m_freedommoney_1y_bingx.npz",
    },
    {
        "name": "MAXXING",
        "symbol": "MAXXING/USDT:USDT",
        "npz": "DB/fast_cache_1m_maxxing_1y_bingx.npz",
    },
    {
        "name": "SUP",
        "symbol": "SUP/USDT:USDT",
        "npz": "DB/akela_meta_short_1m_1y_sup_bingx.npz",
    },
]


def stamp_utc() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def metric(data: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return None


def fmt(value: Any, digits: int = 2) -> str:
    if value is None:
        return "n/a"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def parse_json(stdout: str) -> dict[str, Any] | None:
    try:
        return json.loads(stdout)
    except json.JSONDecodeError:
        start = stdout.find("{")
        end = stdout.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(stdout[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None


def prune_old_basket_raw(protected_names: set[str] | None = None) -> list[str]:
    """Remove old ignored basket raw dirs after compact summaries exist.

    The committed report files are the durable evidence. Raw curves are large
    runtime artifacts and can be regenerated from the manifest commands.
    """

    if os.environ.get("OBW_AKELA_PRUNE_BASKET_RAW", "1").strip().lower() in {"0", "false", "no"}:
        return []
    keep = int(os.environ.get("OBW_AKELA_BASKET_RAW_KEEP", "2") or 2)
    protected = protected_names or set()
    if keep < 1:
        keep = 1

    dirs = sorted(
        [path for path in RAW_ROOT.glob("basket_*") if path.is_dir()],
        key=lambda path: path.name,
        reverse=True,
    )
    kept = 0
    removed: list[str] = []
    for path in dirs:
        if path.name in protected:
            kept += 1
            continue
        stamp = path.name[len("basket_") :] if path.name.startswith("basket_") else path.name
        summary_name = f"basket_summary_{stamp}.md"
        summary_exists = (REPORTS / summary_name).exists()
        if not summary_exists:
            kept += 1
            continue
        if kept < keep:
            kept += 1
            continue
        shutil.rmtree(path)
        removed.append(str(path.relative_to(ROOT)))
    return removed


def run_candidate(candidate: dict[str, str], run_dir: Path, limit_bars: int) -> dict[str, Any]:
    out_dir = run_dir / candidate["name"].lower()
    out_dir.mkdir(parents=True, exist_ok=True)

    npz_path = ROOT / candidate["npz"]
    result: dict[str, Any] = {
        "name": candidate["name"],
        "symbol": candidate["symbol"],
        "npz": candidate["npz"],
        "npz_exists": npz_path.exists(),
        "raw_dir": str(out_dir.relative_to(ROOT)),
    }
    if not npz_path.exists():
        result.update({"returncode": 127, "status": "missing_npz"})
        (out_dir / "backtest.log").write_text("missing npz\n", encoding="utf-8")
        (out_dir / "metrics.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
        return result

    cmd = [
        "python3",
        str(BACKTESTER),
        "--cfg",
        str(CONFIG),
        "--npz",
        str(npz_path),
        "--symbol",
        candidate["symbol"],
        "--export-curves",
        str(out_dir / "curves.csv"),
    ]
    if limit_bars > 0:
        cmd.extend(["--limit-bars", str(limit_bars)])

    started = time.time()
    proc = subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    elapsed = time.time() - started
    (out_dir / "backtest.log").write_text("[cmd] " + " ".join(cmd) + "\n" + proc.stdout, encoding="utf-8")

    parsed = parse_json(proc.stdout)
    result.update(
        {
            "returncode": proc.returncode,
            "status": "ok" if proc.returncode == 0 and parsed is not None else "failed",
            "seconds": round(elapsed, 3),
            "cmd": cmd,
        }
    )
    if parsed:
        result["metrics"] = parsed
        result["return_mtm_pct_on_start"] = metric(parsed, "return_mtm_pct_on_start", "return_pct_on_start")
        result["mdd_mtm_pct"] = metric(parsed, "mdd_mtm_%", "mdd_mtm_pct", "mdd_%")
        result["trades_total"] = metric(parsed, "trades_total", "trades")
        result["margin_call_events_total"] = metric(parsed, "margin_call_events_total", "margin_call_events")
        result["bars_in_margin_call"] = metric(parsed, "bars_in_margin_call")
        result["equity_end_mtm_total"] = metric(parsed, "equity_end_mtm_total")
        result["curves_csv"] = parsed.get("curves_csv")
    else:
        result["parse_error"] = "backtester stdout did not contain JSON"

    (out_dir / "metrics.json").write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def build_summary(manifest: dict[str, Any]) -> str:
    rows = manifest["results"]
    ok_rows = [row for row in rows if row["status"] == "ok"]
    returns = [row.get("return_mtm_pct_on_start") for row in ok_rows if isinstance(row.get("return_mtm_pct_on_start"), (int, float))]
    mdds = [row.get("mdd_mtm_pct") for row in ok_rows if isinstance(row.get("mdd_mtm_pct"), (int, float))]
    margins = [row.get("margin_call_events_total") or 0 for row in ok_rows]
    equal_return = sum(returns) / len(returns) if returns else None
    worst_mdd = min(mdds) if mdds else None
    margin_total = sum(int(x) for x in margins)

    best = max(ok_rows, key=lambda row: row.get("return_mtm_pct_on_start") or -10**18) if ok_rows else None
    worst = min(ok_rows, key=lambda row: row.get("return_mtm_pct_on_start") or 10**18) if ok_rows else None

    lines = [
        "# Akela Basket Validation Latest Summary",
        "",
        f"Updated: {manifest['stamp']}",
        f"Baseline config: `{CONFIG}`",
        f"Backtester: `{BACKTESTER}`",
        f"Raw artifacts: `{manifest['raw_dir']}`",
        f"Limit bars: `{manifest['limit_bars'] or 'full'}`",
        "",
        "## Basket Result",
        "",
        f"- successful symbols: {len(ok_rows)}/{len(rows)}",
        f"- equal-weight terminal return approximation: {fmt(equal_return)}%",
        f"- worst single-symbol MTM drawdown: {fmt(worst_mdd)}%",
        f"- total margin-call events: {margin_total}",
    ]
    if best:
        lines.append(f"- best symbol: `{best['symbol']}` return {fmt(best.get('return_mtm_pct_on_start'))}%")
    if worst:
        lines.append(f"- worst symbol: `{worst['symbol']}` return {fmt(worst.get('return_mtm_pct_on_start'))}%")

    lines.extend(
        [
            "",
            "## Per-Symbol Results",
            "",
            "| symbol | status | return_mtm_% | mdd_mtm_% | trades | margin_calls | seconds |",
            "| --- | --- | ---: | ---: | ---: | ---: | ---: |",
        ]
    )
    for row in rows:
        lines.append(
            "| "
            + " | ".join(
                [
                    f"`{row['symbol']}`",
                    row["status"],
                    fmt(row.get("return_mtm_pct_on_start")),
                    fmt(row.get("mdd_mtm_pct")),
                    fmt(row.get("trades_total"), 0),
                    fmt(row.get("margin_call_events_total"), 0),
                    fmt(row.get("seconds")),
                ]
            )
            + " |"
        )

    lines.extend(
        [
            "",
            "## Interpretation",
            "",
            "This is validation of the upper-layer basket idea using the existing V21 short-leg backtester.",
            "It is not a live promotion and it does not modify production strategy YAMLs.",
        ]
    )
    if len(ok_rows) != len(rows):
        lines.append("Some symbols failed, so do not compare this as a complete basket.")
    elif margin_total > 0:
        lines.append("The basket still has tail-risk work because at least one symbol hit margin-call events.")
    elif equal_return is not None and equal_return > 0:
        lines.append("The basket is worth the next research step: curve alignment and allocation/risk weighting.")
    else:
        lines.append("The basket is not yet strong enough for tuning; revise selector gates first.")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    stamp = stamp_utc()
    limit_bars = int(os.environ.get("OBW_AKELA_BASKET_LIMIT_BARS", "0") or 0)
    pre_pruned = prune_old_basket_raw()
    run_dir = RAW_ROOT / f"basket_{stamp}"
    run_dir.mkdir(parents=True, exist_ok=True)
    REPORTS.mkdir(parents=True, exist_ok=True)

    results = [run_candidate(candidate, run_dir, limit_bars) for candidate in CANDIDATES]
    manifest = {
        "schema": "akela_basket_validation_v1",
        "stamp": stamp,
        "raw_dir": str(run_dir.relative_to(ROOT)),
        "config": str(CONFIG),
        "backtester": str(BACKTESTER),
        "limit_bars": limit_bars,
        "results": results,
    }
    summary = build_summary(manifest)

    latest_manifest = REPORTS / "latest_basket_manifest.json"
    latest_summary = REPORTS / "latest_basket_summary.md"
    dated_summary = REPORTS / f"basket_summary_{stamp}.md"
    latest_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")
    latest_summary.write_text(summary, encoding="utf-8")
    dated_summary.write_text(summary, encoding="utf-8")
    post_pruned = prune_old_basket_raw({run_dir.name})
    if pre_pruned or post_pruned:
        manifest["raw_retention"] = {
            "policy": "keep latest basket raw dirs after committed summaries exist",
            "pre_run_removed": pre_pruned,
            "post_run_removed": post_pruned,
            "keep": int(os.environ.get("OBW_AKELA_BASKET_RAW_KEEP", "2") or 2),
        }
        latest_manifest.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

    print(summary)
    return 0 if all(row["status"] == "ok" for row in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
