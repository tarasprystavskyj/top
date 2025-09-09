#!/usr/bin/env python3
"""One-off helper to merge combined_cache_session.db files from multiple livecfg runs.

This script invokes the existing `merge_rebuild_cache.py` utility with predefined
paths so you don't have to type them manually each time.

Source databases:
  obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_203155/paper_api_results_5m/combined_cache_session.db
  obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_205847/paper_api_results_5m/combined_cache_session.db
  obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_213714/livecfg_cfg_avaai_t5m5000_3_5m/combined_cache_session.db
  obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_213932/livecfg_cfg_avaai_t5m5000_3_5m/combined_cache_session.db

Destination database:
  obw_platform/_reports/_live/livecfg_cfg_avaai_t5m5000_3_5m/combined_cache_session.db

Run this script from the repository root:
  python3 merge_cache_session_once.py
"""
from __future__ import annotations

import subprocess
from pathlib import Path


def main() -> None:
    repo_root = Path(__file__).resolve().parent

    sources = [
        repo_root
        / "obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_203155/paper_api_results_5m/combined_cache_session.db",
        repo_root
        / "obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_205847/paper_api_results_5m/combined_cache_session.db",
        repo_root
        / "obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_213714/livecfg_cfg_avaai_t5m5000_3_5m/combined_cache_session.db",
        repo_root
        / "obw_platform/_reports/_live/livecfg_avaai_t5m5000_320250908_213932/livecfg_cfg_avaai_t5m5000_3_5m/combined_cache_session.db",
    ]

    destination = (
        repo_root
        / "obw_platform/_reports/_live/livecfg_cfg_avaai_t5m5000_3_5m/combined_cache_session.db"
    )

    # Ensure the destination directory exists
    destination.parent.mkdir(parents=True, exist_ok=True)

    cmd = ["python3", "merge_rebuild_cache.py", "-o", str(destination)] + [
        str(p) for p in sources
    ]

    subprocess.check_call(cmd)


if __name__ == "__main__":
    main()
