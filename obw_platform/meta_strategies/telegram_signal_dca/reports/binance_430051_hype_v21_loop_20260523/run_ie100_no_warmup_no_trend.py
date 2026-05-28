#!/usr/bin/env python3
"""Re-run VeronicaUA/HYPE variants with initial_equity=100.

This is paper/backtest-only. It uses the existing closed-position history and
direct lead side labels. The compare script does not evaluate warmup/trend
gates; it only applies V21 DCA sizing levels to source-side trades.
"""
from __future__ import annotations

import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List


ROOT = Path(__file__).resolve().parents[5]
REPORT_DIR = Path(__file__).resolve().parent
SOURCE_WAVE = REPORT_DIR / "wave_002"
OUT_DIR = REPORT_DIR / "wave_002_initial_equity_100_no_warmup_no_trend"
POSITIONS = SOURCE_WAVE / "position_refresh" / "position_history_normalized.csv"
COMPARE = ROOT / "obw_platform" / "meta_strategies" / "telegram_signal_dca" / "compare_binance_copy_positions_dca.py"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def run_variant(cfg: Path, variant_out: Path) -> Dict[str, Any]:
    variant_out.mkdir(parents=True, exist_ok=True)
    log_path = variant_out / "compare.log"
    cmd = [
        sys.executable,
        str(COMPARE),
        "--positions-csv",
        str(POSITIONS),
        "--v21-config",
        str(cfg),
        "--out-dir",
        str(variant_out),
        "--target-notional",
        "100",
        "--initial-equity",
        "100",
        "--dca-counts",
        "0,1,2,3",
        "--sleep-sec",
        "0.08",
    ]
    started = utc_now()
    with log_path.open("a", encoding="utf-8") as log:
        log.write("\n[%s] $ %s\n" % (started, " ".join(cmd)))
        log.flush()
        proc = subprocess.run(cmd, cwd=str(ROOT), text=True, stdout=log, stderr=subprocess.STDOUT)
    return {
        "name": cfg.stem,
        "cfg": str(cfg),
        "out_dir": str(variant_out),
        "returncode": proc.returncode,
        "started_at": started,
        "finished_at": utc_now(),
        "log": str(log_path),
    }


def load_summary(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def best_label(summary: Dict[str, Any]) -> str:
    if not summary:
        return ""
    return max(
        summary,
        key=lambda label: float(summary[label].get("net_pct_per_30d") or summary[label].get("net_pct") or -1e30)
        - abs(float(summary[label].get("max_dd_pct") or 0.0)) * 0.03,
    )


def write_report(results: List[Dict[str, Any]]) -> None:
    old_status = load_summary(SOURCE_WAVE / "variants" / "long_low_exposure" / "summary.json")
    old_best = old_status.get("dca3", {})
    rows = []
    for result in results:
        summary = load_summary(Path(result["out_dir"]) / "summary.json")
        label = best_label(summary)
        item = dict(result)
        item["best_label"] = label
        item["summary"] = summary.get(label, {})
        rows.append(item)
    rows.sort(
        key=lambda r: float(r["summary"].get("net_pct_per_30d") or r["summary"].get("net_pct") or -1e30)
        - abs(float(r["summary"].get("max_dd_pct") or 0.0)) * 0.03,
        reverse=True,
    )
    manifest = {
        "updated_at": utc_now(),
        "source_wave": str(SOURCE_WAVE),
        "positions_csv": str(POSITIONS),
        "initial_equity": 100.0,
        "target_notional": 100.0,
        "no_warmup_no_trend": True,
        "no_warmup_no_trend_note": (
            "The compare script has no trend or warmup gating path. It uses direct lead side, "
            "avg entry/close, and 1m candles only."
        ),
        "old_initial_equity_10000_long_low_exposure_dca3": old_best,
        "variants": rows,
    }
    (OUT_DIR / "MANIFEST.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# VeronicaUA HYPE IE100 No-Warmup/No-Trend Rerun",
        "",
        "Paper/backtest-only. No live orders. No secrets.",
        "",
        "- Updated: `%s`" % manifest["updated_at"],
        "- Source wave: `%s`" % SOURCE_WAVE,
        "- Positions CSV: `%s`" % POSITIONS,
        "- Initial equity: `100`",
        "- Target notional: `100`",
        "- Entry side: direct Binance lead side from historical rows",
        "- Contrarian close: `false`",
        "- Warmup/trend gating: `false`",
        "",
        "## Existing Signal-Side Behavior",
        "",
        "The existing compare path was already no-warmup/no-trend for trade decisions. "
        "The loop's `warmup_days` only affects annual NPZ collection metadata; the actual "
        "`compare_binance_copy_positions_dca.py` backtest does not consult trend fields or warmup windows.",
        "",
        "## Old vs IE100",
        "",
        "| metric | old IE=10000 long_low_exposure dca3 | IE=100 rerun long_low_exposure dca3 |",
        "|---|---:|---:|",
    ]
    long_low = next((r for r in rows if r["name"] == "long_low_exposure"), {})
    new_best = load_summary(Path(long_low.get("out_dir", "")) / "summary.json").get("dca3", {}) if long_low else {}
    for key in ("net_pnl", "net_pct", "net_pct_per_30d", "pf", "max_dd_pct", "win_rate_pct", "positions"):
        lines.append("| `%s` | `%s` | `%s` |" % (key, old_best.get(key, ""), new_best.get(key, "")))
    lines.extend([
        "",
        "## Variant Ranking",
        "",
        "| variant | best label | net % | /30d % | PF | maxDD % | win % | rc |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ])
    for row in rows:
        s = row["summary"]
        lines.append(
            "| %s | %s | %.6f | %.6f | %.6f | %.6f | %.3f | %s |"
            % (
                row["name"],
                row["best_label"],
                float(s.get("net_pct") or 0.0),
                float(s.get("net_pct_per_30d") or 0.0),
                float(s.get("pf") or 0.0),
                float(s.get("max_dd_pct") or 0.0),
                float(s.get("win_rate_pct") or 0.0),
                row["returncode"],
            )
        )
    (OUT_DIR / "REPORT_IE100_NO_WARMUP_NO_TREND.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    cfgs = sorted((SOURCE_WAVE / "configs").glob("*.yaml"))
    results = []
    for cfg in cfgs:
        results.append(run_variant(cfg, OUT_DIR / "variants" / cfg.stem))
    write_report(results)


if __name__ == "__main__":
    main()
