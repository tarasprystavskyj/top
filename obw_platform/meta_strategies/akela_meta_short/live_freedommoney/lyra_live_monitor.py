#!/usr/bin/env python3
"""Lyra: read-only FREEDOMMONEY live monitor.

Lyra does not trade. It reads live artifacts and writes a compact status report.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[4]
LANE = ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "live_freedommoney"
LIVE_DIR = ROOT / "obw_platform" / "_reports" / "_live" / "bingx_freedommoney_v21_min2p2"
STATUS_JSON = LANE / "lyra_latest_status.json"
STATUS_MD = LANE / "lyra_latest_status.md"


def load_json(path: Path) -> Any:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        return {"error": str(exc)}


def sqlite_counts(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False}
    out: dict[str, Any] = {"exists": True}
    try:
        con = sqlite3.connect(str(path))
        cur = con.cursor()
        for table in ("orders", "open_positions", "closed_trades", "equity_curve", "debug_events"):
            try:
                out[f"{table}_rows"] = cur.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            except Exception:
                out[f"{table}_rows"] = None
        con.close()
    except Exception as exc:
        out["error"] = str(exc)
    return out


def main() -> int:
    now = datetime.now(timezone.utc).isoformat()
    session = LIVE_DIR / "session.sqlite"
    positions = load_json(LIVE_DIR / "live_positions.json")
    pnl = load_json(LIVE_DIR / "live_pnl_summary.json")
    exec_metrics = load_json(LIVE_DIR / "live_execution_metrics.json")
    slippage = load_json(LIVE_DIR / "live_slippage_calibration.json")
    status = {
        "schema": "lyra_freedommoney_live_status_v1",
        "ts_utc": now,
        "live_dir": str(LIVE_DIR.relative_to(ROOT)),
        "live_dir_exists": LIVE_DIR.exists(),
        "session": sqlite_counts(session),
        "positions": positions,
        "pnl": pnl,
        "execution_metrics": exec_metrics,
        "slippage": slippage,
        "warnings": [],
    }
    if not LIVE_DIR.exists():
        status["warnings"].append("live result directory does not exist yet")
    if status["session"].get("error"):
        status["warnings"].append("session.sqlite read error")
    totals = (exec_metrics or {}).get("totals") if isinstance(exec_metrics, dict) else None
    if isinstance(totals, dict):
        if float(totals.get("rejected") or 0.0) > 0.0:
            status["warnings"].append("exchange rejected at least one order")
        if float(totals.get("fill_rate") or 1.0) < 0.95:
            status["warnings"].append("fill rate below 95%")

    STATUS_JSON.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
    lines = [
        "# Lyra FREEDOMMONEY Live Status",
        "",
        f"Updated: {now}",
        f"Live dir: `{status['live_dir']}`",
        f"Live dir exists: `{status['live_dir_exists']}`",
        "",
        "## Session",
        "",
    ]
    for key, value in sorted(status["session"].items()):
        lines.append(f"- `{key}`: `{value}`")
    lines.extend(["", "## Warnings", ""])
    if status["warnings"]:
        lines.extend([f"- {item}" for item in status["warnings"]])
    else:
        lines.append("- none")
    lines.append("")
    STATUS_MD.write_text("\n".join(lines), encoding="utf-8")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
