#!/usr/bin/env python3
"""Locate the backtest configuration with the highest ``equity_end`` value.

The script scans ``obw_platform/_reports/_backtest`` for directories whose
name starts with ``backtest_t3m_30d``. For each matching directory it reads the
``bt_summary.csv`` file (containing JSON data) and collects the ``equity_end``
value. The directory with the highest ``equity_end`` is printed to stdout.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Optional, Tuple

BASE_REPORT_DIR = Path(__file__).resolve().parent.parent / "_reports" / "_backtest"
PREFIX = "backtest_t3m_30d"
SUMMARY_FILENAME = "bt_summary.csv"


def load_summary(summary_path: Path) -> Optional[Dict[str, float]]:
    """Load a summary file and return its JSON content.

    Parameters
    ----------
    summary_path:
        Path to the ``bt_summary.csv`` file containing JSON data.

    Returns
    -------
    Optional[Dict[str, float]]
        The parsed JSON dictionary if successful, otherwise ``None``.
    """

    try:
        return json.loads(summary_path.read_text())
    except FileNotFoundError:
        print(f"[skip] Missing summary file: {summary_path}")
    except json.JSONDecodeError as exc:
        print(f"[skip] Could not parse JSON in {summary_path}: {exc}")
    return None


def find_max_equity(directory: Path) -> Tuple[Optional[Path], Optional[float]]:
    """Find the sub-directory containing the maximum ``equity_end`` value."""

    max_equity: float = float("-inf")
    best_path: Optional[Path] = None

    for candidate in sorted(directory.iterdir()):
        if not candidate.is_dir() or not candidate.name.startswith(PREFIX):
            continue

        summary_path = candidate / SUMMARY_FILENAME
        summary = load_summary(summary_path)
        if not summary or "equity_end" not in summary:
            if summary is not None:
                print(f"[skip] 'equity_end' missing in {summary_path}")
            continue

        equity_end = summary["equity_end"]
        if isinstance(equity_end, (int, float)) and equity_end > max_equity:
            max_equity = float(equity_end)
            best_path = candidate

    if best_path is None:
        return None, None

    return best_path, max_equity


def main() -> None:
    if not BASE_REPORT_DIR.exists():
        print(f"Base directory not found: {BASE_REPORT_DIR}")
        return

    best_path, max_equity = find_max_equity(BASE_REPORT_DIR)
    if best_path is None or max_equity is None:
        print("No valid backtest summaries found.")
        return

    print(f"Max equity_end: {max_equity}")
    print(f"Directory: {best_path}")


if __name__ == "__main__":
    main()
