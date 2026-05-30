# FastAPI MVP backend
import os, json, uuid, subprocess, threading, queue, time, shutil, yaml, glob, itertools, copy, re, sys, sqlite3
from datetime import datetime
from typing import Any, Dict, Optional, List, Tuple
from fastapi import FastAPI, HTTPException, Body, Query, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import logging
from dotenv import load_dotenv
import pandas as pd
import numpy as np
try:  # Python 3.9+
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - fallback for Python < 3.9
    ZoneInfo = None
    try:
        from backports.zoneinfo import ZoneInfo as _ZoneInfo

        ZoneInfo = _ZoneInfo
    except Exception:  # pragma: no cover - timezone support unavailable
        pass

log = logging.getLogger(__name__)

# Load backend-local environment variables, e.g. DEPLOY_SECRET and REPO_PATH.
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

try:
    from obw_platform.engine.visualize_results_3 import (
        plot_equity_curves as _viz_plot,
        plot_equity_from_dataframe as _viz_plot_df,
    )
except Exception:  # pragma: no cover - best effort fallback
    _viz_plot_df = None
    try:
        # Prefer the variant with additional comments if available
        from obw_platform.engine.visualize_results_1 import plot_equity_curves as _viz_plot
    except Exception:  # pragma: no cover - best effort fallback
        try:
            from obw_platform.engine.visualize_results import plot_equity_curves as _viz_plot
        except Exception:  # pragma: no cover - missing dependency (e.g. matplotlib)
            _viz_plot = None
            _viz_plot_df = None

APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
REPO_ROOT = os.path.abspath(os.path.join(APP_ROOT, ".."))
BT_ROOT = os.path.join(REPO_ROOT, "obw_platform")
DATA_ROOT = os.path.join(APP_ROOT, "data")
MAIN_CONFIG_DIR = os.path.join(DATA_ROOT, "configs")
OBW_CONFIG_DIR = os.path.abspath(os.path.join(BT_ROOT, "configs"))
CONFIG_DIRS = [MAIN_CONFIG_DIR, OBW_CONFIG_DIR]
RUNS_DIR = os.path.join(DATA_ROOT, "runs")
UNIVERSE_DIR = os.path.abspath(os.path.join(BT_ROOT, "universe"))
# Cache DBs are stored in the repository root under ``DB``.
CACHE_DB_DIR = os.path.join(REPO_ROOT, "DB")
# Persisted performance profile that tracks how long backtests take so we can
# estimate progress for in-flight jobs.  We record the average runtime per 100
# bars per symbol and reuse that for future estimates.
PERF_STATS_FILE = os.path.join(DATA_ROOT, "perf_stats.json")
DEFAULT_SECONDS_PER_100_BARS_PER_SYMBOL = 0.5
# Live session reports are stored within the obw_platform project under
# ``_reports/_live``.  The previous implementation looked for them in the
# repository root, which resulted in an empty list being returned to the
# frontend.  Point to the correct location so available sessions appear in the
# UI selector.
LIVE_RESULTS_DIR = os.path.abspath(
    os.path.join(APP_ROOT, "..", "obw_platform", "_reports", "_live")
)
LIVE_TOP_REPORTS_DIR = os.path.abspath(os.path.join(REPO_ROOT, "_reports", "_live"))
LIVE_REPO_REPORTS_DIR = os.path.abspath(os.path.join(REPO_ROOT, "reports"))
LIVE_VERONIKA_REPORTS_DIR = os.path.abspath(os.path.join(REPO_ROOT, "..", "veronika", "reports"))
BT_VERSION_FILE = os.path.join(DATA_ROOT, "backtester_version.yaml")
BACKTESTER_SCRIPTS = [
    "backtester_dual_long_short_fast_pack_v2.py",
    "backtester_dual_long_short_fast_pack.py",
    "backtester_dual_long_short_mtm.py",
    "backtester_core_speed3_veto_universe_4_mtm_unrealized_v5.py",
    "backtester_core_speed3_veto_universe_4_mtm_unrealized.py",
    "backtester_core_speed3_veto_universe_2.py",
    "backtester_core_speed3_veto_universe.py",
    "backtester_core_speed2.py",
    "backtester_core_speed3.py",
    "backtester_core_speed3_veto.py",
    "backtester_core_v0.py",
    "backtester_core_v1.py",
]

# Map of optional features supported by each backtester script.  This helps
# us only pass CLI flags that a particular backtester understands to avoid
# "unrecognized arguments" errors.
BACKTESTER_CAPABILITIES: Dict[str, Dict[str, bool]] = {
    "backtester_dual_long_short_fast_pack_v2.py": {"plots": True, "time_range": True, "npz": True, "json_stdout": True, "export_curves": True},
    "backtester_dual_long_short_fast_pack.py": {"plots": True, "time_range": True, "npz": True, "json_stdout": True},
    "backtester_dual_long_short_mtm.py": {"plots": True, "time_range": True, "cache_db": True, "json_stdout": True},
    "backtester_core_speed3_veto_universe_2.py": {"plots": True, "time_range": True, "export_csv": True},
    "backtester_core_speed3_veto_universe.py": {"plots": True},
    "backtester_core_speed2.py": {"cwd_outputs": True},
    "backtester_core_speed3.py": {"plots": True},
    "backtester_core_speed3_veto.py": {"plots": True},
}
_BACKTESTER_CAP_CACHE: Dict[str, Dict[str, bool]] = {}


def _safe_backtester_path(script: str) -> str:
    name = os.path.basename(str(script or ""))
    if not name or name not in BACKTESTER_SCRIPTS:
        raise HTTPException(404, "backtester not found")
    path = os.path.abspath(os.path.join(BT_ROOT, name))
    if not _is_within(path, BT_ROOT) or not os.path.isfile(path):
        raise HTTPException(404, "backtester not found")
    return path


def get_backtester_capabilities(script: Optional[str]) -> Dict[str, bool]:
    """Return CLI capabilities for a backtester, detected from --help when possible."""

    name = os.path.basename(str(script or load_backtester_version()))
    if name in _BACKTESTER_CAP_CACHE:
        return dict(_BACKTESTER_CAP_CACHE[name])
    caps = dict(BACKTESTER_CAPABILITIES.get(name, {}))
    path = os.path.join(BT_ROOT, name)
    if os.path.isfile(path):
        try:
            p = subprocess.run(
                [sys.executable or "python3", name, "--help"],
                cwd=BT_ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                timeout=8,
            )
            help_text = p.stdout or ""
            flag_map = {
                "debug": "--debug",
                "plots": "--plots",
                "time_range": "--time-from",
                "cache_db": "--cache_db",
                "npz": "--npz",
                "symbol": "--symbol",
                "symbols_file": "--symbols-file",
                "allow_symbols": "--allow-symbols",
                "export_csv": "--export-csv",
                "export_curves": "--export-curves",
            }
            for key, flag in flag_map.items():
                if flag in help_text:
                    caps[key] = True
        except Exception:
            pass
    _BACKTESTER_CAP_CACHE[name] = dict(caps)
    return caps


def _timeframe_to_minutes(value: Any) -> Optional[int]:
    """Best-effort conversion of timeframe representations to minutes."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        if value <= 0:
            return None
        return int(value)
    if isinstance(value, str):
        text = value.strip().lower()
        if not text:
            return None
        match = re.fullmatch(r"(?i)(\d+(?:\.\d+)?)([a-z]*)", text)
        if not match:
            return None
        amount = float(match.group(1))
        unit = match.group(2).lower()
        unit_map = {
            "": 1,
            "m": 1,
            "min": 1,
            "mins": 1,
            "minute": 1,
            "minutes": 1,
            "h": 60,
            "hr": 60,
            "hrs": 60,
            "hour": 60,
            "hours": 60,
            "d": 1440,
            "day": 1440,
            "days": 1440,
            "w": 10080,
            "week": 10080,
            "weeks": 10080,
        }
        factor = unit_map.get(unit)
        if factor is None or amount <= 0:
            return None
        minutes = int(amount * factor)
        return minutes if minutes > 0 else None
    return None


def _extract_timeframe_minutes(data: Any) -> Optional[int]:
    """Walk a mapping/list to discover a timeframe definition."""

    if isinstance(data, dict):
        for key in ("timeframe", "tf", "bar_tf", "bar_timeframe", "bar_interval", "interval"):
            minutes = _timeframe_to_minutes(data.get(key))
            if minutes:
                return minutes
        for key in ("session", "strategy", "strategy_params", "engine", "runner", "data", "params"):
            minutes = _extract_timeframe_minutes(data.get(key))
            if minutes:
                return minutes
    elif isinstance(data, (list, tuple)):
        for item in data:
            minutes = _extract_timeframe_minutes(item)
            if minutes:
                return minutes
    return None

def _sqlite_table_names(path: str) -> List[str]:
    try:
        con = sqlite3.connect(path)
        try:
            rows = con.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
            return [str(row[0]) for row in rows if row and row[0]]
        finally:
            con.close()
    except Exception:
        return []


# --- helpers: cache DB discovery -------------------------------------------
def _list_cache_db_files() -> List[Dict[str, Any]]:
    """Return available cache DB/NPZ files under ``CACHE_DB_DIR``.

    The deployment keeps cache databases in ``DB/``.  Expose both the display
    name (filename) and the repository-relative path so the frontend can pass a
    stable identifier while the backend resolves the absolute path before
    launching the backtester.
    """

    entries: List[Dict[str, str]] = []
    if not os.path.isdir(CACHE_DB_DIR):
        return entries
    try:
        candidates = sorted(os.scandir(CACHE_DB_DIR), key=lambda e: e.name.lower())
    except FileNotFoundError:  # pragma: no cover - directory removed between check and scan
        return entries
    for entry in candidates:
        if not entry.is_file():
            continue
        name = entry.name
        lower = name.lower()
        if not lower.endswith((".db", ".sqlite", ".sqlite3", ".npz")):
            continue
        rel_path = os.path.relpath(entry.path, REPO_ROOT)
        kind = "npz" if lower.endswith(".npz") else "db"
        label = f"[{kind.upper()}] {name}"
        item: Dict[str, Any] = {"name": label, "path": rel_path, "kind": kind}
        if kind == "db":
            item["tables"] = _sqlite_table_names(entry.path)
        entries.append(item)
    return entries


def _is_within(path: str, root: str) -> bool:
    try:
        common = os.path.commonpath([os.path.abspath(path), os.path.abspath(root)])
    except ValueError:
        return False
    return common == os.path.abspath(root)


def resolve_cache_db(value: Optional[str]) -> Optional[str]:
    """Resolve a cache DB/NPZ selector value to an absolute path.

    ``value`` may be an absolute path, a repository-relative path, or just the
    filename present in ``DB/``.  Only paths that stay within the repository
    root are accepted to avoid leaking files outside of the project.
    """

    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    normalized = raw.replace("\\", os.sep)
    candidates = []
    if os.path.isabs(normalized):
        candidates.append(normalized)
    else:
        # As-is relative path (e.g. "DB/foo.db" or "../obw_platform/...").
        candidates.append(os.path.join(REPO_ROOT, normalized))
        # Relative to the DB directory when only a filename is provided.
        candidates.append(os.path.join(CACHE_DB_DIR, normalized))
        candidates.append(os.path.join(CACHE_DB_DIR, os.path.basename(normalized)))
        # Relative to the backtester root as a final fallback.
        candidates.append(os.path.join(BT_ROOT, normalized))
    seen = set()
    for cand in candidates:
        full = os.path.abspath(cand)
        if full in seen:
            continue
        seen.add(full)
        if not os.path.isfile(full):
            continue
        if _is_within(full, REPO_ROOT):
            return full
    return None


def _count_symbols_in_file(path: str) -> Optional[int]:
    try:
        count = 0
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                count += 1
        return count or None
    except Exception:
        return None


def _resolve_universe_path(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    normalized = raw.strip().replace("\\", os.sep)
    if not normalized:
        return None
    candidates = []
    if os.path.isabs(normalized):
        candidates.append(normalized)
    else:
        candidates.append(os.path.join(BT_ROOT, normalized))
        candidates.append(os.path.join(REPO_ROOT, normalized))
        candidates.append(os.path.join(UNIVERSE_DIR, normalized))
        base = os.path.basename(normalized)
        if base != normalized:
            candidates.append(os.path.join(UNIVERSE_DIR, base))
    seen = set()
    for cand in candidates:
        full = os.path.abspath(cand)
        if full in seen:
            continue
        seen.add(full)
        if os.path.isfile(full):
            return full
    return None


def _symbol_count_from_value(value: Any) -> Optional[int]:
    if isinstance(value, dict):
        symbols = value.get("symbols")
        if isinstance(symbols, (list, tuple, set)):
            items = [s for s in symbols if s]
            if items:
                return len(items)
        for key in ("file", "path", "symbols_file", "symbols_path"):
            path = value.get(key)
            if isinstance(path, str):
                resolved = _resolve_universe_path(path)
                if resolved:
                    cnt = _count_symbols_in_file(resolved)
                    if cnt:
                        return cnt
    elif isinstance(value, (list, tuple, set)):
        items = [s for s in value if s]
        if items:
            return len(items)
    elif isinstance(value, str):
        resolved = _resolve_universe_path(value)
        if resolved:
            cnt = _count_symbols_in_file(resolved)
            if cnt:
                return cnt
    return None


def _estimate_symbol_count(meta: Dict[str, Any]) -> int:
    override = meta.get("override") or {}
    if isinstance(override, dict):
        for key in ("allow_symbols", "symbols"):
            cnt = _symbol_count_from_value(override.get(key))
            if cnt:
                return max(1, cnt)
        for key in ("symbols_file", "universe_file"):
            path = override.get(key)
            if isinstance(path, str):
                resolved = _resolve_universe_path(path)
                if resolved:
                    cnt = _count_symbols_in_file(resolved)
                    if cnt:
                        return max(1, cnt)
        uni_cnt = _symbol_count_from_value(override.get("universe"))
        if uni_cnt:
            return max(1, uni_cnt)
    cfg_name = meta.get("cfg_name")
    cfg_path = find_config(cfg_name) if cfg_name else None
    if cfg_path and os.path.isfile(cfg_path):
        try:
            cfg_data = yaml.safe_load(open(cfg_path, "r")) or {}
        except Exception:
            cfg_data = {}
        if isinstance(cfg_data, dict):
            for key in ("universe", "symbols", "allow_symbols"):
                cnt = _symbol_count_from_value(cfg_data.get(key))
                if cnt:
                    return max(1, cnt)
    return 1


def _seconds_per_100_bars_per_symbol() -> float:
    with perf_lock:
        val = perf_stats.get("per_100_per_symbol")
    if isinstance(val, (int, float)) and val > 0:
        return float(val)
    return DEFAULT_SECONDS_PER_100_BARS_PER_SYMBOL


def _estimate_expected_duration(limit_bars: int, symbol_count: int) -> float:
    per_unit = _seconds_per_100_bars_per_symbol()
    bars = max(1, int(limit_bars or 0))
    symbols = max(1, int(symbol_count or 0))
    units = (bars / 100.0) * symbols
    return per_unit * units


def _update_perf_profile(duration: float, limit_bars: int, symbol_count: int) -> None:
    if duration <= 0 or limit_bars <= 0 or symbol_count <= 0:
        return
    units = (limit_bars / 100.0) * symbol_count
    if units <= 0:
        return
    per_unit = duration / units
    if per_unit <= 0:
        return
    with perf_lock:
        current = perf_stats.get("per_100_per_symbol")
        samples = perf_stats.get("samples", 0) or 0
        if isinstance(samples, int) and samples >= 0 and isinstance(current, (int, float)) and current > 0:
            new_val = (current * samples + per_unit) / (samples + 1)
        else:
            new_val = per_unit
            samples = 0
        perf_stats["per_100_per_symbol"] = new_val
        perf_stats["samples"] = samples + 1
        _save_perf_stats({"per_100_per_symbol": new_val, "samples": samples + 1})


# --- helpers: live equity from session.sqlite --------------------------------
def _session_equity_df(session_db):
    import sqlite3, json, pandas as pd, numpy as np
    if not os.path.exists(session_db):
        return None

    con = sqlite3.connect(session_db)
    cur = con.cursor()

    # 0) read initial_equity from config_snapshots
    init_eq = 100.0
    try:
        row = cur.execute(
            "SELECT cfg_json FROM config_snapshots ORDER BY ts_utc DESC LIMIT 1;"
        ).fetchone()
        if row and row[0]:
            snap = json.loads(row[0])
            init_eq = (
                snap.get("initial_equity")
                or snap.get("portfolio", {}).get("initial_equity")
                or 100.0
            )
    except Exception:
        pass

    # 1) try reconstructing from closed trades/positions (PREFERRED)
    tbl = None
    for name in ("open_positions", "positions"):
        try:
            cur.execute(f"SELECT 1 FROM {name} LIMIT 1;")
            tbl = name
            break
        except Exception:
            continue

    if tbl:
        has_status = "status" in [r[1] for r in cur.execute(f"PRAGMA table_info({tbl});").fetchall()]
        has_fees = "fees_paid" in [r[1] for r in cur.execute(f"PRAGMA table_info({tbl});").fetchall()]
        sel = "side, qty, entry_fill, exit_fill, exit_fill_ts" + (", fees_paid" if has_fees else "")
        where = "WHERE exit_fill IS NOT NULL AND exit_fill_ts IS NOT NULL"
        if has_status:
            where = "WHERE status='CLOSED' AND exit_fill IS NOT NULL AND exit_fill_ts IS NOT NULL"

        df = pd.read_sql(f"SELECT {sel} FROM {tbl} {where} ORDER BY exit_fill_ts;", con)
        if not df.empty:
            df["ts"] = pd.to_datetime(df["exit_fill_ts"], errors="coerce", utc=True)
            for c in ("qty", "entry_fill", "exit_fill"):
                df[c] = pd.to_numeric(df[c], errors="coerce")
            if has_fees:
                df["fees_paid"] = pd.to_numeric(df["fees_paid"], errors="coerce").fillna(0.0)

            pnl = np.where(
                df["side"].str.upper() == "LONG",
                (df["exit_fill"] - df["entry_fill"]) * df["qty"],
                (df["entry_fill"] - df["exit_fill"]) * df["qty"],
            )
            if has_fees:
                pnl = pnl - df["fees_paid"]

            out = pd.DataFrame({"ts": df["ts"], "equity": init_eq + pnl.cumsum()})
            out = out.dropna(subset=["ts"]).sort_values("ts")
            try:
                out.attrs["initial_equity"] = float(init_eq)
            except Exception:
                pass
            con.close()
            return out

    # 2) Fallback: use snapshots from `equity` table (may actually be PnL)
    try:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(equity);").fetchall()]
    except Exception:
        cols = []
    if cols:
        try:
            df = pd.read_sql("SELECT * FROM equity ORDER BY 1;", con)
            if not df.empty:
                tcol = next((c for c in df.columns if c.lower() in ("ts", "ts_utc", "time", "timestamp")), None)
                vcol = next((c for c in df.columns if c.lower() in ("equity", "equity_usdt", "value", "pnl")), None)
                if tcol and vcol:
                    df = df[[tcol, vcol]].rename(columns={tcol: "ts", vcol: "equity"})
                    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
                    df = df.dropna(subset=["ts", "equity"]).sort_values("ts")
                    # Heuristic: if equity looks like small PnL around zero – convert to equity by adding init_eq
                    try:
                        rng = float(df["equity"].max() - df["equity"].min())
                        looks_like_pnl = (df["equity"].abs().quantile(0.9) < init_eq * 0.2) and (rng < init_eq * 0.5)
                    except Exception:
                        looks_like_pnl = False
                    if looks_like_pnl:
                        df["equity"] = init_eq + df["equity"]
                    try:
                        df.attrs["initial_equity"] = float(init_eq)
                    except Exception:
                        pass
                    con.close()
                    return df
        except Exception:
            pass

    con.close()
    return None


def _session_closed_trades(session_db):
    """Return closed trades from session.sqlite as a list of dicts."""
    import sqlite3
    import pandas as pd
    import numpy as np

    if not os.path.exists(session_db):
        return None
    con = sqlite3.connect(session_db)
    cur = con.cursor()
    tbl = None
    for name in ("open_positions", "positions"):
        try:
            cur.execute(f"SELECT 1 FROM {name} LIMIT 1;")
            tbl = name
            break
        except Exception:
            continue
    if not tbl:
        con.close()
        return None
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({tbl});").fetchall()]
    has_status = "status" in cols
    has_fees = "fees_paid" in cols
    has_close_reason = "close_reason" in cols
    sel_cols = [
        "symbol",
        "side",
        "qty",
        "entry_fill",
        "entry_fill_ts",
        "exit_fill",
        "exit_fill_ts",
        "ts_close",
    ]
    if has_close_reason:
        sel_cols.insert(sel_cols.index("ts_close"), "close_reason")
    if has_fees:
        sel_cols.append("fees_paid")
    extra_cols = [
        col
        for col in (
            "entry_slip_bp",
            "entry_lag_sec",
            "exit_slip_bp",
            "exit_lag_sec",
        )
        if col in cols
    ]
    sel_cols.extend(extra_cols)
    sel = ", ".join(sel_cols)
    where = "WHERE exit_fill IS NOT NULL AND exit_fill_ts IS NOT NULL"
    if has_status:
        where = (
            "WHERE status='CLOSED' AND exit_fill IS NOT NULL "
            "AND exit_fill_ts IS NOT NULL"
        )
    df = pd.read_sql(
        f"SELECT {sel} FROM {tbl} {where} ORDER BY exit_fill_ts;", con
    )
    try:
        orders_df = pd.read_sql(
            "SELECT symbol, ts_utc, reason FROM orders WHERE mode='EXIT';",
            con,
        )
    except Exception:
        orders_df = None
    con.close()
    if df.empty:
        return None

    def _clean_reason(value):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            return ""
        text = str(value).strip()
        return "" if text.lower() in {"", "nan", "none", "null", "nat"} else text

    if has_close_reason and "close_reason" in df.columns:
        df["close_reason"] = df["close_reason"].apply(_clean_reason)
    else:
        df["close_reason"] = pd.Series([""] * len(df), dtype="object")
    for c in (
        "qty",
        "entry_fill",
        "exit_fill",
        "fees_paid",
        "entry_slip_bp",
        "entry_lag_sec",
        "exit_slip_bp",
        "exit_lag_sec",
    ):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["realised_pnl"] = np.where(
        df["side"].str.upper() == "LONG",
        (df["exit_fill"] - df["entry_fill"]) * df["qty"],
        (df["entry_fill"] - df["exit_fill"]) * df["qty"],
    )
    if has_fees and "fees_paid" in df:
        df["realised_pnl"] = df["realised_pnl"] - df["fees_paid"]
    # map close reasons from recorded exit orders
    try:
        missing_mask = df["close_reason"].astype(str).str.strip() == ""
        if orders_df is not None and not orders_df.empty and missing_mask.any():
            orders_df = orders_df.dropna(subset=["symbol", "ts_utc"])
            orders_df["symbol"] = orders_df["symbol"].astype(str)
            orders_df["ts_utc"] = orders_df["ts_utc"].astype(str)
            orders_df["_key"] = orders_df["symbol"] + "|" + orders_df["ts_utc"]
            reason_map = dict(
                zip(
                    orders_df["_key"],
                    orders_df["reason"].where(orders_df["reason"].notna(), None),
                )
            )

            def _clean(value):
                if value is None or pd.isna(value):
                    return ""
                text = str(value).strip()
                return "" if text.lower() in {"", "nan", "none", "nat"} else text

            keys = []
            for _, row in df.iterrows():
                sym = _clean(row.get("symbol"))
                ts_val = _clean(row.get("exit_fill_ts")) or _clean(row.get("ts_close"))
                keys.append(f"{sym}|{ts_val}" if sym and ts_val else "")
            if keys:
                # only fill rows where close_reason is still empty
                for idx, k in enumerate(keys):
                    if not k or not missing_mask.iloc[idx]:
                        continue
                    r = reason_map.get(k)
                    txt = _clean_reason(r)
                    if txt:
                        df.at[idx, "close_reason"] = txt
    except Exception:
        pass
    # final cleanup: enforce textual reasons
    df["close_reason"] = df["close_reason"].apply(_clean_reason)
    df = df.drop(columns=["ts_close"], errors="ignore")
    if "close_reason" in df.columns:
        cols = list(df.columns)
        cols.remove("close_reason")
        insert_at = cols.index("exit_fill_ts") + 1 if "exit_fill_ts" in cols else len(cols)
        cols.insert(insert_at, "close_reason")
        df = df[cols]
    return df.to_dict(orient="records")


def _make_live_plots(base_dir):
    """Generate basic live session plots from ``session.sqlite``."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates
    import numpy as np
    import pandas as pd

    session_db = os.path.join(base_dir, "session.sqlite")
    eq_df = _session_equity_df(session_db)
    trades = _session_closed_trades(session_db)
    if eq_df is None or eq_df.empty or not trades:
        return

    trade_df = pd.DataFrame(trades)
    for col in ("entry_fill", "exit_fill", "qty", "fees_paid", "realised_pnl", "realized_pnl"):
        if col in trade_df:
            trade_df[col] = pd.to_numeric(trade_df[col], errors="coerce").fillna(0.0)
    if "exit_fill_ts" in trade_df.columns:
        trade_df["exit_fill_ts"] = pd.to_datetime(trade_df["exit_fill_ts"], errors="coerce")
    ret = np.where(
        trade_df["side"].str.upper() == "LONG",
        (trade_df["exit_fill"] - trade_df["entry_fill"]) / trade_df["entry_fill"],
        (trade_df["entry_fill"] - trade_df["exit_fill"]) / trade_df["entry_fill"],
    )
    plt.figure(); plt.hist(ret, bins=30)
    plt.title("Distribution of Returns per Trade")
    plt.xlabel("Return per trade"); plt.ylabel("Count")
    plt.tight_layout(); plt.savefig(os.path.join(base_dir, "returns_hist.png"), dpi=140); plt.close()

    eq_series = pd.to_numeric(eq_df["equity"], errors="coerce")
    eq_series = eq_series.dropna()
    if eq_series.empty:
        return
    eq_curve = eq_series.to_numpy()

    produced = False
    if _viz_plot_df:
        try:
            pnl_col = "realised_pnl" if "realised_pnl" in trade_df.columns else "realized_pnl"
            if pnl_col not in trade_df.columns:
                trade_df[pnl_col] = np.where(
                    trade_df["side"].str.upper() == "LONG",
                    (trade_df["exit_fill"] - trade_df["entry_fill"]) * trade_df["qty"],
                    (trade_df["entry_fill"] - trade_df["exit_fill"]) * trade_df["qty"],
                )
                if "fees_paid" in trade_df.columns:
                    trade_df[pnl_col] = trade_df[pnl_col] - trade_df["fees_paid"]
            init_eq = eq_df.attrs.get("initial_equity")
            if init_eq is None and pnl_col in trade_df.columns:
                pnl_series = trade_df[pnl_col].astype(float)
                if not pnl_series.empty:
                    init_eq = float(eq_series.iloc[-1] - pnl_series.cumsum().iloc[-1])
            title_suffix = os.path.basename(base_dir.rstrip(os.sep)) or "live"
            paths = _viz_plot_df(
                trade_df,
                initial_equity=init_eq,
                time_column="exit_fill_ts" if "exit_fill_ts" in trade_df.columns else None,
                show=False,
                save_dir=base_dir,
                file_prefix="viz",
                title_suffix=title_suffix,
            )
            alias_map = {
                "equity_vs_trade": "equity_by_trade.png",
                "dd_vs_trade": "drawdown_by_trade.png",
                "equity_vs_time": "equity_by_time.png",
            }
            for key, dest in alias_map.items():
                src = paths.get(key)
                if src and os.path.exists(src):
                    try:
                        shutil.copyfile(src, os.path.join(base_dir, dest))
                    except Exception:
                        pass
            produced = True
        except Exception:
            log.exception("live_result %s: failed to render live equity plots", base_dir)

    if not produced:
        plt.figure(); plt.plot(range(len(eq_curve)), eq_curve)
        plt.title("Equity vs Trade #")
        plt.xlabel("Trade #"); plt.ylabel("Equity")
        plt.tight_layout(); plt.savefig(os.path.join(base_dir, "equity_by_trade.png"), dpi=140); plt.close()

        if len(eq_curve) > 1:
            peaks = np.maximum.accumulate(eq_curve)
            dd = (eq_curve - peaks) / peaks
            plt.figure(); plt.plot(range(len(dd)), dd)
            plt.title("Drawdown vs Trade #")
            plt.xlabel("Trade #"); plt.ylabel("Drawdown (fraction)")
            plt.tight_layout(); plt.savefig(os.path.join(base_dir, "drawdown_by_trade.png"), dpi=140); plt.close()

        plt.figure(figsize=(8, 4))
        plt.plot(eq_df["ts"], eq_curve)
        ax = plt.gca()
        ax.xaxis.set_major_locator(mdates.HourLocator())
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
        plt.xticks(rotation=45)
        plt.title("Live Equity vs Time")
        plt.xlabel("Time"); plt.ylabel("Equity")
        plt.tight_layout(); plt.savefig(os.path.join(base_dir, "equity_by_time.png"), dpi=160); plt.close()

def load_backtester_version() -> str:
    try:
        data = yaml.safe_load(open(BT_VERSION_FILE, "r")) or {}
        ver = data.get("version")
        if ver in BACKTESTER_SCRIPTS:
            return ver
    except Exception:
        pass
    return BACKTESTER_SCRIPTS[0]


def save_backtester_version(ver: str) -> None:
    try:
        with open(BT_VERSION_FILE, "w") as f:
            yaml.safe_dump({"version": ver}, f)
    except Exception:
        pass


def _load_perf_stats() -> Dict[str, Any]:
    if not os.path.isdir(DATA_ROOT):
        os.makedirs(DATA_ROOT, exist_ok=True)
    try:
        with open(PERF_STATS_FILE, "r") as f:
            data = json.load(f) or {}
        per_val = data.get("per_100_per_symbol")
        samples = int(data.get("samples", 0) or 0)
        if isinstance(per_val, (int, float)) and per_val > 0:
            return {"per_100_per_symbol": float(per_val), "samples": samples}
    except Exception:
        pass
    return {"per_100_per_symbol": None, "samples": 0}


def _save_perf_stats(stats: Dict[str, Any]) -> None:
    try:
        tmp_path = PERF_STATS_FILE + ".tmp"
        with open(tmp_path, "w") as f:
            json.dump(stats, f)
        os.replace(tmp_path, PERF_STATS_FILE)
    except Exception:
        pass


perf_stats: Dict[str, Any] = _load_perf_stats()
perf_lock = threading.Lock()

os.makedirs(DATA_ROOT, exist_ok=True)
os.makedirs(MAIN_CONFIG_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)
os.makedirs(UNIVERSE_DIR, exist_ok=True)

job_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
jobs: Dict[str, Dict[str, Any]] = {}
lock = threading.Lock()


def _mark_job_finished(job_id: str, success: bool) -> None:
    finished_at = time.time()
    duration: Optional[float] = None
    limit_bars: Optional[int] = None
    symbol_count: Optional[int] = None
    with lock:
        info = jobs.get(job_id)
        if not info:
            return
        timing = info.setdefault("timing", {})
        if "finished_at" not in timing:
            timing["finished_at"] = finished_at
        started_at = timing.get("started_at")
        if isinstance(started_at, (int, float)):
            duration = max(0.0, finished_at - float(started_at))
            timing["duration"] = duration
        if "limit_bars" in timing:
            limit_bars = timing.get("limit_bars")
        if limit_bars is None and isinstance(info.get("meta"), dict):
            limit_bars = info["meta"].get("limit_bars")
            if limit_bars is not None:
                timing["limit_bars"] = limit_bars
        if "symbol_count" in timing:
            symbol_count = timing.get("symbol_count")
        if symbol_count is None and isinstance(info.get("meta"), dict):
            symbol_count = info["meta"].get("symbol_count")
            if symbol_count is not None:
                timing["symbol_count"] = symbol_count
        info["progress"] = 1.0
    if success and duration and limit_bars and symbol_count:
        try:
            _update_perf_profile(float(duration), int(limit_bars), int(symbol_count))
        except Exception:
            pass


def worker():
    while True:
        job = job_q.get()
        if job is None: break
        jid = job["job_id"]
        with lock:
            jobs[jid]["status"] = "running"
            jobs[jid]["progress"] = max(jobs[jid].get("progress", 0.0) or 0.0, 0.0)
            timing = jobs[jid].setdefault("timing", {})
            timing.setdefault("started_at", time.time())
        try:
            if job["kind"] == "backtest":
                run_backtest(job)
                _mark_job_finished(jid, success=True)
            elif job["kind"] == "grid":
                run_grid(job)
                with lock:
                    jobs[jid]["progress"] = 1.0
            with lock:
                jobs[jid]["status"] = "done"
        except Exception as e:
            _mark_job_finished(jid, success=False)
            with lock:
                jobs[jid]["status"] = "error"
                jobs[jid]["message"] = str(e)
        finally:
            job_q.task_done()

threading.Thread(target=worker, daemon=True).start()

class BacktestReq(BaseModel):
    cfg_name: str
    limit_bars: int = 5000
    label: Optional[str] = None
    branch: Optional[str] = None
    cache_db: Optional[str] = None
    symbol: Optional[str] = None
    override: Optional[Dict[str, Any]] = None
    backtester: Optional[str] = None
    debug: bool = False

class GridAxis(BaseModel):
    path: str
    values: List[Any]

class GridReq(BaseModel):
    cfg_name: str
    limit_bars: int = 5000
    cache_db: Optional[str] = None
    grid: List[GridAxis]


class LiveRunReq(BaseModel):
    cfg_name: str
    exchange: str = "bingx"
    universe_file: str
    debug: bool = False

def deep_update(d, path, value):
    cur = d
    keys = path.split(".")
    for k in keys[:-1]:
        if k not in cur or not isinstance(cur[k], dict):
            cur[k] = {}
        cur = cur[k]
    cur[keys[-1]] = value

def apply_overrides(cfg: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(cfg)
    for k,v in (override or {}).items():
        if isinstance(v, dict) and "." not in k:
            base = out.get(k, {}) if isinstance(out.get(k), dict) else {}
            base = apply_overrides(base, v)
            out[k] = base
        else:
            deep_update(out, k, v)
    return out

def cmd_backtester(
    cfg_path,
    limit_bars,
    cache_db=None,
    plots_dir=None,
    script=None,
    symbols_file=None,
    allow_symbols=None,
    symbol=None,
    time_from=None,
    time_to=None,
    export_csv=False,
    debug=False,
):
    """Build a command line for the selected backtester script.

    Only include CLI flags that the target backtester supports.  This keeps
    older implementations (e.g. speed2) from failing with "unrecognized"
    arguments when newer flags like ``--plots`` are present.
    """
    # run inside obw_platform so relative paths in configs resolve correctly
    bt_script = script or load_backtester_version()
    caps = get_backtester_capabilities(bt_script)
    python_bin = sys.executable or "python3"
    cmd = [python_bin, bt_script, "--cfg", cfg_path]
    if time_from and caps.get("time_range"):
        cmd += ["--time-from", time_from]
    if time_to and caps.get("time_range"):
        cmd += ["--time-to", time_to]
    if not (time_from or time_to):
        cmd += ["--limit-bars", str(limit_bars)]

    if cache_db:
        lower_cache = str(cache_db).lower()
        if lower_cache.endswith(".npz"):
            if caps.get("npz"):
                cmd += ["--npz", cache_db]
            else:
                raise RuntimeError(f"{bt_script} does not support NPZ input")
        else:
            if caps.get("cache_db"):
                cmd += ["--cache_db", cache_db]
            elif caps.get("npz"):
                raise RuntimeError(f"{bt_script} requires NPZ input, got cache DB")
    if symbols_file and caps.get("symbols_file"):
        cmd += ["--symbols-file", symbols_file]
    if allow_symbols and caps.get("allow_symbols"):
        if isinstance(allow_symbols, (list, tuple)):
            allow_symbols = ",".join(allow_symbols)
        cmd += ["--allow-symbols", allow_symbols]
    if symbol and caps.get("symbol"):
        cmd += ["--symbol", str(symbol)]

    # Only add --plots if the selected backtester advertises support for it
    if plots_dir and caps.get("plots"):
        cmd += ["--plots", plots_dir]
    if export_csv and caps.get("export_csv"):
        cmd += ["--export-csv"]
    if export_csv and caps.get("export_curves"):
        cmd += ["--export-curves", os.path.join(plots_dir or os.path.dirname(cfg_path), "curves.csv")]
    if debug and caps.get("debug"):
        cmd += ["--debug"]
    return cmd


def validate_backtester_input(bt_script: str, cache_path: Optional[str]) -> None:
    """Fail early for cache formats the selected backtester cannot consume."""

    caps = get_backtester_capabilities(bt_script)
    if cache_path:
        lower_cache = str(cache_path).lower()
        if lower_cache.endswith(".npz") and not caps.get("npz"):
            raise HTTPException(400, f"{bt_script} does not support NPZ input")
        if not lower_cache.endswith(".npz") and caps.get("npz") and not caps.get("cache_db"):
            raise HTTPException(400, f"{bt_script} requires NPZ input, got cache DB")
        required_tables = _required_sqlite_tables_for_backtester(bt_script, lower_cache)
        if required_tables:
            missing = [table for table in required_tables if not _sqlite_has_table(cache_path, table)]
            if missing:
                missing_text = ", ".join(missing)
                raise HTTPException(
                    400,
                    f"{bt_script} cannot use this DB: missing required table(s): {missing_text}. "
                    "Choose a compatible cache DB/NPZ for this backtester.",
                )
    elif caps.get("npz") and not caps.get("cache_db"):
        raise HTTPException(400, f"{bt_script} requires selecting an NPZ cache")


def _required_sqlite_tables_for_backtester(bt_script: str, cache_path_lower: str) -> List[str]:
    if cache_path_lower.endswith(".npz"):
        return []
    name = os.path.basename(str(bt_script or ""))
    if name.startswith("backtester_core_") or name == "backtester_dual_long_short_mtm.py":
        return ["price_indicators"]
    return []


def _sqlite_has_table(path: str, table: str) -> bool:
    try:
        con = sqlite3.connect(path)
        try:
            row = con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
                (table,),
            ).fetchone()
            return bool(row)
        finally:
            con.close()
    except Exception:
        return False


def _tail_text(path: str, max_chars: int = 5000) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            text = f.read()
        return text[-max_chars:]
    except Exception:
        return ""


def _diagnose_backtester_failure(log_text: str) -> Dict[str, str]:
    text = log_text or ""
    missing_db = re.search(r"DB file '([^']+)' not found", text)
    if missing_db:
        filename = missing_db.group(1)
        return {
            "kind": "missing_cache_db",
            "title": "Cache DB file is missing",
            "message": f"Backtester failed because the config points to missing DB file: {filename}.",
            "advice": "Select one of the available Cache DB / NPZ files in the form, or copy the required DB file into the project DB folder.",
        }
    missing_module = re.search(r"ModuleNotFoundError: No module named '([^']+)'", text)
    if missing_module:
        module = missing_module.group(1)
        return {
            "kind": "missing_dependency",
            "title": "Backend dependency is missing",
            "message": f"Backtester failed because Python module '{module}' is not installed in the backend environment.",
            "advice": "Install the dependency into UI/backend/.venv and restart the backend. Current backend should use that venv for new runs.",
        }
    missing_table = re.search(r"sqlite3\.OperationalError: no such table: ([A-Za-z0-9_]+)", text)
    if missing_table:
        table = missing_table.group(1)
        return {
            "kind": "incompatible_cache_schema",
            "title": "Selected DB is not compatible with this backtester",
            "message": f"Backtester expected table '{table}', but the selected SQLite DB does not contain it.",
            "advice": "Choose a DB built for this backtester, or use an NPZ-capable backtester with an NPZ cache.",
        }
    bad_args = re.search(r"unrecognized arguments?: (.+)", text)
    if bad_args:
        return {
            "kind": "unsupported_backtester_arguments",
            "title": "Backtester options are incompatible",
            "message": f"The selected backtester rejected CLI argument(s): {bad_args.group(1).strip()}",
            "advice": "Select a matching backtester version for this config, or adjust the backend capability map.",
        }
    traceback_line = ""
    for line in reversed(text.splitlines()):
        if line.strip() and not line.startswith("  "):
            traceback_line = line.strip()
            break
    return {
        "kind": "backtester_runtime_error",
        "title": "Backtester runtime error",
        "message": traceback_line or "Backtester failed during execution.",
        "advice": "Open the run logs for the full traceback, then check config, selected cache, and selected backtester compatibility.",
    }


def find_config(name: str) -> Optional[str]:
    for d in CONFIG_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


def _repo_relative(path: str) -> str:
    try:
        if _is_within(path, REPO_ROOT):
            return os.path.relpath(path, REPO_ROOT)
    except Exception:
        pass
    return path


def _cache_kind(path: Optional[str]) -> Optional[str]:
    if not path:
        return None
    return "npz" if str(path).lower().endswith(".npz") else "db"


def _load_config_for_inference(path: str) -> Tuple[str, Dict[str, Any]]:
    raw = open(path, "r", encoding="utf-8", errors="replace").read()
    cfg = yaml.safe_load(raw) or {}
    return raw, cfg if isinstance(cfg, dict) else {}


def infer_cache_for_config_path(path: str) -> Optional[str]:
    """Return a project-relative cache DB/NPZ when a config clearly maps to one."""

    try:
        raw, cfg = _load_config_for_inference(path)
    except Exception:
        return None
    for key in ("npz", "cache_npz", "cache_db", "cache_db_path"):
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            resolved = resolve_cache_db(val)
            if resolved:
                return _repo_relative(resolved)
    haystack = f"{os.path.basename(path)}\n{raw}".lower()
    candidates: List[str] = []
    if "freedommoney" in haystack:
        candidates.append("DB/fast_cache_1m_freedommoney_1y_bingx.npz")
    if "maxxing" in haystack:
        candidates.append("DB/fast_cache_1m_maxxing_1y_bingx.npz")
    for rel in candidates:
        resolved = resolve_cache_db(rel)
        if resolved:
            return _repo_relative(resolved)
    return None


def infer_symbol_for_config_path(path: str) -> Optional[str]:
    try:
        raw, cfg = _load_config_for_inference(path)
    except Exception:
        return None
    for key in ("symbol", "market_symbol", "backtest_symbol"):
        val = cfg.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    haystack = f"{os.path.basename(path)}\n{raw}".lower()
    if "freedommoney" in haystack:
        return "FREEDOMMONEY/USDT:USDT"
    if "maxxing" in haystack:
        return "MAXXING/USDT:USDT"
    if re.search(r"\bcheck\b", haystack):
        return "CHECK/USDT:USDT"
    return None


def infer_backtester_for_config_path(path: str) -> Optional[str]:
    """Return a backtester only when the config contains a reliable signal."""

    try:
        raw = open(path, "r", encoding="utf-8", errors="replace").read()
        cfg = yaml.safe_load(raw) or {}
    except Exception:
        return None
    if isinstance(cfg, dict):
        explicit = cfg.get("backtester_file") or cfg.get("backtester")
        if isinstance(explicit, str) and os.path.basename(explicit) in BACKTESTER_SCRIPTS:
            return os.path.basename(explicit)
    else:
        return None

    textual_hits = []
    for match in re.finditer(r"backtester[\w_\-]*\.py", raw):
        name = os.path.basename(match.group(0))
        if name in BACKTESTER_SCRIPTS:
            textual_hits.append(name)
    if textual_hits and len(set(textual_hits)) == 1:
        return textual_hits[0]
    if textual_hits:
        return None

    if isinstance(cfg, dict):
        cls_long = str(cfg.get("strategy_class_long") or "")
        cls_short = str(cfg.get("strategy_class_short") or "")
        cls_single = str(cfg.get("strategy_class") or "")
        cls_all = " ".join([cls_single, cls_long, cls_short])
        if "cryptomine_pack_dual" in cls_long or "cryptomine_pack_dual" in cls_short:
            return "backtester_dual_long_short_fast_pack_v2.py"
        if "cryptomine_c_limit14_even_dual" in cls_long or "cryptomine_c_limit14_even_dual" in cls_short:
            return "backtester_dual_long_short_mtm.py"
        if "strategy_class_long" in cfg or "strategy_class_short" in cfg:
            return None
        if "strategies.breakout_avaai_full.BreakoutAVAAIFull" in cls_all:
            return "backtester_core_speed2.py"
    return None


def _extract_json_object_from_text(text: str) -> Optional[Dict[str, Any]]:
    """Extract the last JSON object from a mixed log file."""

    decoder = json.JSONDecoder()
    candidates = []
    metric_keys = {
        "equity_start_total",
        "equity_end_realized_total",
        "equity_end_mtm_total",
        "realized_pnl_total",
        "total_pnl",
        "trades_total",
    }
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, end = decoder.raw_decode(text[i:])
        except Exception:
            continue
        if isinstance(obj, dict):
            candidates.append((bool(metric_keys.intersection(obj.keys())), end, obj))
    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]))
    return candidates[-1][2]


def _postprocess_backtest_outputs(out_dir: str, logs_path: str, bt_script: str, started_at: Optional[float] = None) -> None:
    """Normalize outputs from different backtesters for the Run UI."""

    # Some backtesters write JSON to stdout instead of summary.csv/trades.csv.
    try:
        text = open(logs_path, "r", encoding="utf-8", errors="replace").read()
    except Exception:
        text = ""
    parsed = _extract_json_object_from_text(text) if text else None
    if parsed:
        summary_json = os.path.join(out_dir, "summary.json")
        with open(summary_json, "w", encoding="utf-8") as f:
            json.dump(parsed, f, indent=2, ensure_ascii=False, default=str)
        summary_csv = os.path.join(out_dir, "summary.csv")
        flat = {k: v for k, v in parsed.items() if isinstance(v, (str, int, float, bool)) or v is None}
        pd.DataFrame([flat]).to_csv(summary_csv, index=False)

    # Normalize common file names produced by older dual backtesters.
    aliases = {
        "dual_summary.csv": "summary.csv",
        "bt_summary.csv": "summary.csv",
        "dual_trades.csv": "trades.csv",
        "bt_trades.csv": "trades.csv",
    }
    for src_name, dst_name in aliases.items():
        src = os.path.join(out_dir, src_name)
        dst = os.path.join(out_dir, dst_name)
        if os.path.exists(src) and not os.path.exists(dst):
            try:
                shutil.copyfile(src, dst)
            except Exception:
                pass

    # Older single-leg cores write summary/trades into BT_ROOT instead of the
    # requested run directory.
    if get_backtester_capabilities(bt_script).get("cwd_outputs"):
        for name in ("summary.csv", "trades.csv"):
            src = os.path.join(BT_ROOT, name)
            dst = os.path.join(out_dir, name)
            if not os.path.exists(src) or os.path.exists(dst):
                continue
            try:
                if started_at and os.path.getmtime(src) + 1 < started_at:
                    continue
                shutil.copyfile(src, dst)
            except Exception:
                pass

    if not glob.glob(os.path.join(out_dir, "*.png")):
        _make_summary_equity_plot(out_dir)


def _make_summary_equity_plot(out_dir: str) -> None:
    """Create a minimal equity plot for old backtesters that only emit summary.csv."""

    summary_path = os.path.join(out_dir, "summary.csv")
    if not os.path.exists(summary_path):
        return
    try:
        df = pd.read_csv(summary_path)
        if df.empty:
            return
        row = df.iloc[0].to_dict()
        start = row.get("equity_start") or row.get("equity_start_total")
        end = row.get("equity_end") or row.get("equity_end_mtm_total") or row.get("equity_end_realized_total")
        start = float(start)
        end = float(end)
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, ax = plt.subplots(figsize=(8, 4))
        ax.plot([0, 1], [start, end], marker="o")
        ax.set_title("Equity Summary")
        ax.set_xticks([0, 1], ["start", "end"])
        ax.set_ylabel("Equity")
        ax.grid(True, alpha=0.3)
        fig.tight_layout()
        fig.savefig(os.path.join(out_dir, "equity_by_trade.png"), dpi=140)
        plt.close(fig)
    except Exception:
        pass

def infer_config_from_session(name: str) -> Optional[str]:
    """Best-effort lookup of a config file based on a live session name.

    Many live result directories omit a copy of the configuration used to
    generate them.  They do, however, encode the config name in the directory
    itself (e.g. ``livecfg_cfg_avaai_t5m5000_3_5m``).  Walk backwards through
    the components of that suffix until we find a matching config file.
    """

    prefix = "livecfg_"
    if not name.startswith(prefix):
        return None
    suffix = name[len(prefix) :]
    parts = suffix.split("_")
    for i in range(len(parts), 0, -1):
        candidate = "_".join(parts[:i]) + ".yaml"
        cfg = find_config(candidate)
        if cfg:
            return cfg
    return None

def run_backtest(job):
    jid = job["job_id"]
    meta = job["meta"]
    out_dir = os.path.join(RUNS_DIR, jid); os.makedirs(out_dir, exist_ok=True)
    src = find_config(meta["cfg_name"])
    if not src:
        raise RuntimeError("Config not found")
    cfg_obj = yaml.safe_load(open(src,"r").read())
    merged = apply_overrides(cfg_obj, meta.get("override") or {})
    cfg_path = os.path.join(out_dir, "cfg_merged.yaml")
    bt_script = meta.get("backtester") or load_backtester_version()
    bt_caps = get_backtester_capabilities(bt_script)
    if meta.get("cache_db") and not (bt_caps.get("cache_db") or bt_caps.get("npz")):
        merged["cache_db"] = meta.get("cache_db")
    with open(cfg_path, "w") as f:
        yaml.safe_dump(merged, f, sort_keys=False)
    logs = os.path.join(out_dir, "logs.txt")
    cmd = cmd_backtester(
        cfg_path,
        meta["limit_bars"],
        meta.get("cache_db"),
        out_dir,
        bt_script,
        symbol=meta.get("symbol"),
        export_csv=True,
        debug=bool(meta.get("debug")),
    )
    cmd_str = " ".join(cmd)
    with lock:
        jobs.setdefault(jid, {}).setdefault("meta", {})
        jobs[jid]["cmd"] = cmd_str
        jobs[jid]["meta"].setdefault("cache_db", meta.get("cache_db"))
    started_at = time.time()
    with open(logs, "w") as lf:
        lf.write(f"[cmd] {cmd_str}\n")
        lf.flush()
        p = subprocess.Popen(cmd, cwd=BT_ROOT, stdout=lf, stderr=lf)
        p.wait()
    if p.returncode != 0:
        diagnosis = _diagnose_backtester_failure(_tail_text(logs))
        diagnosis["exit_code"] = str(p.returncode)
        with lock:
            jobs.setdefault(jid, {})["error_detail"] = diagnosis
        raise RuntimeError(diagnosis.get("message") or f"backtester failed with code {p.returncode}")
    save_backtester_version(bt_script)
    _postprocess_backtest_outputs(out_dir, logs, bt_script, started_at=started_at)
    # Generate extra visualization plots if possible
    if _viz_plot is not None:
        try:
            _viz_plot(
                trades_csv=os.path.join(out_dir, "bt_trades.csv"),
                summary_csv=os.path.join(out_dir, "bt_summary.csv") if os.path.exists(os.path.join(out_dir, "bt_summary.csv")) else None,
                show=False,
                save_dir=out_dir,
                file_prefix="viz",
            )
        except Exception:
            pass

def run_grid(job):
    jid = job["job_id"]
    req = job["meta"]["req"]
    base_cfg = yaml.safe_load(open(find_config(req["cfg_name"]),"r").read())
    out_dir = os.path.join(RUNS_DIR, jid); os.makedirs(out_dir, exist_ok=True)
    axes = req["grid"]
    paths = [a["path"] for a in axes]
    values = [a["values"] for a in axes]
    combos = list(itertools.product(*values))
    for i, combo in enumerate(combos, start=1):
        var = copy.deepcopy(base_cfg)
        for pth, val in zip(paths, combo):
            deep_update(var, pth, val)
        subdir = os.path.join(out_dir, f"{jid}_{i:03d}"); os.makedirs(subdir, exist_ok=True)
        cfg_path = os.path.join(subdir, "cfg_merged.yaml")
        with open(cfg_path,"w") as f: yaml.safe_dump(var, f, sort_keys=False)
        logs = os.path.join(subdir, "logs.txt")
        grid_cache = resolve_cache_db(req.get("cache_db")) if req.get("cache_db") else None
        cmd = cmd_backtester(
            cfg_path,
            req.get("limit_bars", 5000),
            grid_cache,
            export_csv=True,
        )
        with open(logs, "w") as lf:
            p = subprocess.Popen(
                cmd,
                cwd=BT_ROOT,
                stdout=lf,
                stderr=lf,
            )
            p.wait()
        if p.returncode != 0:
            raise RuntimeError(f"backtester failed with code {p.returncode}")

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Git deploy endpoints: /api/deploy/status and /api/deploy/pull
from git_deploy import router as deploy_router
app.include_router(deploy_router, prefix="/api", tags=["deploy"])

@app.get("/api/health")
def health(): return {"ok": True}

@app.get("/api/backtesters")
def backtesters():
    # Expose available backtester scripts along with their optional features so
    # the frontend can retain only supported parameters for a chosen
    # implementation.
    return {
        "versions": BACKTESTER_SCRIPTS,
        "current": load_backtester_version(),
        "capabilities": {name: get_backtester_capabilities(name) for name in BACKTESTER_SCRIPTS},
        "files": {
            name: {
                "path": os.path.join(BT_ROOT, name),
                "url": f"/api/backtesters/{name}/file",
            }
            for name in BACKTESTER_SCRIPTS
            if os.path.isfile(os.path.join(BT_ROOT, name))
        },
    }

@app.get("/api/configs")
def configs():
    out: Dict[str, Dict[str, Any]] = {}
    for d in CONFIG_DIRS:
        for p in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            st = os.stat(p)
            name = os.path.basename(p)
            bt_file = infer_backtester_for_config_path(p)
            suggested_cache = infer_cache_for_config_path(p)
            suggested_symbol = infer_symbol_for_config_path(p)
            item = {
                "name": name,
                "path": p,
                "updated_at": st.st_mtime,
                "backtester_file": bt_file,
            }
            if bt_file:
                item["backtester_url"] = f"/api/backtesters/{bt_file}/file"
            if suggested_cache:
                item["cache_db"] = suggested_cache
                item["cache_db_kind"] = _cache_kind(suggested_cache)
            else:
                try:
                    _, cfg_obj = _load_config_for_inference(p)
                except Exception:
                    cfg_obj = {}
                for key in ("npz", "cache_npz", "cache_db", "cache_db_path"):
                    val = cfg_obj.get(key)
                    if isinstance(val, str) and val.strip() and not resolve_cache_db(val.strip()):
                        item["cache_db_missing"] = val.strip()
                        break
            if suggested_symbol:
                item["symbol"] = suggested_symbol
            out[name] = item
    return list(out.values())


@app.get("/api/backtesters/{script}/file")
def backtester_file(script: str):
    return FileResponse(_safe_backtester_path(script), media_type="text/x-python", filename=os.path.basename(script))


@app.get("/api/cache_dbs")
def cache_dbs():
    return _list_cache_db_files()


@app.get("/api/configs/{name}")
def config_get(name: str):
    p = find_config(name)
    if not p:
        raise HTTPException(404, "not found")
    txt = open(p,"r").read()
    try: parsed = yaml.safe_load(txt)
    except Exception as e: parsed = {"_error": str(e)}
    return {"name": name, "yaml_text": txt, "parsed": parsed, "schema": {"title":"Config"}}

@app.put("/api/configs/{name}")
def config_put(name: str, body: Dict[str, Any] = Body(...)):
    p = os.path.join(MAIN_CONFIG_DIR, name)
    txt = body.get("yaml_text")
    if not isinstance(txt,str): raise HTTPException(400,"yaml_text must be string")
    try: yaml.safe_load(txt)
    except Exception as e: raise HTTPException(400, f"YAML error: {e}")
    open(p,"w").write(txt)
    return {"ok": True}

@app.get("/api/universes")
def universes():
    items = []
    for p in sorted(glob.glob(os.path.join(UNIVERSE_DIR, "*.txt"))):
        items.append(os.path.basename(p))
    return items


@app.post("/api/live_run")
def live_run(req: LiveRunReq):
    exchange = (req.exchange or "").strip().lower()
    if exchange not in {"bingx", "bybit"}:
        raise HTTPException(400, "exchange must be 'bingx' or 'bybit'")

    cfg_path = find_config(req.cfg_name)
    if not cfg_path:
        raise HTTPException(404, f"config not found: {req.cfg_name}")

    universe_name = (req.universe_file or "").strip()
    if not universe_name:
        raise HTTPException(400, "universe_file is required")
    universe_path = os.path.join(UNIVERSE_DIR, universe_name)
    if not os.path.isfile(universe_path):
        raise HTTPException(404, f"universe file not found: {universe_name}")

    rel_cfg = os.path.relpath(cfg_path, BT_ROOT)
    rel_universe = os.path.relpath(universe_path, BT_ROOT)
    rel_results = os.path.join("_reports", "_live", f"{exchange}_ena_bundle")
    os.makedirs(os.path.join(BT_ROOT, rel_results), exist_ok=True)
    logs_file = os.path.join(
        BT_ROOT,
        rel_results,
        f"live_runner_{exchange}_{time.strftime('%Y%m%d_%H%M%S')}.log",
    )

    cmd = [
        "python3",
        "bt_live_paper_runner_separated_universe_4.py",
        "--mode",
        "live",
        "--env-file",
        ".env",
        "--cfg",
        rel_cfg,
        "--exchange",
        exchange,
        "--symbol-format",
        "usdtm",
        "--poll-sec",
        "2",
        "--bar-delay-sec",
        "1",
        "--limit_klines",
        "300",
        "--prewarm-bars",
        "300",
        "--results-dir",
        rel_results,
        "--session-db",
        "session.sqlite",
        "--cache-out",
        "combined_cache_session.db",
        "--hour-cache",
        "save",
        "--universe-file",
        rel_universe,
    ]
    if req.debug:
        cmd.append("--debug")

    with open(logs_file, "w") as lf:
        proc = subprocess.Popen(cmd, cwd=BT_ROOT, stdout=lf, stderr=lf)

    return {
        "ok": True,
        "pid": proc.pid,
        "cmd": cmd,
        "logs_file": os.path.relpath(logs_file, BT_ROOT),
        "results_dir": rel_results,
    }

@app.post("/api/backtest")
def backtest(req: BacktestReq):
    req_meta = req.model_dump()
    cfg_path_for_req = find_config(req.cfg_name)
    if not cfg_path_for_req:
        raise HTTPException(404, f"config not found: {req.cfg_name}")
    cache_label = (req_meta.get("cache_db") or "").strip() or None
    if not cache_label:
        cache_label = infer_cache_for_config_path(cfg_path_for_req)
        if not cache_label:
            try:
                _, cfg_for_cache = _load_config_for_inference(cfg_path_for_req)
            except Exception:
                cfg_for_cache = {}
            embedded_cache = None
            for key in ("npz", "cache_npz", "cache_db", "cache_db_path"):
                val = cfg_for_cache.get(key)
                if isinstance(val, str) and val.strip():
                    embedded_cache = val.strip()
                    break
            if embedded_cache and not resolve_cache_db(embedded_cache):
                raise HTTPException(
                    400,
                    f"Config references cache DB/NPZ that is not available locally: {embedded_cache}. "
                    "Select an available Cache DB / NPZ from the form or add the missing file.",
                )
    resolved_cache = resolve_cache_db(cache_label) if cache_label else None
    if cache_label and not resolved_cache:
        raise HTTPException(400, f"cache db not found: {cache_label}")
    bt_script = req_meta.get("backtester") or load_backtester_version()
    validate_backtester_input(bt_script, resolved_cache)
    if not req_meta.get("symbol"):
        req_meta["symbol"] = infer_symbol_for_config_path(cfg_path_for_req)
    req_meta["cache_db_label"] = cache_label
    req_meta["cache_db"] = resolved_cache
    symbol_count = _estimate_symbol_count(req_meta)
    req_meta["symbol_count"] = symbol_count
    expected_duration = _estimate_expected_duration(req.limit_bars, symbol_count)
    req_meta["expected_duration_seconds"] = expected_duration
    jid = str(uuid.uuid4())
    timing_info = {
        "limit_bars": req.limit_bars,
        "symbol_count": symbol_count,
        "expected_duration": expected_duration,
        "created_at": time.time(),
    }
    with lock:
        jobs[jid] = {
            "status": "queued",
            "meta": req_meta,
            "kind": "backtest",
            "progress": 0.0,
            "timing": timing_info,
        }
    out_dir = os.path.join(RUNS_DIR, jid); os.makedirs(out_dir, exist_ok=True)
    meta = {
        "cfg_name": req.cfg_name,
        "limit_bars": req.limit_bars,
        "started_at": time.time(),
        "backtester": bt_script,
    }
    if resolved_cache:
        meta["cache_db"] = resolved_cache
    if cache_label and cache_label != resolved_cache:
        meta["cache_db_label"] = cache_label
    if req_meta.get("symbol"):
        meta["symbol"] = req_meta.get("symbol")
    if req_meta.get("debug"):
        meta["debug"] = True
    if req_meta.get("override"):
        meta["override"] = req_meta.get("override")
    meta["symbol_count"] = symbol_count
    meta["expected_duration_seconds"] = expected_duration
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f)
    job_q.put({"job_id": jid, "meta": req_meta, "kind":"backtest"})
    return {"job_id": jid}

@app.get("/api/jobs/{job_id}/status")
def status(job_id: str):
    with lock:
        job_info = copy.deepcopy(jobs.get(job_id))
    if not job_info:
        raise HTTPException(404, "job not found")
    resp: Dict[str, Any] = {
        "status": job_info.get("status"),
        "message": job_info.get("message"),
    }
    if isinstance(job_info.get("error_detail"), dict):
        resp["error_detail"] = job_info["error_detail"]
    meta = job_info.get("meta") or {}
    if isinstance(meta, dict):
        cfg_name = meta.get("cfg_name")
        if isinstance(cfg_name, str):
            resp["cfg_name"] = cfg_name
        override = meta.get("override")
        if isinstance(override, dict):
            resp["override"] = override
            if "universe_file" not in resp:
                uni_val = override.get("symbols_file") or override.get("universe_file")
                if isinstance(uni_val, str):
                    resp["universe_file"] = uni_val
                elif isinstance(override.get("universe"), dict):
                    uni_obj = override["universe"]
                    for key in ("file", "path"):
                        val = uni_obj.get(key)
                        if isinstance(val, str):
                            resp["universe_file"] = val
                            break
        cache_label = meta.get("cache_db_label")
        if cache_label:
            resp["cache_db_label"] = cache_label
        cache_path = meta.get("cache_db")
        if cache_path:
            resp["cache_db"] = cache_path
        backtester = meta.get("backtester")
        if isinstance(backtester, str):
            resp["backtester"] = backtester
    timing = job_info.get("timing") or {}
    progress = job_info.get("progress")
    expected = timing.get("expected_duration")
    started_at = timing.get("started_at")
    now = time.time()
    status_val = job_info.get("status")
    if status_val in ("done", "error"):
        progress = 1.0
    elif status_val == "running":
        if isinstance(started_at, (int, float)) and isinstance(expected, (int, float)) and expected > 0:
            elapsed = max(0.0, now - float(started_at))
            progress = max(progress or 0.0, min(0.99, elapsed / expected))
        elif progress is None:
            progress = 0.0
    elif progress is None:
        progress = 0.0
    if progress is not None:
        resp["progress"] = max(0.0, min(1.0, float(progress)))
    if isinstance(expected, (int, float)) and expected >= 0:
        resp["expected_duration_seconds"] = float(expected)
    if isinstance(started_at, (int, float)):
        elapsed = max(0.0, now - float(started_at))
        resp["started_at"] = float(started_at)
        resp["elapsed_seconds"] = elapsed
        if isinstance(expected, (int, float)) and expected > 0:
            remaining = max(0.0, expected - elapsed)
            resp["eta_seconds"] = remaining
            resp["progress"] = max(resp.get("progress", 0.0), min(1.0, elapsed / expected))
    symbol_count = timing.get("symbol_count")
    if isinstance(symbol_count, (int, float)):
        resp["symbol_count"] = int(symbol_count)
    limit_bars = timing.get("limit_bars")
    if isinstance(limit_bars, (int, float)):
        resp["limit_bars"] = int(limit_bars)
    return resp

@app.get("/api/jobs/{job_id}/result")
def result(job_id: str):
    out_dir = os.path.join(RUNS_DIR, job_id)
    if not os.path.isdir(out_dir): raise HTTPException(404, "out dir not found")
    arts = {}
    plot_files = [
        "returns_hist.png",
        "equity_by_trade.png",
        "equity_by_time.png",
        "drawdown_by_trade.png",
        "viz_equity_vs_trade.png",
        "viz_dd_vs_trade.png",
        "viz_equity_vs_time.png",
        "dual_equity_curve.png",
        "dual_mtm_pnl.png",
        "dual_pnl_panels_all.png",
        "dual_margin_call_excess.png",
    ]
    for fn in sorted(set(("summary.csv", "summary.json", "trades.csv", "cfg_merged.yaml", "logs.txt", *plot_files))):
        p = os.path.join(out_dir, fn)
        if os.path.exists(p):
            arts[fn] = f"/api/jobs/{job_id}/artifacts/{fn}"
    for p in sorted(glob.glob(os.path.join(out_dir, "*.png"))):
        fn = os.path.basename(p)
        arts.setdefault(fn, f"/api/jobs/{job_id}/artifacts/{fn}")
    summary = {}
    if "summary.csv" in arts:
        import csv
        with open(os.path.join(out_dir, "summary.csv")) as f:
            rows = list(csv.DictReader(f))
            if rows: summary = rows[0]
    elif "summary.json" in arts:
        try:
            with open(os.path.join(out_dir, "summary.json"), "r", encoding="utf-8") as f:
                summary = json.load(f)
        except Exception:
            summary = {}
    trades = []
    if "trades.csv" in arts:
        import csv
        with open(os.path.join(out_dir, "trades.csv")) as f:
            trades = list(csv.DictReader(f))[:500]
    resp: Dict[str, Any] = {"summary": summary, "trades": trades, "artifacts": arts}
    job_info = jobs.get(job_id) or {}
    if isinstance(job_info.get("error_detail"), dict):
        resp["error_detail"] = job_info["error_detail"]
    elif not summary and "logs.txt" in arts:
        diagnosis = _diagnose_backtester_failure(_tail_text(os.path.join(out_dir, "logs.txt")))
        if diagnosis:
            resp["error_detail"] = diagnosis
    job_meta = job_info.get("meta") or {}
    meta_path = os.path.join(out_dir, "meta.json")
    file_meta: Dict[str, Any] = {}
    if os.path.isfile(meta_path):
        try:
            with open(meta_path, "r") as mf:
                loaded = json.load(mf)
            if isinstance(loaded, dict):
                file_meta = loaded
        except Exception:
            file_meta = {}
    combined_meta: Dict[str, Any] = {}
    if isinstance(file_meta, dict):
        combined_meta.update(file_meta)
    if isinstance(job_meta, dict):
        combined_meta.update(job_meta)
    cfg_name = combined_meta.get("cfg_name")
    if isinstance(cfg_name, str):
        resp["cfg_name"] = cfg_name
    override_meta = combined_meta.get("override")
    if isinstance(override_meta, dict):
        resp["override"] = override_meta
        if "universe_file" not in resp:
            uni_val = override_meta.get("symbols_file") or override_meta.get("universe_file")
            if isinstance(uni_val, str):
                resp["universe_file"] = uni_val
            elif isinstance(override_meta.get("universe"), dict):
                uni_obj = override_meta["universe"]
                for key in ("file", "path"):
                    val = uni_obj.get(key)
                    if isinstance(val, str):
                        resp["universe_file"] = val
                        break
    cache_label = combined_meta.get("cache_db_label")
    if cache_label:
        resp["cache_db_label"] = cache_label
    cache_path = combined_meta.get("cache_db")
    if cache_path:
        resp["cache_db"] = cache_path
    backtester = combined_meta.get("backtester")
    if isinstance(backtester, str):
        resp["backtester"] = backtester
    debug_enabled = bool((job_meta or {}).get("debug") or (file_meta or {}).get("debug"))
    if debug_enabled:
        debug_info: Dict[str, Any] = {}
        cmd = job_info.get("cmd")
        if cmd:
            debug_info["cmd"] = cmd
        cache_db = job_meta.get("cache_db") or combined_meta.get("cache_db")
        if cache_db:
            debug_info["cache_db"] = cache_db
            debug_info["cache_db_exists"] = os.path.isfile(cache_db)
        cache_label = job_meta.get("cache_db_label") or combined_meta.get("cache_db_label")
        if cache_label and cache_label != cache_db:
            debug_info["cache_db_label"] = cache_label
        symbol_count = job_meta.get("symbol_count") or combined_meta.get("symbol_count")
        if isinstance(symbol_count, (int, float)):
            debug_info["symbol_count"] = int(symbol_count)
        expected_dbg = job_meta.get("expected_duration_seconds") or combined_meta.get(
            "expected_duration_seconds"
        )
        timing_info = job_info.get("timing") or {}
        if isinstance(expected_dbg, (int, float)):
            debug_info["expected_duration_seconds"] = float(expected_dbg)
        elif isinstance(timing_info.get("expected_duration"), (int, float)):
            debug_info["expected_duration_seconds"] = float(timing_info["expected_duration"])
        duration_val = timing_info.get("duration")
        if isinstance(duration_val, (int, float)):
            debug_info["duration_seconds"] = float(duration_val)
        if debug_info:
            resp["debug"] = debug_info
    return resp

@app.get("/api/jobs/{job_id}/artifacts/{name}")
def artifact(job_id: str, name: str):
    out_dir = os.path.join(RUNS_DIR, job_id)
    p = os.path.join(out_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    return FileResponse(p)

@app.get("/api/runs")
def runs(limit: int = 50):
    items = []
    for d in os.listdir(RUNS_DIR):
        meta_path = os.path.join(RUNS_DIR, d, "meta.json")
        if os.path.isfile(meta_path):
            meta = json.load(open(meta_path))
            items.append({"job_id": d, **meta})
    items.sort(key=lambda x: x.get("started_at", 0), reverse=True)
    return items[:limit]

@app.post("/api/grid")
def grid(req: GridReq):
    jid = str(uuid.uuid4())
    job_meta = {"req": req.model_dump()}
    with lock:
        jobs[jid] = {
            "status": "queued",
            "meta": job_meta,
            "kind": "grid",
            "progress": 0.0,
            "timing": {"created_at": time.time()},
        }
    job_q.put({"job_id": jid, "meta": job_meta, "kind": "grid"})
    return {"job_id": jid}


@app.get("/api/live_results")
def live_results():
    """List available live result directories."""
    if not os.path.isdir(LIVE_RESULTS_DIR):
        return []
    names = []
    for d in sorted(os.listdir(LIVE_RESULTS_DIR)):
        if os.path.isdir(os.path.join(LIVE_RESULTS_DIR, d)):
            names.append(d)
    return names


@app.get("/api/live_results/{name}")
def live_result(name: str, debug: int = Query(0)):
    """Return visualization artifacts for a live session along with an optional
    on-demand backtest of the same session data.

    The live session is expected to contain ``trades.csv`` and ``summary.csv``
    files produced by the running strategy.  If ``combined_cache_session.db``
    and a configuration file (matching ``cfg_*.yaml``) are present, the
    endpoint will also launch a backtest using that cached data and generate a
    comparable set of visualization images.  This avoids the need for the
    frontend to orchestrate a separate backtest run via the general ``/api/backtest``
    endpoint and keeps the API surface simple.
    """

    base = os.path.join(LIVE_RESULTS_DIR, name)
    if not os.path.isdir(base):
        raise HTTPException(404, "not found")

    # --- Live session visualisation -------------------------------------
    trades = os.path.join(base, "trades.csv")
    summary = os.path.join(base, "summary.csv")
    if _viz_plot and os.path.exists(trades):
        try:
            _viz_plot(
                trades_csv=trades,
                summary_csv=summary if os.path.exists(summary) else None,
                show=False,
                save_dir=base,
                file_prefix="viz",
            )
        except Exception:
            log.exception("live_result %s: failed to generate viz plots", name)
    try:
        _make_live_plots(base)
    except Exception:
        log.exception("live_result %s: failed to generate live equity plots", name)
    arts: Dict[str, str] = {}
    plot_candidates = {
        "returns_hist.png": ["returns_hist.png"],
        "equity_by_time.png": ["equity_by_time.png", "viz_equity_vs_time.png"],
        "equity_by_trade.png": ["equity_by_trade.png", "viz_equity_vs_trade.png"],
        "drawdown_by_trade.png": ["drawdown_by_trade.png", "viz_dd_vs_trade.png"],
    }
    for key, candidates in plot_candidates.items():
        for candidate in candidates:
            p = os.path.join(base, candidate)
            if os.path.exists(p):
                url = f"/api/live_results/{name}/files/{candidate}"
                arts[key] = url
                if candidate != key:
                    arts[candidate] = url
                break

    # --- Optional backtest using the same cache/config ------------------
    # Default structure returned when we cannot build a matching backtest.
    # ``summary`` is ``None`` instead of an empty dict so the frontend can
    # easily detect the absence of data and avoid showing an empty "{}" block.
    backtest = {"artifacts": {}, "summary": None, "trades": [], "logs": None, "time_range_text": None, "files": {}}
    cfg_candidates = sorted(glob.glob(os.path.join(base, "cfg_*.yaml")))
    cfg_path = cfg_candidates[0] if cfg_candidates else infer_config_from_session(name)
    cache_db = os.path.join(base, "combined_cache_session.db")

    allow_syms = None
    symbols_file = None
    session_db = os.path.join(base, "session.sqlite")
    kyiv_tz = ZoneInfo("Europe/Kyiv") if ZoneInfo else None
    live_range = None
    live_trades: List[Dict[str, Any]] = []
    tf_minutes: Optional[int] = None
    bt_time_from: Optional[str] = None
    bt_time_to: Optional[str] = None
    if os.path.exists(session_db):
        try:
            import sqlite3, json
            con = sqlite3.connect(session_db)
            cur = con.cursor()
            row = cur.execute(
                "SELECT cfg_json FROM config_snapshots ORDER BY ts_utc DESC LIMIT 1;"
            ).fetchone()
            if row and row[0]:
                snap = json.loads(row[0])
                if tf_minutes is None:
                    tf_minutes = _extract_timeframe_minutes(snap)
                allow_syms = snap.get("symbols_whitelist") or snap.get("universe", {}).get("allow")
                sym_file = snap.get("universe", {}).get("file")
                if sym_file and sym_file != "<cli>":
                    symbols_file = sym_file
            con.close()
        except Exception:
            log.exception("live_result %s: failed to read session db", name)
        try:
            lt = _session_closed_trades(session_db)
            if lt:
                live_trades = lt
        except Exception:
            log.exception("live_result %s: failed to extract live trades", name)
    trades_csv = os.path.join(base, "trades.csv")
    if not live_trades and os.path.exists(trades_csv):
        try:
            import csv
            with open(trades_csv) as f:
                live_trades = list(csv.DictReader(f))
            for t in live_trades:
                try:
                    entry = float(t.get("entry_fill") or 0)
                    exit_ = float(t.get("exit_fill") or 0)
                    qty = float(t.get("qty") or 0)
                    side = str(t.get("side") or "").upper()
                    fees = float(t.get("fees_paid") or 0)
                    pnl = (exit_ - entry) * qty if side == "LONG" else (entry - exit_) * qty
                    pnl -= fees
                    t["realised_pnl"] = pnl
                except Exception:
                    continue
        except Exception:
            log.exception("live_result %s: failed to parse trades.csv", name)

    if tf_minutes is None and cfg_path and os.path.isfile(cfg_path):
        try:
            with open(cfg_path, "r") as fh:
                cfg_payload = yaml.safe_load(fh) or {}
            if isinstance(cfg_payload, dict):
                tf_minutes = _extract_timeframe_minutes(cfg_payload)
        except Exception:
            pass

    if live_trades:
        try:
            import pandas as pd

            ts_series = pd.to_datetime(
                [t.get("exit_fill_ts") for t in live_trades], errors="coerce", utc=True
            ).dropna()
            if not ts_series.empty:
                tmin = ts_series.min()
                tmax = ts_series.max()
                bt_time_from = tmin.strftime("%Y-%m-%dT%H:%M:%SZ")
                tmax_for_bt = tmax
                if tf_minutes and tf_minutes > 0:
                    tmax_for_bt = tmax_for_bt + pd.Timedelta(minutes=tf_minutes)
                bt_time_to = tmax_for_bt.strftime("%Y-%m-%dT%H:%M:%SZ")
                if kyiv_tz is not None:
                    start = tmin.astimezone(kyiv_tz).strftime("%Y-%m-%d %H:%M")
                    end = tmax.astimezone(kyiv_tz).strftime("%Y-%m-%d %H:%M")
                    live_range = {"start": start, "end": end}
            if kyiv_tz is not None:
                for trade in live_trades:
                    ts_val = pd.to_datetime(trade.get("exit_fill_ts"), errors="coerce", utc=True)
                    if pd.notna(ts_val):
                        trade["exit_fill_ts"] = ts_val.astimezone(kyiv_tz).strftime("%Y-%m-%d %H:%M")
        except Exception:
            log.exception("live_result %s: failed to normalise live trade timestamps", name)

    bt_cmd = None
    bt_stdout = None
    if cfg_path and os.path.exists(cache_db):
        bt_plots = os.path.join(base, "bt_plots")
        if os.path.isdir(bt_plots):
            shutil.rmtree(bt_plots)
        os.makedirs(bt_plots, exist_ok=True)
        logs = os.path.join(base, "bt_logs.txt")
        cmd = cmd_backtester(
            cfg_path,
            5000,
            cache_db=cache_db,
            plots_dir=bt_plots,
            symbols_file=symbols_file,
            allow_symbols=allow_syms,
            time_from=bt_time_from,
            time_to=bt_time_to,
            export_csv=True,
            debug=bool(debug),
        )
        bt_cmd = " ".join(cmd)
        result = subprocess.run(cmd, cwd=BT_ROOT, capture_output=True, text=True)
        with open(logs, "w") as lf:
            lf.write(result.stdout)
            lf.write(result.stderr)
        bt_stdout = result.stdout
        bt_logs = f"/api/live_results/{name}/files/{os.path.basename(logs)}" if os.path.exists(logs) else None
        bt_arts: Dict[str, str] = {}
        bt_summary = None
        bt_trades = []
        time_range_text = None
        trades_path = summary_path = None
        for line in bt_stdout.splitlines():
            if line.startswith("[time range]"):
                time_range_text = line[len("[time range] "):].strip()
            if line.startswith("[files]"):
                parts = dict(p.split("=", 1) for p in line[7:].split())
                trades_path = parts.get("bt_trades")
                summary_path = parts.get("bt_summary")
        if trades_path and os.path.exists(trades_path):
            try:
                import csv
                with open(trades_path) as f:
                    bt_trades = list(csv.DictReader(f))
            except Exception:
                log.exception("live_result %s: failed to parse bt_trades", name)
                bt_trades = []
        if summary_path and os.path.exists(summary_path):
            try:
                import json as _json
                bt_summary = _json.load(open(summary_path))
            except Exception:
                log.exception("live_result %s: failed to parse bt_summary", name)
        if result.returncode == 0 and _viz_plot and trades_path and os.path.exists(trades_path):
            try:
                _viz_plot(
                    trades_csv=trades_path,
                    summary_csv=summary_path if summary_path and os.path.exists(summary_path) else None,
                    show=False,
                    save_dir=bt_plots,
                    file_prefix="bt_viz",
                )
            except Exception:
                log.exception("live_result %s: failed to generate backtest viz plots", name)
        core_files = [
            "returns_hist.png",
            "equity_by_trade.png",
            "equity_by_time.png",
            "drawdown_by_trade.png",
        ]
        for fn in core_files:
            pth = os.path.join(bt_plots, fn)
            if os.path.exists(pth):
                bt_arts[fn] = f"/api/live_results/{name}/files/bt_plots/{fn}"
        viz_map = {
            "bt_viz_equity_vs_trade.png": "viz_equity_vs_trade.png",
            "bt_viz_dd_vs_trade.png": "viz_dd_vs_trade.png",
            "bt_viz_equity_vs_time.png": "viz_equity_vs_time.png",
        }
        for src_name, key in viz_map.items():
            pth = os.path.join(bt_plots, src_name)
            if os.path.exists(pth):
                bt_arts[key] = f"/api/live_results/{name}/files/bt_plots/{src_name}"
        backtest = {
            "artifacts": bt_arts,
            "summary": bt_summary,
            "trades": bt_trades,
            "logs": bt_logs,
            "time_range_text": time_range_text,
            "files": {"bt_trades": trades_path, "bt_summary": summary_path},
        }
        
    bt_range = None
    bt_trades = backtest.get("trades") or []
    if bt_trades:
        t0 = bt_trades[0]
        t1 = bt_trades[-1]
        k = next((c for c in ("ts_utc", "ts") if c in t0), None)
        if k:
            bt_range = {"start": t0[k], "end": t1[k]}

    resp = {
        "artifacts": arts,
        "backtest": backtest,
        "live_range": live_range,
        "live_trades": live_trades,
        "bt_range": bt_range,
    }

    if debug:
        dbg = {
            "dir": base,
            "exists": os.path.isdir(base),
            "files": sorted(os.listdir(base)),
        }
        if bt_cmd:
            dbg["bt_cmd"] = bt_cmd
        if bt_stdout:
            dbg["bt_stdout"] = bt_stdout
        sdb = os.path.join(base, "session.sqlite")
        if os.path.exists(sdb):
            import sqlite3
            con = sqlite3.connect(sdb)
            cur = con.cursor()
            try:
                integ = cur.execute("PRAGMA integrity_check;").fetchone()[0]
            except Exception as e:
                integ = f"error:{e}"
            try:
                tabs = [
                    r[0]
                    for r in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type='table';"
                    ).fetchall()
                ]
            except Exception as e:
                tabs = [f"error:{e}"]
            counts: Dict[str, Any] = {}
            for t in tabs:
                try:
                    counts[t] = cur.execute(f"SELECT COUNT(*) FROM {t};").fetchone()[0]
                except Exception as e:
                    counts[t] = f"error:{e}"
            con.close()
            dbg["session_db"] = {
                "size_bytes": os.path.getsize(sdb),
                "integrity": integ,
                "tables": tabs,
                "counts": counts,
            }
        resp["debug"] = dbg

    return resp


TV_BACKTEST_SOURCE_DIR = os.path.abspath(os.path.join(REPO_ROOT, "obw_platform", "_reports", "TV_backtest_source"))
VALIDATION_REPORTS_DIR = os.path.abspath(os.path.join(REPO_ROOT, "obw_platform", "_reports", "backtest_live_validation"))
VALIDATION_UPLOADS_DIR = os.path.join(VALIDATION_REPORTS_DIR, "_uploads")
LIVE_SESSION_TABLE_KINDS = {"open_positions", "orders", "debug_events", "stdio"}


def _live_results_roots() -> List[str]:
    env_roots = os.environ.get("BACKTEST_LIVE_VALIDATION_LIVE_ROOTS") or os.environ.get("LIVE_RESULTS_ROOTS")
    candidates: List[str] = []
    if env_roots:
        candidates.extend([part.strip() for part in env_roots.split(os.pathsep)])
    candidates.extend([LIVE_RESULTS_DIR, LIVE_TOP_REPORTS_DIR, LIVE_REPO_REPORTS_DIR, LIVE_VERONIKA_REPORTS_DIR])
    out: List[str] = []
    for item in candidates:
        if item:
            out.append(os.path.abspath(item))
    return list(dict.fromkeys(out))


def _is_within_live_results_root(path: str) -> bool:
    return any(_is_within(path, root) for root in _live_results_roots())


def _active_status_json_path(session_dir: str) -> Optional[str]:
    marker = os.path.join(session_dir, "ACTIVE_STATUS_PATH.txt")
    if not os.path.isfile(marker):
        return None
    try:
        with open(marker, "r", encoding="utf-8", errors="replace") as fh:
            raw = fh.read().strip()
    except Exception:
        return None
    if not raw:
        return None
    path = raw if os.path.isabs(raw) else os.path.join(session_dir, raw)
    path = os.path.abspath(path)
    if not _is_within(path, session_dir) or not os.path.isfile(path):
        return None
    return path


def _looks_like_live_session_dir(session_dir: str) -> bool:
    markers = (
        "status.json",
        "session_status.json",
        "session.json",
        "meta.json",
        "summary.json",
        "config.json",
        "RUN_STATUS.json",
        "ACTIVE_STATUS_PATH.txt",
        "session.sqlite",
        "stdio.log",
        "stdout.log",
        "stderr.log",
    )
    if any(os.path.exists(os.path.join(session_dir, marker)) for marker in markers):
        return True
    return bool(glob.glob(os.path.join(session_dir, "live_stdout_*.log")))


def _has_live_runner_artifact(session_dir: str) -> bool:
    name = os.path.basename(os.path.abspath(session_dir)).lower()
    if "live" in name or "canary" in name:
        return True
    markers = (
        "RUN_STATUS.json",
        "ACTIVE_STATUS_PATH.txt",
        "ACTIVE_SESSION_DB_PATH.txt",
        "live_equity.csv",
        "live_chart_events.csv",
        "live_chart_events.jsonl",
        "live_mark_ohlcv.npz",
    )
    if any(os.path.exists(os.path.join(session_dir, marker)) for marker in markers):
        return True
    patterns = ("live_stdout_*.log", "run_telemetry_*.jsonl", "*ohlcv*.npz")
    return any(glob.glob(os.path.join(session_dir, pattern)) for pattern in patterns)


def _validation_source_candidates() -> List[str]:
    env_override = os.environ.get("TV_BACKTEST_SOURCE_DIR")
    candidates = [
        env_override,
        TV_BACKTEST_SOURCE_DIR,
        os.path.join(BT_ROOT, "_reports", "TV_backtest_source"),
        os.path.join(REPO_ROOT, "obw_platform", "_reports", "TV_backtest_source"),
        os.path.join(os.getcwd(), "obw_platform", "_reports", "TV_backtest_source"),
        "/workspace/top/obw_platform/_reports/TV_backtest_source",
    ]
    out: List[str] = []
    for item in candidates:
        if not item:
            continue
        out.append(os.path.abspath(item))
    # preserve order, remove duplicates
    return list(dict.fromkeys(out))


def _resolve_validation_source_dir() -> Optional[str]:
    for cand in _validation_source_candidates():
        if os.path.isdir(cand):
            return cand
    return None


def _safe_float(value: Any) -> float:
    try:
        v = float(value)
    except Exception:
        return 0.0
    if np.isnan(v) or np.isinf(v):
        return 0.0
    return v


def _extract_first_float(value: Any) -> float:
    txt = str(value or "").replace("−", "-").replace(",", ".")
    m = re.search(r"([+-]?\d+(?:\.\d+)?)", txt)
    return float(m.group(1)) if m else 0.0


def _parse_bingx_datetime_series(series: pd.Series) -> pd.Series:
    """Parse known BingX datetime variants with fallback."""
    s = series.astype(str).str.strip()
    out = pd.to_datetime(s, format="%m/%d/%y %I:%M %p", errors="coerce", utc=True)
    mask = out.isna()
    if mask.any():
        out.loc[mask] = pd.to_datetime(s.loc[mask], format="%d/%m/%y %H:%M", errors="coerce", utc=True)
    mask = out.isna()
    if mask.any():
        out.loc[mask] = pd.to_datetime(s.loc[mask], errors="coerce", dayfirst=True, utc=True)
    return out


def _infer_symbol_guess_from_name(name: str) -> Optional[str]:
    up = (name or "").upper()
    m = re.search(r'BINGX_([A-Z0-9]+)(USDT|USDC)\.P', up)
    if not m:
        m = re.search(r'([A-Z0-9]+)(USDT|USDC)\.P', up)
    if not m:
        return None
    return f"{m.group(1)}-{m.group(2)}"


def _dominant_interval_seconds(ts: pd.Series) -> Optional[int]:
    if ts is None or len(ts) < 2:
        return None
    d = ts.sort_values().diff().dt.total_seconds().dropna()
    d = d[d > 0]
    if d.empty:
        return None
    rounded = d.round().astype(int)
    mode = rounded.mode()
    if mode.empty:
        return int(max(1, round(float(d.median()))))
    return int(max(1, mode.iloc[0]))


def _format_interval_label(seconds: Optional[int]) -> str:
    if not seconds or seconds <= 0:
        return "unknown"
    if seconds % 86400 == 0:
        return f"{seconds // 86400}d"
    if seconds % 3600 == 0:
        return f"{seconds // 3600}h"
    if seconds % 60 == 0:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def _read_tv_csv(tv_path: str) -> pd.DataFrame:
    df = pd.read_csv(tv_path, encoding="utf-8-sig")
    if "Date and time" not in df.columns:
        raise ValueError("TradingView CSV must include 'Date and time'")
    df = df.copy()
    df["dt"] = pd.to_datetime(df["Date and time"], errors="coerce", utc=True)
    bad = int(df["dt"].isna().sum())
    if bad:
        raise ValueError(f"TradingView CSV has {bad} rows with invalid timestamps")
    return df


def _inspect_tv_path(tv_path: str) -> Dict[str, Any]:
    df = _read_tv_csv(tv_path)
    start = df["dt"].min()
    end = df["dt"].max()
    interval_seconds = _dominant_interval_seconds(df["dt"])
    symbol = _infer_symbol_guess_from_name(os.path.basename(tv_path))
    out = {
        "filename": os.path.basename(tv_path),
        "path": tv_path,
        "symbol": symbol,
        "start": start.isoformat() if pd.notna(start) else None,
        "end": end.isoformat() if pd.notna(end) else None,
        "bar_interval_seconds": interval_seconds,
        "bar_interval_label": _format_interval_label(interval_seconds),
        "rows": int(len(df)),
    }
    log.info("bt_live_validation inspect file=%s symbol=%s interval=%s start=%s end=%s", tv_path, symbol, out["bar_interval_label"], out["start"], out["end"])
    return out


def _validation_run_dir(run_id: str) -> str:
    return os.path.join(VALIDATION_REPORTS_DIR, run_id)


def _list_validation_source_files() -> Dict[str, Any]:
    src_dir = _resolve_validation_source_dir()
    if not src_dir:
        return {"source_dir": None, "searched_dirs": _validation_source_candidates(), "files": []}
    out: List[Dict[str, Any]] = []
    for entry in sorted(os.scandir(src_dir), key=lambda e: e.name.lower()):
        if not entry.is_file() or not entry.name.lower().endswith(".csv"):
            continue
        st = entry.stat()
        out.append(
            {
                "name": entry.name,
                "path": entry.path,
                "size": int(st.st_size),
                "modified_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime)),
                "symbol_guess": _infer_symbol_guess_from_name(entry.name),
            }
        )
    return {"source_dir": src_dir, "searched_dirs": _validation_source_candidates(), "files": out}


def _ensure_under(path: str, root: str) -> None:
    if not _is_within(path, root):
        raise HTTPException(status_code=400, detail="path is outside allowed root")


def _is_allowed_validation_path(path: str) -> bool:
    ap = os.path.abspath(path)
    if _is_within(ap, REPO_ROOT):
        return True
    if _is_within(ap, VALIDATION_UPLOADS_DIR):
        return True
    src_dir = _resolve_validation_source_dir()
    if src_dir and _is_within(ap, src_dir):
        return True
    return False


def _is_retryable_extract_error(stdout: str, stderr: str) -> bool:
    txt = f"{stdout or ''}\n{stderr or ''}".lower()
    return ("bingx error 106551" in txt) or ("bingxerror: bingx error 106551" in txt) or ("bingx error" in txt and "system error" in txt)


def _find_live_match_ready_csv(live_path: str, symbol: Optional[str]) -> Optional[str]:
    if not live_path:
        return None
    base = os.path.abspath(live_path)
    if not os.path.isdir(base):
        return None
    if not _is_within_live_results_root(base):
        return None
    symbol_safe = str(symbol or "").replace("-", "_")
    candidates = [
        os.path.join(base, f"{symbol_safe}_trade_history_for_match.csv") if symbol_safe else None,
        os.path.join(base, f"{symbol}_trade_history_for_match.csv") if symbol else None,
        os.path.join(base, "trade_history_for_match.csv"),
    ]
    candidates = [c for c in candidates if c and os.path.isfile(c)]
    if candidates:
        return candidates[0]
    for pattern in ("*_trade_history_for_match.csv", "*trade_history*.csv", "*fills*.csv"):
        hits = sorted(glob.glob(os.path.join(base, pattern)))
        if hits:
            return hits[0]
    return None


def _safe_read_json(path: str) -> Any:
    try:
        with open(path, "r", encoding="utf-8-sig") as fh:
            return json.load(fh)
    except Exception:
        return None


def _to_iso_from_any(value: Any) -> Optional[str]:
    if value is None:
        return None
    try:
        dt = pd.to_datetime(value, utc=True, errors="coerce")
        if pd.isna(dt):
            return None
        return dt.isoformat()
    except Exception:
        return None


def _session_meta_candidates(session_dir: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for name in ("status.json", "session_status.json", "session.json", "meta.json", "summary.json", "config.json", "RUN_STATUS.json"):
        p = os.path.join(session_dir, name)
        if os.path.isfile(p):
            data = _safe_read_json(p)
            if isinstance(data, dict):
                out.append(data)
    active_status_path = _active_status_json_path(session_dir)
    if active_status_path:
        data = _safe_read_json(active_status_path)
        if isinstance(data, dict):
            out.append(data)
    return out


def _meta_lookup(meta: Dict[str, Any], keys: List[str]) -> Any:
    for key in keys:
        if key in meta and meta.get(key) is not None:
            return meta.get(key)
    return None


def _extract_meta_fields(meta_list: List[Dict[str, Any]]) -> Dict[str, Any]:
    exchange = None
    timeframe = None
    status = None
    started_at = None
    updated_at = None
    open_legs = None
    filled_orders = None
    last_debug_event = None
    last_equity_ts = None
    for meta in meta_list:
        exchange = exchange or _meta_lookup(meta, ["exchange", "live_exchange", "venue", "market"])
        timeframe = timeframe or _meta_lookup(meta, ["timeframe", "tf", "interval", "bar_interval"])
        status = status or _meta_lookup(meta, ["status", "state", "runner_status"])
        started_at = started_at or _to_iso_from_any(_meta_lookup(meta, ["started_at", "started", "start_ts", "start_time"]))
        updated_at = updated_at or _to_iso_from_any(_meta_lookup(meta, ["updated_at", "last_updated", "heartbeat_ts", "utc", "ts", "timestamp"]))
        if open_legs is None:
            val = _meta_lookup(meta, ["open_legs", "open_positions", "open_count"])
            if isinstance(val, (int, float)):
                open_legs = int(val)
            elif isinstance(meta.get("open_paper_trades"), list):
                open_legs = len(meta["open_paper_trades"])
        if filled_orders is None:
            val = _meta_lookup(meta, ["filled_orders", "orders_filled", "total_filled_orders"])
            if isinstance(val, (int, float)):
                filled_orders = int(val)
            elif isinstance(meta.get("open_paper_trades"), list):
                filled_orders = sum(
                    int(row.get("fills") or 0)
                    for row in meta["open_paper_trades"]
                    if isinstance(row, dict)
                )
        if last_debug_event is None and isinstance(meta.get("last_debug_event"), dict):
            last_debug_event = meta.get("last_debug_event")
        last_equity_ts = last_equity_ts or _to_iso_from_any(_meta_lookup(meta, ["last_equity_ts", "equity_ts"]))
    return {
        "exchange": exchange,
        "timeframe": timeframe,
        "status": status,
        "started_at": started_at,
        "updated_at": updated_at,
        "open_legs": open_legs,
        "filled_orders": filled_orders,
        "last_debug_event": last_debug_event,
        "last_equity_ts": last_equity_ts,
    }


def _infer_live_status(raw_status: Any, session_dir: str, updated_at: Optional[str], last_debug_event: Any) -> str:
    normalized = str(raw_status or "").strip().lower()
    if normalized == "error":
        return normalized
    control = _safe_read_json(os.path.join(session_dir, "RUN_STATUS.json"))
    if isinstance(control, dict):
        c = control.get("control")
        if isinstance(c, dict) and c.get("kill") is True:
            return "stopped"
    if isinstance(last_debug_event, dict) and str(last_debug_event.get("level", "")).lower() in {"error", "fatal", "critical"}:
        return "error"
    for marker in ("KILL", "STOP"):
        if os.path.isfile(os.path.join(session_dir, marker)):
            return "stopped"
    for marker in ("error.txt", "last_error.json", "fatal.log"):
        if os.path.isfile(os.path.join(session_dir, marker)):
            return "error"
    if normalized in {"running", "stopped"}:
        return normalized
    running_markers = ("runner.pid", "running", "alive")
    if any(os.path.exists(os.path.join(session_dir, m)) for m in running_markers):
        return "running"
    if updated_at:
        try:
            delta = time.time() - pd.to_datetime(updated_at, utc=True).timestamp()
            if 0 <= delta <= 15 * 60:
                return "running"
        except Exception:
            pass
        return "stopped"
    if normalized == "unknown":
        return normalized
    return "unknown"


def _read_table_rows_from_file(path: str, limit: int = 200) -> List[Dict[str, Any]]:
    ext = os.path.splitext(path)[1].lower()
    try:
        if ext == ".csv":
            df = pd.read_csv(path).tail(limit)
            return df.replace({np.nan: None}).to_dict(orient="records")
        if ext in {".json", ".js"}:
            data = _safe_read_json(path)
            if isinstance(data, list):
                return [r if isinstance(r, dict) else {"value": r} for r in data[-limit:]]
            if isinstance(data, dict):
                rows = data.get("rows")
                if isinstance(rows, list):
                    return [r if isinstance(r, dict) else {"value": r} for r in rows[-limit:]]
                return [data]
        if ext in {".jsonl", ".ndjson", ".log", ".txt"}:
            lines = []
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()[-limit:]
            out: List[Dict[str, Any]] = []
            for idx, line in enumerate(lines):
                line = line.rstrip("\n")
                parsed = None
                if ext in {".jsonl", ".ndjson"}:
                    try:
                        parsed = json.loads(line)
                    except Exception:
                        parsed = None
                if isinstance(parsed, dict):
                    out.append(parsed)
                else:
                    out.append({"line_no": idx + 1, "line": line})
            return out
    except Exception:
        return []
    return []


def _sqlite_rows_for_live_table(session_dir: str, kind: str, limit: int = 200) -> List[Dict[str, Any]]:
    db_path = os.path.join(session_dir, "session.sqlite")
    if not os.path.isfile(db_path):
        return []
    if kind == "open_positions":
        tables = ("open_positions", "positions")
    elif kind == "orders":
        tables = ("orders", "filled_orders")
    else:
        return []
    try:
        con = sqlite3.connect(db_path)
        try:
            existing = set(_sqlite_table_names(db_path))
            for table in tables:
                if table not in existing:
                    continue
                cols = [r[1] for r in con.execute(f'PRAGMA table_info("{table}")').fetchall()]
                if not cols:
                    continue
                where = ""
                if kind == "open_positions" and "status" in cols:
                    where = "WHERE status IS NULL OR UPPER(status) NOT IN ('CLOSED', 'EXITED')"
                order_col = next((c for c in ("ts_utc", "updated_at", "created_at", "entry_fill_ts", "exit_fill_ts", "ts", "timestamp") if c in cols), None)
                sql = f'SELECT * FROM "{table}" {where}'
                if order_col:
                    sql += f' ORDER BY "{order_col}" DESC'
                sql += f" LIMIT {int(limit)}"
                df = pd.read_sql(sql, con)
                if not df.empty:
                    rows = df.replace({np.nan: None}).to_dict(orient="records")
                    if order_col:
                        rows.reverse()
                    return rows
        finally:
            con.close()
    except Exception:
        return []
    return []


def _live_session_sqlite_path(session_dir: str) -> Optional[str]:
    candidates = [os.path.join(session_dir, "session.sqlite")]
    marker = os.path.join(session_dir, "ACTIVE_SESSION_DB_PATH.txt")
    if os.path.isfile(marker):
        try:
            raw = open(marker, "r", encoding="utf-8", errors="replace").read().strip()
        except Exception:
            raw = ""
        if raw:
            path = raw if os.path.isabs(raw) else os.path.abspath(os.path.join(REPO_ROOT, raw))
            candidates.insert(0, path)
    for path in candidates:
        path = os.path.abspath(path)
        if _is_within(path, session_dir) and os.path.isfile(path):
            return path
    return None


def _sqlite_live_order_rows(session_dir: str, symbol: Optional[str] = None, limit: int = 10000) -> List[Dict[str, Any]]:
    db_path = _live_session_sqlite_path(session_dir)
    if not db_path:
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        try:
            existing = set(_sqlite_table_names(db_path))
            if "orders" not in existing:
                return []
            cols = [r[1] for r in con.execute('PRAGMA table_info("orders")').fetchall()]
            if not cols:
                return []
            where = []
            params: List[Any] = []
            if "status" in cols:
                where.append("UPPER(status) = 'FILLED'")
            if symbol and "symbol" in cols:
                where.append("REPLACE(UPPER(symbol), '-', '') = ?")
                params.append(str(symbol).replace("-", "").upper())
            sql = 'SELECT * FROM "orders"'
            if where:
                sql += " WHERE " + " AND ".join(where)
            order_col = next((c for c in ("ts_utc", "bar_time_utc", "created_at", "ts", "timestamp") if c in cols), None)
            if order_col:
                sql += f' ORDER BY "{order_col}" ASC'
            sql += f" LIMIT {int(limit)}"
            return [dict(row) for row in con.execute(sql, params).fetchall()]
        finally:
            con.close()
    except Exception:
        return []


def _live_order_direction(row: Dict[str, Any]) -> str:
    order_type = str(row.get("type") or "").upper()
    side = str(row.get("side") or "").upper()
    side_label = "Long" if side == "LONG" else "Short" if side == "SHORT" else side.title()
    action = "Закрити" if order_type in {"CLOSE", "EXIT"} else "Відкрити"
    return f"{action} {side_label}".strip()


def _write_live_orders_match_csv_from_sqlite(session_dir: str, dst: str, symbol: Optional[str]) -> Optional[str]:
    rows = _sqlite_live_order_rows(session_dir, symbol=symbol)
    if not rows:
        return None
    out: List[Dict[str, Any]] = []
    for row in rows:
        ts = _to_iso_from_any(row.get("ts_utc") or row.get("bar_time_utc") or row.get("timestamp"))
        if not ts:
            continue
        sym = str(row.get("symbol") or symbol or "").replace("-", "")
        qty = _safe_float(row.get("qty"))
        price = _safe_float(row.get("price"))
        out.append(
            {
                "Час виконання": ts,
                "Ф’ючерси / Напрямок": f"{sym}\n{_live_order_direction(row)}",
                "Виконано": f"{qty:g}",
                "Ціна виконання": f"{price:g}",
                "Закриті PnL / %": "0 USDT",
                "Комісія": "0 USDT",
                "Ордер №": str(row.get("order_id") or ""),
                "Операція": "",
            }
        )
    if not out:
        return None
    pd.DataFrame(out).to_csv(dst, index=False, encoding="utf-8-sig")
    return dst


def _sqlite_live_order_series(session_dir: str, symbol: Optional[str] = None) -> List[Dict[str, Any]]:
    rows = _sqlite_live_order_rows(session_dir, symbol=symbol, limit=10000)
    out: List[Dict[str, Any]] = []
    cumulative = 0.0
    for row in rows:
        ts = _to_iso_from_any(row.get("ts_utc") or row.get("bar_time_utc") or row.get("timestamp"))
        if not ts:
            continue
        order_type = str(row.get("type") or "").upper()
        qty = _safe_float(row.get("qty"))
        price = _safe_float(row.get("price"))
        signed_notional = qty * price
        if order_type in {"CLOSE", "EXIT"}:
            signed_notional *= -1.0
        cumulative += signed_notional
        out.append({"ts": ts, "value": float(cumulative)})
    return out


def _run_status_open_position_rows(session_dir: str, limit: int = 200) -> List[Dict[str, Any]]:
    status = _safe_read_json(os.path.join(session_dir, "RUN_STATUS.json"))
    if not isinstance(status, dict):
        return []
    trades = status.get("open_paper_trades")
    if not isinstance(trades, list):
        return []
    out: List[Dict[str, Any]] = []
    for trade in trades[-limit:]:
        if not isinstance(trade, dict):
            continue
        row = dict(trade)
        avg_entry = row.get("avg_entry")
        notional = row.get("notional")
        if row.get("qty") is None and avg_entry:
            try:
                row["qty"] = float(notional or 0.0) / float(avg_entry)
            except Exception:
                pass
        row.setdefault("status", "OPEN")
        row.setdefault("exchange", status.get("live_exchange"))
        row.setdefault("live_symbol", status.get("live_symbol"))
        row.setdefault("updated_at", status.get("utc"))
        row.setdefault("source", "RUN_STATUS.json")
        out.append(row)
    return out


def _session_table_rows(session_dir: str, kind: str, limit: int = 200) -> List[Dict[str, Any]]:
    if kind == "open_positions":
        candidates = ["open_positions.csv", "positions_open.csv", "positions.csv", "open_positions.json"]
    elif kind == "orders":
        candidates = ["orders.csv", "filled_orders.csv", "orders.json", "orders.jsonl"]
    elif kind == "debug_events":
        candidates = ["debug_events.jsonl", "debug_events.csv", "debug_events.json", "events_debug.jsonl", "events.jsonl"]
    elif kind == "stdio":
        candidates = ["stdio.log", "stdout.log", "stderr.log", "stdout.txt", "stderr.txt"]
        candidates.extend(os.path.basename(p) for p in sorted(glob.glob(os.path.join(session_dir, "live_stdout_*.log"))))
    else:
        return []
    if kind == "open_positions":
        run_status_rows = _run_status_open_position_rows(session_dir, limit=limit)
        if run_status_rows:
            return run_status_rows
    for rel in candidates:
        full = os.path.join(session_dir, rel)
        if os.path.isfile(full):
            rows = _read_table_rows_from_file(full, limit=limit)
            if rows:
                return rows
    sqlite_rows = _sqlite_rows_for_live_table(session_dir, kind, limit=limit)
    if sqlite_rows:
        return sqlite_rows
    return []


def _coerce_point(ts: Any, value: Any) -> Optional[Dict[str, Any]]:
    iso = _to_iso_from_any(ts)
    if not iso:
        return None
    try:
        v = float(value)
    except Exception:
        return None
    if np.isnan(v) or np.isinf(v):
        return None
    return {"ts": iso, "value": float(v)}


def _series_from_df(df: pd.DataFrame) -> List[Dict[str, Any]]:
    if df.empty:
        return []
    ts_col = next((c for c in ("ts", "timestamp", "time", "dt", "datetime") if c in df.columns), None)
    val_col = next((c for c in ("value", "equity", "pnl", "cum_pnl", "distance", "abs_distance") if c in df.columns), None)
    if not ts_col or not val_col:
        return []
    out: List[Dict[str, Any]] = []
    for _, row in df.iterrows():
        p = _coerce_point(row.get(ts_col), row.get(val_col))
        if p:
            out.append(p)
    out.sort(key=lambda x: x["ts"])
    return out


def _load_series_file(session_dir: str, candidates: List[str]) -> List[Dict[str, Any]]:
    series, _source = _load_series_file_with_source(session_dir, candidates)
    return series


def _load_series_file_with_source(session_dir: str, candidates: List[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    for rel in candidates:
        full = os.path.join(session_dir, rel)
        if not os.path.isfile(full):
            continue
        ext = os.path.splitext(full)[1].lower()
        try:
            out: List[Dict[str, Any]] = []
            if ext == ".csv":
                out = _series_from_df(pd.read_csv(full))
            elif ext in {".json", ".js"}:
                data = _safe_read_json(full)
                rows = data.get("rows") if isinstance(data, dict) else data
                if isinstance(rows, list):
                    for row in rows:
                        if not isinstance(row, dict):
                            continue
                        p = _coerce_point(row.get("ts") or row.get("timestamp"), row.get("value") or row.get("equity") or row.get("pnl"))
                        if p:
                            out.append(p)
                    out.sort(key=lambda x: x["ts"])
            if out:
                return out, rel
        except Exception:
            continue
    return [], None


def _last_csv_ts(session_dir: str, candidates: List[str]) -> Optional[str]:
    for rel in candidates:
        full = os.path.join(session_dir, rel)
        if not os.path.isfile(full):
            continue
        try:
            with open(full, "rb") as fh:
                fh.seek(0, os.SEEK_END)
                pos = fh.tell()
                chunk = b""
                while pos > 0 and chunk.count(b"\n") < 2:
                    step = min(4096, pos)
                    pos -= step
                    fh.seek(pos)
                    chunk = fh.read(step) + chunk
                lines = [line for line in chunk.decode("utf-8", errors="replace").splitlines() if line.strip()]
            if len(lines) < 2:
                continue
            ts = lines[-1].split(",", 1)[0].strip()
            return _to_iso_from_any(ts) or ts
        except Exception:
            continue
    return None


def _load_series_column_file_with_source(session_dir: str, candidates: List[str], value_columns: List[str]) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    for rel in candidates:
        full = os.path.join(session_dir, rel)
        if not os.path.isfile(full):
            continue
        try:
            df = pd.read_csv(full)
        except Exception:
            continue
        if df.empty:
            continue
        time_col = next((c for c in ("ts", "timestamp", "time", "dt", "Date and time") if c in df.columns), None)
        value_col = next((c for c in value_columns if c in df.columns), None)
        if not time_col or not value_col:
            continue
        out: List[Dict[str, Any]] = []
        for _, row in df.iterrows():
            point = _coerce_point(row.get(time_col), row.get(value_col))
            if point:
                out.append(point)
        if out:
            return sorted(out, key=lambda x: x["ts"]), rel
    return [], None


def _live_realized_pnl_series(session_dir: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    full = os.path.join(session_dir, "live_chart_events.csv")
    if not os.path.isfile(full):
        return [], None
    try:
        df = pd.read_csv(full)
    except Exception:
        return [], None
    if df.empty or "ts" not in df.columns or "pnl" not in df.columns:
        return [], None
    out: List[Dict[str, Any]] = []
    realized = 0.0
    for _, row in df.sort_values("ts").iterrows():
        ts = _to_iso_from_any(row.get("ts"))
        pnl = _safe_float(row.get("pnl"))
        if not ts or pnl == 0.0:
            continue
        realized += pnl
        out.append({"ts": ts, "value": float(realized)})
    return out, "live_chart_events.csv" if out else None


def _sqlite_live_equity_series(session_dir: str) -> List[Dict[str, Any]]:
    db_path = _live_session_sqlite_path(session_dir)
    if not db_path:
        return []
    try:
        df = _session_equity_df(db_path)
    except Exception:
        df = None
    if df is None or getattr(df, "empty", True):
        return []
    if "ts" not in df.columns or "equity" not in df.columns:
        return []
    out = df.copy()
    out["ts"] = pd.to_datetime(out["ts"], errors="coerce", utc=True)
    out["equity"] = pd.to_numeric(out["equity"], errors="coerce")
    out = out.dropna(subset=["ts", "equity"]).sort_values("ts")
    if out.empty:
        return []
    try:
        base = float(out.attrs.get("initial_equity"))
    except Exception:
        base = float(out["equity"].iloc[0])
    return [{"ts": row["ts"].isoformat(), "value": float(row["equity"] - base)} for _, row in out.iterrows()]


def _sqlite_live_equity_snapshot_series(session_dir: str) -> List[Dict[str, Any]]:
    db_path = _live_session_sqlite_path(session_dir)
    if not db_path:
        return []
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            existing = set(_sqlite_table_names(db_path))
            if "equity" not in existing:
                return []
            cols = [r[1] for r in con.execute('PRAGMA table_info("equity")').fetchall()]
            time_col = next((c for c in ("ts_utc", "ts", "timestamp", "time") if c in cols), None)
            value_col = next((c for c in ("equity_usdt", "equity", "value") if c in cols), None)
            if not time_col or not value_col:
                return []
            base_row = con.execute(
                f'SELECT "{value_col}" FROM "equity" WHERE "{time_col}" IS NOT NULL AND "{value_col}" IS NOT NULL ORDER BY "{time_col}" LIMIT 1'
            ).fetchone()
            try:
                rows = con.execute(
                    f'''
                    SELECT e."{time_col}" AS ts, e."{value_col}" AS equity
                    FROM "equity" e
                    JOIN (
                        SELECT substr("{time_col}", 1, 16) AS bucket, max(rowid) AS rid
                        FROM "equity"
                        WHERE "{time_col}" IS NOT NULL AND "{value_col}" IS NOT NULL
                        GROUP BY bucket
                    ) m ON e.rowid = m.rid
                    ORDER BY e."{time_col}"
                    '''
                ).fetchall()
            except Exception:
                rows = con.execute(
                    f'SELECT "{time_col}" AS ts, "{value_col}" AS equity FROM "equity" ORDER BY "{time_col}"'
                ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    if not rows:
        return []
    df = pd.DataFrame(rows, columns=["ts", "equity"])
    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
    df["equity"] = pd.to_numeric(df["equity"], errors="coerce")
    df = df.dropna(subset=["ts", "equity"]).sort_values("ts")
    if df.empty:
        return []
    try:
        base = float(base_row[0]) if base_row else float(df["equity"].iloc[0])
    except Exception:
        base = float(df["equity"].iloc[0])
    return [{"ts": row["ts"].isoformat(), "value": float(row["equity"] - base)} for _, row in df.iterrows()]


def _tv_backtest_price_series(tv_path: Optional[str]) -> List[Dict[str, Any]]:
    if not tv_path:
        return []
    abs_path = os.path.abspath(str(tv_path))
    if not _is_allowed_validation_path(abs_path) or not os.path.isfile(abs_path):
        return []
    try:
        df = _read_tv_csv(abs_path)
    except Exception:
        return []
    price_col = next((c for c in ("Price USDT", "price", "Price", "close", "Close") if c in df.columns), None)
    if not price_col:
        return []
    out = df.copy()
    out[price_col] = pd.to_numeric(out[price_col], errors="coerce")
    out = out.dropna(subset=["dt", price_col]).sort_values("dt")
    if out.empty:
        return []
    points: List[Dict[str, Any]] = []
    for _, row in out.iterrows():
        points.append({"ts": row["dt"].isoformat(), "value": float(row[price_col])})
    return points


def _active_live_telemetry_path(session_dir: str) -> Optional[str]:
    pointer = os.path.join(session_dir, "ACTIVE_TELEMETRY_PATH.txt")
    try:
        raw = Path(pointer).read_text(encoding="utf-8").strip()
    except Exception:
        return None
    if not raw:
        return None
    path = raw if os.path.isabs(raw) else os.path.join(session_dir, raw)
    try:
        session_root = os.path.abspath(session_dir)
        candidate = os.path.abspath(path)
        if os.path.commonpath([session_root, candidate]) != session_root:
            return None
    except Exception:
        return None
    if os.path.isfile(path) and path.lower().endswith(".jsonl"):
        return path
    return None


def _latest_live_telemetry_path(session_dir: str) -> Optional[str]:
    active = _active_live_telemetry_path(session_dir)

    def score(path: str) -> Tuple[int, int]:
        try:
            mtime = int(os.path.getmtime(path))
        except Exception:
            mtime = 0
        basename = os.path.basename(path)
        priority = 2 if active and os.path.abspath(path) == os.path.abspath(active) else 1 if basename == "telemetry.jsonl" else 0
        return mtime, priority

    candidates = sorted(
        _live_telemetry_paths(session_dir),
        key=score,
        reverse=True,
    )
    return candidates[0] if candidates else None


_LIVE_TELEMETRY_MARK_CACHE: Dict[str, Tuple[Tuple[Tuple[str, int, int], ...], List[Dict[str, Any]]]] = {}
_LIVE_TELEMETRY_OHLCV_CACHE: Dict[str, Tuple[Tuple[Tuple[str, int, int], ...], List[Dict[str, Any]]]] = {}
MAX_TELEMETRY_BYTES_FOR_CHART = 80 * 1024 * 1024
_TELEMETRY_MARK_RE = re.compile(r'"mark"\s*:\s*(-?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)')
_TELEMETRY_UTC_RE = re.compile(r'"utc"\s*:\s*"([^"]+)"')


def _live_telemetry_paths(session_dir: str) -> List[str]:
    candidates = list(glob.glob(os.path.join(session_dir, "run_telemetry_*.jsonl")))
    compact = os.path.join(session_dir, "telemetry.jsonl")
    if os.path.isfile(compact):
        candidates.append(compact)
    active = _active_live_telemetry_path(session_dir)
    if active:
        candidates.append(active)
    deduped = {os.path.abspath(path): path for path in candidates}
    return sorted(
        deduped.values(),
        key=lambda p: (os.path.basename(p), os.path.getmtime(p) if os.path.exists(p) else 0),
    )


def _live_telemetry_signature(paths: List[str]) -> Tuple[Tuple[str, int, int], ...]:
    signature = []
    for path in paths:
        try:
            st = os.stat(path)
            signature.append((path, int(st.st_size), int(st.st_mtime_ns)))
        except Exception:
            continue
    return tuple(signature)


def _live_telemetry_total_bytes(paths: List[str]) -> int:
    total = 0
    for path in paths:
        try:
            total += int(os.path.getsize(path))
        except Exception:
            continue
    return total


def _extract_mark_point_from_telemetry_line(line: str) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        row = json.loads(line)
    except Exception:
        return None
    if not isinstance(row, dict):
        return None
    status = row.get("status") if isinstance(row.get("status"), dict) else {}
    input_meta = row.get("input_meta") if isinstance(row.get("input_meta"), dict) else {}
    if not input_meta and isinstance(status.get("input_meta"), dict):
        input_meta = status.get("input_meta") or {}
    market = input_meta.get("market") if isinstance(input_meta.get("market"), dict) else {}
    mark = market.get("mark")
    ts = row.get("ts") or row.get("utc") or row.get("timestamp")
    if ts is None:
        ts = status.get("utc") or status.get("timestamp")
    return _coerce_point(ts, mark)


def _extract_mark_point_from_telemetry_line_fast(line: str) -> Optional[Dict[str, Any]]:
    if '"mark"' not in line:
        return None
    mark_match = _TELEMETRY_MARK_RE.search(line)
    if not mark_match:
        return _extract_mark_point_from_telemetry_line(line)
    utc_matches = list(_TELEMETRY_UTC_RE.finditer(line))
    if not utc_matches:
        return _extract_mark_point_from_telemetry_line(line)
    return _coerce_point(utc_matches[-1].group(1), mark_match.group(1))


def _iso_to_epoch_seconds_fast(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    try:
        raw = value.replace("Z", "+00:00")
        return datetime.fromisoformat(raw).timestamp()
    except Exception:
        try:
            ts = pd.to_datetime(value, utc=True, errors="coerce")
            if pd.isna(ts):
                return None
            return float(ts.timestamp())
        except Exception:
            return None


def _extract_mark_bucket_from_telemetry_line_fast(line: str) -> Optional[Tuple[int, float]]:
    if '"mark"' not in line:
        return None
    mark_match = _TELEMETRY_MARK_RE.search(line)
    if not mark_match:
        point = _extract_mark_point_from_telemetry_line(line)
        if not point:
            return None
        ts_seconds = _iso_to_epoch_seconds_fast(point.get("ts"))
        value = _safe_float(point.get("value"))
    else:
        utc_matches = list(_TELEMETRY_UTC_RE.finditer(line))
        if not utc_matches:
            point = _extract_mark_point_from_telemetry_line(line)
            if not point:
                return None
            ts_seconds = _iso_to_epoch_seconds_fast(point.get("ts"))
            value = _safe_float(point.get("value"))
        else:
            ts_seconds = _iso_to_epoch_seconds_fast(utc_matches[-1].group(1))
            value = _safe_float(mark_match.group(1))
    if ts_seconds is None or value is None or not np.isfinite(value):
        return None
    return int(ts_seconds // 60) * 60, float(value)


def _live_mark_points_from_telemetry_raw(session_dir: str) -> List[Dict[str, Any]]:
    paths = _live_telemetry_paths(session_dir)
    if not paths:
        return []
    if _live_telemetry_total_bytes(paths) > MAX_TELEMETRY_BYTES_FOR_CHART:
        latest = _latest_live_telemetry_path(session_dir)
        paths = [latest] if latest else []
    if not paths:
        return []
    signature = _live_telemetry_signature(paths)
    cached = _LIVE_TELEMETRY_MARK_CACHE.get(session_dir)
    if cached and cached[0] == signature:
        return list(cached[1])

    dedup: Dict[str, Dict[str, Any]] = {}
    for path in paths:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except Exception:
            continue
        with fh:
            for line in fh:
                point = _extract_mark_point_from_telemetry_line_fast(line)
                if point:
                    dedup[point["ts"]] = point
    points = sorted(dedup.values(), key=lambda x: x["ts"])
    _LIVE_TELEMETRY_MARK_CACHE[session_dir] = (signature, points)
    return list(points)


def _mark_points_to_minute_bars(points: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    buckets: Dict[int, Dict[str, Any]] = {}
    for point in points:
        try:
            ts = pd.to_datetime(point.get("ts"), utc=True, errors="coerce")
            value = float(point.get("value"))
        except Exception:
            continue
        if pd.isna(ts) or not np.isfinite(value):
            continue
        bucket = int(ts.timestamp() // 60) * 60
        existing = buckets.get(bucket)
        if not existing:
            buckets[bucket] = {"open": value, "high": value, "low": value, "close": value}
            continue
        existing["high"] = max(float(existing["high"]), value)
        existing["low"] = min(float(existing["low"]), value)
        existing["close"] = value
    out: List[Dict[str, Any]] = []
    for bucket in sorted(buckets):
        row = buckets[bucket]
        out.append(
            {
                "ts": pd.to_datetime(bucket, unit="s", utc=True).isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )
    return out


def _live_telemetry_ohlcv_bars(session_dir: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    paths = _live_telemetry_paths(session_dir)
    if not paths:
        return [], None
    source = "live telemetry jsonl:mark_ohlc_1m"
    if _live_telemetry_total_bytes(paths) > MAX_TELEMETRY_BYTES_FOR_CHART:
        latest = _latest_live_telemetry_path(session_dir)
        paths = [latest] if latest else []
        source = "latest live telemetry jsonl:mark_ohlc_1m"
    if not paths:
        return [], None
    signature = _live_telemetry_signature(paths)
    cached = _LIVE_TELEMETRY_OHLCV_CACHE.get(session_dir)
    if cached and cached[0] == signature:
        return list(cached[1]), source

    buckets: Dict[int, Dict[str, Any]] = {}
    for path in paths:
        try:
            fh = open(path, "r", encoding="utf-8", errors="replace")
        except Exception:
            continue
        with fh:
            for line in fh:
                bucket_point = _extract_mark_bucket_from_telemetry_line_fast(line)
                if not bucket_point:
                    continue
                bucket, value = bucket_point
                existing = buckets.get(bucket)
                if not existing:
                    buckets[bucket] = {"open": value, "high": value, "low": value, "close": value}
                    continue
                existing["high"] = max(float(existing["high"]), value)
                existing["low"] = min(float(existing["low"]), value)
                existing["close"] = value

    rows: List[Dict[str, Any]] = []
    for bucket in sorted(buckets):
        row = buckets[bucket]
        rows.append(
            {
                "ts": pd.to_datetime(bucket, unit="s", utc=True).isoformat(),
                "open": float(row["open"]),
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": float(row["close"]),
            }
        )
    _LIVE_TELEMETRY_OHLCV_CACHE[session_dir] = (signature, rows)
    return list(rows), source if rows else None


def _live_mark_points_from_telemetry(session_dir: str) -> List[Dict[str, Any]]:
    bars, _source = _live_telemetry_ohlcv_bars(session_dir)
    if not bars:
        bars = _mark_points_to_minute_bars(_live_mark_points_from_telemetry_raw(session_dir))
    return [{"ts": row["ts"], "value": float(row["close"])} for row in bars]


def _series_start_ts(series: List[Dict[str, Any]]) -> Optional[pd.Timestamp]:
    if not series:
        return None
    try:
        ts = pd.to_datetime(series[0].get("ts"), utc=True, errors="coerce")
        return ts if pd.notna(ts) else None
    except Exception:
        return None


def _minute_close_series(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not series:
        return []
    try:
        df = pd.DataFrame(series)
    except Exception:
        return []
    if df.empty or "ts" not in df.columns or "value" not in df.columns:
        return []
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna(subset=["ts", "value"]).sort_values("ts")
    if df.empty:
        return []
    df["bucket"] = df["ts"].dt.floor("min")
    out = df.groupby("bucket", sort=True)["value"].last().reset_index()
    return [{"ts": row["bucket"].isoformat(), "value": float(row["value"])} for _, row in out.iterrows()]


def _prefer_broader_live_series(
    primary: List[Dict[str, Any]],
    primary_source: Optional[str],
    fallback: List[Dict[str, Any]],
    fallback_source: Optional[str],
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not fallback:
        return primary, primary_source
    if not primary:
        return fallback, fallback_source
    primary_start = _series_start_ts(primary)
    fallback_start = _series_start_ts(fallback)
    if fallback_start is not None and (primary_start is None or fallback_start < primary_start):
        return fallback, fallback_source
    return primary, primary_source


def _live_ohlcv_bars_from_npz(session_dir: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    candidates = sorted(
        glob.glob(os.path.join(session_dir, "*ohlcv*.npz")),
        key=lambda p: os.path.basename(p),
    )
    by_time: Dict[str, Dict[str, Any]] = {}
    sources: List[str] = []
    for path in candidates:
        try:
            data = np.load(path, allow_pickle=True)
            timestamps = data["timestamp_s"]
            opens = data["open"]
            highs = data["high"]
            lows = data["low"]
            closes = data["close"]
        except Exception:
            continue
        added = 0
        for idx in range(len(timestamps)):
            try:
                ts = pd.to_datetime(int(timestamps[idx]), unit="s", utc=True).isoformat()
                row = {
                    "ts": ts,
                    "open": float(opens[idx]),
                    "high": float(highs[idx]),
                    "low": float(lows[idx]),
                    "close": float(closes[idx]),
                }
            except Exception:
                continue
            if all(np.isfinite(row[k]) for k in ("open", "high", "low", "close")):
                by_time[ts] = row
                added += 1
        if added:
            sources.append(os.path.basename(path))

    rows = sorted(by_time.values(), key=lambda x: x["ts"])
    return rows, "+".join(sources) if rows and sources else None


def _live_ohlcv_bars_from_csv(session_dir: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    path = os.path.join(session_dir, "live_candles.csv")
    if not os.path.exists(path):
        return [], None
    try:
        df = pd.read_csv(path)
    except Exception:
        return [], None
    if df.empty or "ts" not in df.columns:
        return [], None
    value_cols = [col for col in ("open", "high", "low", "close") if col in df.columns]
    if not value_cols:
        return [], None
    df["ts"] = pd.to_datetime(df["ts"], utc=True, errors="coerce")
    for col in value_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df = df.dropna(subset=["ts", *value_cols]).sort_values("ts")
    if df.empty:
        return [], None
    df["bucket"] = df["ts"].dt.floor("min")
    rows: List[Dict[str, Any]] = []
    for bucket, group in df.groupby("bucket", sort=True):
        opens = group["open"] if "open" in group.columns else group[value_cols[0]]
        highs = group["high"] if "high" in group.columns else group[value_cols[0]]
        lows = group["low"] if "low" in group.columns else group[value_cols[0]]
        closes = group["close"] if "close" in group.columns else group[value_cols[0]]
        row = {
            "ts": bucket.isoformat(),
            "open": float(opens.iloc[0]),
            "high": float(highs.max()),
            "low": float(lows.min()),
            "close": float(closes.iloc[-1]),
        }
        if all(np.isfinite(row[col]) for col in ("open", "high", "low", "close")):
            rows.append(row)
    return rows, "live_candles.csv" if rows else None


def _has_price_bar_gap(rows: List[Dict[str, Any]], max_gap_seconds: int = 120) -> bool:
    if len(rows) < 2:
        return False
    prev: Optional[pd.Timestamp] = None
    for row in rows:
        ts = pd.to_datetime(row.get("ts"), utc=True, errors="coerce")
        if pd.isna(ts):
            continue
        if prev is not None and (ts - prev).total_seconds() > max_gap_seconds:
            return True
        prev = ts
    return False


def _merge_price_bars(primary: List[Dict[str, Any]], fallback: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_time: Dict[str, Dict[str, Any]] = {}
    for row in fallback:
        ts = str(row.get("ts") or "")
        if ts:
            by_time[ts] = row
    for row in primary:
        ts = str(row.get("ts") or "")
        if ts:
            by_time[ts] = row
    return sorted(by_time.values(), key=lambda x: x["ts"])


def _fill_short_price_bar_gaps(rows: List[Dict[str, Any]], max_gap_seconds: int = 600) -> Tuple[List[Dict[str, Any]], int]:
    if len(rows) < 2:
        return rows, 0
    filled: List[Dict[str, Any]] = []
    inserted = 0
    for row in rows:
        if not filled:
            filled.append(row)
            continue
        prev = filled[-1]
        prev_seconds = _iso_to_epoch_seconds_fast(prev.get("ts"))
        row_seconds = _iso_to_epoch_seconds_fast(row.get("ts"))
        if prev_seconds is not None and row_seconds is not None:
            gap = row_seconds - prev_seconds
            if 120 < gap <= max_gap_seconds:
                close = _safe_float(prev.get("close"))
                if close is not None:
                    next_ts = int(prev_seconds // 60) * 60 + 60
                    while next_ts < int(row_seconds):
                        filled.append(
                            {
                                "ts": pd.to_datetime(next_ts, unit="s", utc=True).isoformat(),
                                "open": close,
                                "high": close,
                                "low": close,
                                "close": close,
                            }
                        )
                        inserted += 1
                        next_ts += 60
        filled.append(row)
    return filled, inserted


def _chart_point_budget(raw: Any, default: int = 1800) -> int:
    try:
        value = int(raw)
    except Exception:
        value = default
    return max(300, min(5000, value))


def _downsample_price_bars(rows: List[Dict[str, Any]], max_points: int) -> Tuple[List[Dict[str, Any]], int]:
    if len(rows) <= max_points:
        return rows, 0
    step = int(np.ceil(len(rows) / max_points))
    out: List[Dict[str, Any]] = []
    for idx in range(0, len(rows), step):
        chunk = rows[idx : idx + step]
        if not chunk:
            continue
        try:
            opens = [_safe_float(row.get("open")) for row in chunk]
            highs = [_safe_float(row.get("high")) for row in chunk]
            lows = [_safe_float(row.get("low")) for row in chunk]
            closes = [_safe_float(row.get("close")) for row in chunk]
            highs_f = [v for v in highs if v is not None]
            lows_f = [v for v in lows if v is not None]
            open_v = next((v for v in opens if v is not None), None)
            close_v = next((v for v in reversed(closes) if v is not None), None)
        except Exception:
            continue
        if open_v is None or close_v is None or not highs_f or not lows_f:
            continue
        out.append(
            {
                "ts": chunk[0].get("ts"),
                "open": float(open_v),
                "high": float(max(highs_f)),
                "low": float(min(lows_f)),
                "close": float(close_v),
            }
        )
    return out, len(rows) - len(out)


def _point_xy(point: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    ts_seconds = _iso_to_epoch_seconds_fast(point.get("ts"))
    value = _safe_float(point.get("value"))
    if ts_seconds is None or value is None or not np.isfinite(value):
        return None
    return ts_seconds, float(value)


def _downsample_line_series_lttb(series: List[Dict[str, Any]], max_points: int) -> Tuple[List[Dict[str, Any]], int]:
    if len(series) <= max_points or max_points < 3:
        return series, 0
    points = [point for point in series if _point_xy(point) is not None]
    if len(points) <= max_points:
        return points, len(series) - len(points)
    threshold = max_points
    every = (len(points) - 2) / float(threshold - 2)
    sampled: List[Dict[str, Any]] = [points[0]]
    a_idx = 0
    for i in range(threshold - 2):
        avg_start = int(np.floor((i + 1) * every)) + 1
        avg_end = int(np.floor((i + 2) * every)) + 1
        avg_end = min(avg_end, len(points))
        avg_range = points[avg_start:avg_end] or [points[-1]]
        avg_xy = [_point_xy(point) for point in avg_range]
        avg_xy = [xy for xy in avg_xy if xy is not None]
        if not avg_xy:
            continue
        avg_x = sum(x for x, _ in avg_xy) / len(avg_xy)
        avg_y = sum(y for _, y in avg_xy) / len(avg_xy)

        range_start = int(np.floor(i * every)) + 1
        range_end = int(np.floor((i + 1) * every)) + 1
        range_end = min(range_end, len(points) - 1)
        a_xy = _point_xy(points[a_idx])
        if a_xy is None:
            continue
        ax, ay = a_xy
        best_idx = range_start
        best_area = -1.0
        for idx in range(range_start, max(range_start + 1, range_end)):
            current_xy = _point_xy(points[idx])
            if current_xy is None:
                continue
            cx, cy = current_xy
            area = abs((ax - avg_x) * (cy - ay) - (ax - cx) * (avg_y - ay))
            if area > best_area:
                best_area = area
                best_idx = idx
        sampled.append(points[best_idx])
        a_idx = best_idx
    sampled.append(points[-1])
    return sampled, len(series) - len(sampled)


def _downsample_live_chart_payload(payload: Dict[str, Any], max_points: int) -> None:
    stats: Dict[str, Dict[str, int]] = {}
    for key in ("live", "backtest", "live_realized", "backtest_realized", "backtest_price", "distance", "mark"):
        rows = payload.get(key)
        if isinstance(rows, list) and len(rows) > max_points:
            downsampled, dropped = _downsample_line_series_lttb(rows, max_points)
            payload[key] = downsampled
            if dropped:
                stats[key] = {"from": len(rows), "to": len(downsampled)}
    rows = payload.get("price_bars")
    if isinstance(rows, list) and len(rows) > max_points:
        downsampled, dropped = _downsample_price_bars(rows, max_points)
        payload["price_bars"] = downsampled
        if dropped:
            stats["price_bars"] = {"from": len(rows), "to": len(downsampled)}
    if stats:
        payload["downsampled"] = {"max_points": max_points, "series": stats}


def _label_from_live_order(row: Dict[str, Any]) -> Tuple[str, str, str]:
    order_type = str(row.get("type") or "").upper()
    reason = str(row.get("reason") or "")
    extra = row.get("extra")
    fill_type = ""
    if isinstance(extra, str) and extra.strip():
        try:
            extra = json.loads(extra)
        except Exception:
            extra = {}
    if isinstance(extra, dict):
        fill = extra.get("fill") if isinstance(extra.get("fill"), dict) else {}
        fill_type = str(fill.get("fill_type") or "")
    reason_l = reason.lower()
    fill_l = fill_type.lower()
    if order_type in {"CLOSE", "EXIT"}:
        return "Meta strategy full close", "meta_close", "#F87171"
    if "mark_crossed_dca_level" in reason_l or fill_l.startswith("dca_add"):
        return "DCA buy", "dca_buy", "#22C55E"
    if "lead_open_position_detected" in reason_l or fill_l == "base_entry":
        return "Meta strategy open", "meta_open", "#38BDF8"
    side = str(row.get("side") or "").upper()
    if order_type == "OPEN" and side in {"LONG", "BUY"}:
        return "DCA buy", "dca_buy", "#22C55E"
    if order_type == "OPEN":
        return "Meta strategy open", "meta_open", "#38BDF8"
    return "DCA sell", "dca_sell", "#F59E0B"


def _live_event_markers(session_dir: str) -> List[Dict[str, Any]]:
    rows = _sqlite_live_order_rows(session_dir, limit=10000)
    out: List[Dict[str, Any]] = []
    for idx, row in enumerate(rows):
        ts = _to_iso_from_any(row.get("ts_utc") or row.get("bar_time_utc") or row.get("timestamp"))
        price = _safe_float(row.get("price"))
        if not ts or price <= 0:
            continue
        text, kind, color = _label_from_live_order(row)
        is_close = kind in {"meta_close", "dca_sell"}
        out.append(
            {
                "id": str(row.get("order_id") or f"order-{idx}"),
                "time": ts,
                "price": float(price),
                "text": text,
                "kind": kind,
                "layer": "events",
                "color": color,
                "shape": "arrowDown" if is_close else "arrowUp",
                "position": "atPriceTop" if is_close else "atPriceBottom",
            }
        )
    closed_positions = _sqlite_rows_for_live_table(session_dir, "open_positions", limit=10000)
    for idx, row in enumerate(closed_positions):
        status = str(row.get("status") or "").upper()
        ts = _to_iso_from_any(row.get("exit_fill_ts") or row.get("close_ts") or row.get("closed_at"))
        price = _safe_float(row.get("exit_fill") or row.get("exit") or row.get("close_price"))
        if status != "CLOSED" or not ts or price <= 0:
            continue
        marker_id = f"position-close-{idx}"
        if any(m["time"] == ts and abs(float(m["price"]) - price) < 1e-9 for m in out):
            continue
        out.append(
            {
                "id": marker_id,
                "time": ts,
                "price": float(price),
                "text": "Meta strategy full close",
                "kind": "meta_close",
                "layer": "events",
                "color": "#F87171",
                "shape": "arrowDown",
                "position": "atPriceTop",
            }
        )
    return sorted(out, key=lambda x: x["time"])


def _live_strategy_labels(session_dir: str, anchor_time: Optional[str], anchor_price: Optional[float]) -> List[Dict[str, Any]]:
    status = _safe_read_json(os.path.join(session_dir, "RUN_STATUS.json"))
    params = status.get("candidate_params") if isinstance(status, dict) else None
    if not isinstance(params, dict) or not anchor_time or anchor_price is None:
        return []
    preferred = [
        "fresh_base_pct",
        "normal_base_pct",
        "fresh_tp_percent",
        "fresh_callback_percent",
        "max_position_cost_pct",
        "dca_profile",
    ]
    labels: List[Dict[str, Any]] = []
    for idx, key in enumerate(preferred):
        if key not in params:
            continue
        value = params.get(key)
        price = float(anchor_price) * (1.0 + (idx + 1) * 0.0015)
        labels.append(
            {
                "id": f"param-{key}",
                "time": anchor_time,
                "price": price,
                "text": f"{key}: {value}",
                "layer": "parameters",
                "color": "#22D3EE",
            }
        )
    return labels


def _live_target_price_lines(session_dir: str) -> List[Dict[str, Any]]:
    status = _safe_read_json(os.path.join(session_dir, "RUN_STATUS.json"))
    params = status.get("candidate_params") if isinstance(status, dict) else None
    tp_pct = _safe_float(params.get("fresh_tp_percent")) if isinstance(params, dict) else 0.0
    rows = _sqlite_live_order_rows(session_dir, limit=10000)
    if not rows:
        return []

    context: Optional[Dict[str, Any]] = None
    side = "LONG"
    for row in reversed(rows):
        extra = row.get("extra")
        if isinstance(extra, str) and extra.strip():
            try:
                extra = json.loads(extra)
            except Exception:
                extra = {}
        if not isinstance(extra, dict):
            continue
        closed = extra.get("closed")
        if isinstance(closed, dict):
            context = closed
            side = str(closed.get("side") or row.get("side") or side).upper()
            break

    if not isinstance(context, dict):
        return []

    direction = -1.0 if side == "SHORT" else 1.0
    lines: List[Dict[str, Any]] = []
    seen = set()

    def add_line(kind: str, price: float, text: str, color: str, width: int = 1) -> None:
        price = _safe_float(price)
        if price <= 0:
            return
        key = (kind, round(price, 8), text)
        if key in seen:
            return
        seen.add(key)
        lines.append(
            {
                "id": f"{kind}-{len(lines)}",
                "price": float(price),
                "text": text,
                "kind": kind,
                "layer": "targets",
                "color": color,
                "lineStyle": "dashed",
                "lineWidth": width,
            }
        )

    levels = context.get("levels")
    next_idx_raw = context.get("next_level_idx")
    if next_idx_raw is not None:
        next_idx = int(_safe_float(next_idx_raw))
        if isinstance(levels, list) and 0 <= next_idx < len(levels):
            add_line("next_dca_buy", levels[next_idx], "Next DCA buy", "#22C55E", 2)

    if tp_pct > 0:
        fills = context.get("fills")
        if isinstance(fills, list):
            for fill in fills:
                if not isinstance(fill, dict):
                    continue
                fill_type = str(fill.get("fill_type") or "")
                if not fill_type.startswith("dca_add"):
                    continue
                fill_price = _safe_float(fill.get("live_fill_price") or fill.get("expected_price"))
                if fill_price <= 0:
                    continue
                target = fill_price * (1.0 + direction * tp_pct / 100.0)
                suffix = fill_type.replace("dca_add_", "#")
                add_line("dca_sell_target", target, f"DCA sell TP {suffix}", "#F59E0B")

        avg_entry = _safe_float(context.get("avg_entry") or context.get("entry"))
        explicit_tp = _safe_float(context.get("tp_price") or context.get("take_profit_price"))
        full_tp = explicit_tp if explicit_tp > 0 else avg_entry * (1.0 + direction * tp_pct / 100.0) if avg_entry > 0 else 0.0
        add_line("full_sell_tp", full_tp, "Full sell TP", "#F87171", 2)

    return lines[:12]


def _live_snapshot_chart_payload(session_dir: str) -> Optional[Dict[str, Any]]:
    chart_path = os.path.join(session_dir, "chart.json")
    if not os.path.isfile(chart_path):
        return None
    data = _safe_read_json(chart_path)
    if not isinstance(data, dict):
        return None
    useful_keys = {
        "live",
        "backtest",
        "live_realized",
        "backtest_realized",
        "backtest_price",
        "price_bars",
        "distance",
        "mark",
        "markers",
        "labels",
        "price_lines",
    }
    if not any(isinstance(data.get(key), list) and data.get(key) for key in useful_keys):
        return None
    out = dict(data)
    sources = out.get("sources") if isinstance(out.get("sources"), dict) else {}
    sources = dict(sources)
    sources.setdefault("snapshot", "chart.json")
    out["sources"] = sources
    return out


def _resolve_live_session_path(raw_path: str) -> str:
    abs_path = os.path.abspath(raw_path or "")
    if not abs_path or not _is_within_live_results_root(abs_path):
        raise HTTPException(400, "path is outside allowed root")
    if not os.path.isdir(abs_path):
        raise HTTPException(404, "session not found")
    return abs_path


def _live_root_label(root: str) -> str:
    root = os.path.abspath(root)
    labels = (
        (LIVE_RESULTS_DIR, "production obw_platform/_reports/_live"),
        (LIVE_TOP_REPORTS_DIR, "repo _reports/_live"),
        (LIVE_REPO_REPORTS_DIR, "repo reports"),
        (LIVE_VERONIKA_REPORTS_DIR, "legacy veronika/reports"),
    )
    for candidate, label in labels:
        try:
            if root == os.path.abspath(candidate):
                return label
        except Exception:
            continue
    try:
        rel = os.path.relpath(root, REPO_ROOT)
        if not rel.startswith(".."):
            return rel
    except Exception:
        pass
    return root


def _live_session_sort_key(item: Dict[str, Any]) -> Tuple[float, int, int, str]:
    updated = item.get("updated_at")
    try:
        ts = pd.to_datetime(updated, utc=True, errors="coerce")
        epoch = float(ts.timestamp()) if pd.notna(ts) else 0.0
    except Exception:
        epoch = 0.0
    status_rank = 1 if item.get("status") == "running" else 0
    root_rank = 1 if item.get("root") == LIVE_RESULTS_DIR else 0
    return (epoch, status_rank, root_rank, str(item.get("path") or ""))


def _annotate_live_sessions(sessions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    by_name: Dict[str, List[Dict[str, Any]]] = {}
    for item in sessions:
        by_name.setdefault(str(item.get("name") or ""), []).append(item)
    newest_by_name: Dict[str, str] = {}
    for name, items in by_name.items():
        if len(items) > 1:
            newest_by_name[name] = max(items, key=_live_session_sort_key).get("path")
    for item in sessions:
        root = str(item.get("root") or "")
        item["root_label"] = _live_root_label(root)
        dup = len(by_name.get(str(item.get("name") or ""), [])) > 1
        item["duplicate_name"] = dup
        if dup:
            item["display_name"] = f"{item.get('name')} [{item['root_label']}]"
            if item.get("path") != newest_by_name.get(str(item.get("name") or "")):
                item["stale_duplicate"] = True
        else:
            item["display_name"] = item.get("name")
    return sorted(sessions, key=_live_session_sort_key, reverse=True)


def _build_live_session_summary(session_dir: str, light: bool = False) -> Dict[str, Any]:
    st = os.stat(session_dir)
    meta_list = _session_meta_candidates(session_dir)
    meta = _extract_meta_fields(meta_list)
    if meta["open_legs"] is None and not light:
        meta["open_legs"] = len(_session_table_rows(session_dir, "open_positions", limit=1000))
    if meta["filled_orders"] is None and not light:
        meta["filled_orders"] = len(_session_table_rows(session_dir, "orders", limit=10000))
    if meta["last_debug_event"] is None and not light:
        debug_rows = _session_table_rows(session_dir, "debug_events", limit=1)
        if debug_rows:
            top = debug_rows[-1]
            meta["last_debug_event"] = {
                "level": top.get("level") if isinstance(top, dict) else None,
                "event_type": top.get("event_type") if isinstance(top, dict) else None,
                "ts": top.get("ts") if isinstance(top, dict) else None,
            }
    if meta["last_equity_ts"] is None:
        if light:
            meta["last_equity_ts"] = _last_csv_ts(session_dir, ["equity.csv", "live_equity.csv", "equity_curve.csv"])
        else:
            live = _load_series_file(session_dir, ["equity.csv", "live_equity.csv", "equity_curve.csv"])
            if live:
                meta["last_equity_ts"] = live[-1]["ts"]
    updated_at = meta["updated_at"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(st.st_mtime))
    status = _infer_live_status(meta["status"], session_dir, updated_at, meta["last_debug_event"])
    return {
        "name": os.path.basename(session_dir),
        "path": session_dir,
        "exchange": meta["exchange"],
        "timeframe": str(meta["timeframe"]) if meta["timeframe"] is not None else None,
        "status": status,
        "started_at": meta["started_at"],
        "updated_at": updated_at,
        "open_legs": int(meta["open_legs"] or 0),
        "filled_orders": int(meta["filled_orders"] or 0),
        "last_debug_event": meta["last_debug_event"],
        "last_equity_ts": meta["last_equity_ts"],
    }



def _read_csv_if_exists(path: Optional[str]) -> pd.DataFrame:
    if not path or not os.path.exists(path):
        return pd.DataFrame()
    try:
        return pd.read_csv(path, encoding="utf-8-sig")
    except Exception:
        return pd.DataFrame()

def _compute_mdd(cum: List[float]) -> float:
    if not cum:
        return 0.0
    arr = np.array(cum, dtype=float)
    peak = np.maximum.accumulate(arr)
    dd = arr - peak
    return float(dd.min())


def _compute_position_state(rows: pd.DataFrame, side_col: str, qty_col: str, price_col: str, entry_value: str, exit_value: str) -> Dict[str, float]:
    pos_qty = 0.0
    avg = 0.0
    for _, row in rows.iterrows():
        side = str(row.get(side_col, ""))
        qty = abs(_safe_float(row.get(qty_col)))
        price = _safe_float(row.get(price_col))
        is_entry = entry_value.lower() in side.lower()
        is_exit = exit_value.lower() in side.lower()
        if is_entry and qty > 0:
            new_qty = pos_qty + qty
            avg = ((avg * pos_qty) + (price * qty)) / new_qty if new_qty > 0 else 0.0
            pos_qty = new_qty
        elif is_exit and qty > 0:
            pos_qty = max(0.0, pos_qty - qty)
            if pos_qty == 0:
                avg = 0.0
    return {"qty": float(pos_qty), "entry": float(avg)}


def _load_validation_dataset(run_id: str) -> Dict[str, Any]:
    run_dir = _validation_run_dir(run_id)
    meta_path = os.path.join(run_dir, "meta.json")
    if not os.path.isfile(meta_path):
        raise HTTPException(404, "run not found")
    meta = json.load(open(meta_path, "r"))
    tv_path = meta.get("tv_path")
    match_ready_csv = meta.get("match_ready_csv")
    matched_csv = meta.get("matched_csv")
    unmatched_tv_csv = meta.get("unmatched_tv_csv")
    unmatched_real_csv = meta.get("unmatched_real_csv")

    tv_df = _read_tv_csv(tv_path) if tv_path and os.path.exists(tv_path) else pd.DataFrame()
    if (not match_ready_csv or not os.path.exists(match_ready_csv)) and os.path.isdir(run_dir):
        candidates = sorted(glob.glob(os.path.join(run_dir, "*_trade_history_for_match.csv")))
        if candidates:
            match_ready_csv = candidates[0]
    bx_df = _read_csv_if_exists(match_ready_csv)
    matched_df = _read_csv_if_exists(matched_csv)
    unmatched_tv = _read_csv_if_exists(unmatched_tv_csv)
    unmatched_real = _read_csv_if_exists(unmatched_real_csv)
    canvas_candidates = sorted(glob.glob(os.path.join(run_dir, "*_canvas.png")))
    fallback_canvas_url = None
    if canvas_candidates:
        fallback_canvas_url = f"/api/backtest_live_validation/run/{run_id}/files/{os.path.basename(canvas_candidates[0])}"

    if not bx_df.empty:
        bx_df = bx_df.copy()
        bx_df["dt"] = _parse_bingx_datetime_series(bx_df.get("Час виконання", pd.Series(dtype=object)))
        bx_df["qty"] = bx_df.get("Виконано", pd.Series(dtype=object)).apply(_extract_first_float)
        bx_df["price"] = bx_df.get("Ціна виконання", pd.Series(dtype=object)).apply(_extract_first_float)
        bx_df["closed_pnl"] = bx_df.get("Закриті PnL / %", pd.Series(dtype=object)).apply(_extract_first_float)
        bx_df["fee"] = bx_df.get("Комісія", pd.Series(dtype=object)).apply(_extract_first_float)
        bx_df["net_pnl"] = bx_df["closed_pnl"] + bx_df["fee"]

    if not tv_df.empty:
        tv_df = tv_df.sort_values("dt")
        tv_df["Net P&L USDT"] = pd.to_numeric(tv_df.get("Net P&L USDT"), errors="coerce").fillna(0.0)
        tv_df["Size (qty)"] = pd.to_numeric(tv_df.get("Size (qty)"), errors="coerce").fillna(0.0).abs()
        tv_df["Price USDT"] = pd.to_numeric(tv_df.get("Price USDT"), errors="coerce").fillna(0.0)

    backtest_line = tv_df[tv_df.get("Type", "").astype(str).str.contains("Exit", case=False, na=False)].copy() if not tv_df.empty else pd.DataFrame()
    if not backtest_line.empty:
        backtest_line["cum_pnl"] = backtest_line["Net P&L USDT"].cumsum()
    live_line = bx_df.sort_values("dt").copy() if not bx_df.empty else pd.DataFrame()
    if live_line.empty and not matched_df.empty:
        # Fallback for environments where match-ready CSV path/name differs.
        fallback = matched_df.copy()
        fallback["dt"] = pd.to_datetime(fallback.get("real_time"), errors="coerce", utc=True)
        net_from_real = pd.to_numeric(fallback.get("real_net_pnl"), errors="coerce")
        if net_from_real.isna().all():
            closed = pd.to_numeric(fallback.get("real_closed_pnl"), errors="coerce").fillna(0.0)
            fee = pd.to_numeric(fallback.get("real_fee"), errors="coerce").fillna(0.0)
            net_from_real = closed + fee
        fallback["net_pnl"] = net_from_real.fillna(0.0)
        fallback["qty"] = pd.to_numeric(fallback.get("real_qty"), errors="coerce").fillna(0.0).abs()
        fallback["price"] = pd.to_numeric(fallback.get("real_price"), errors="coerce").fillna(0.0)
        fallback["Ф’ючерси / Напрямок"] = fallback.get("side", pd.Series(dtype=object))
        fallback["Ф’ючерси / Напрямок"] = np.where(
            fallback["Ф’ючерси / Напрямок"].astype(str).str.contains("Entry", case=False, na=False),
            "Відкрити Short",
            "Закрити Short",
        )
        live_line = fallback.sort_values("dt")
    if not live_line.empty:
        live_line["cum_pnl"] = live_line["net_pnl"].cumsum()

    base_equity = 10000.0
    pnl_chart = {
        "backtest": [{"ts": r["dt"].isoformat(), "value": float(r["cum_pnl"])} for _, r in backtest_line.iterrows()],
        "live": [{"ts": r["dt"].isoformat(), "value": float(r["cum_pnl"])} for _, r in live_line.iterrows() if pd.notna(r.get("dt"))],
    }

    margin_chart = {
        "backtest_margin_used_pct": [],
        "live_margin_used_pct": [],
        "live_available_margin": [],
        "live_free_margin_pct": [],
    }
    for _, r in tv_df.iterrows():
        used_pct = ((abs(float(r["Size (qty)"])) * float(r["Price USDT"])) / base_equity) * 100.0 if base_equity else 0.0
        margin_chart["backtest_margin_used_pct"].append({"ts": r["dt"].isoformat(), "value": float(used_pct)})
    for _, r in live_line.iterrows():
        if pd.isna(r.get("dt")):
            continue
        used_pct = ((abs(float(r.get("qty", 0.0))) * float(r.get("price", 0.0))) / base_equity) * 100.0 if base_equity else 0.0
        available = base_equity - ((abs(float(r.get("qty", 0.0))) * float(r.get("price", 0.0))))
        margin_chart["live_margin_used_pct"].append({"ts": r["dt"].isoformat(), "value": float(used_pct)})
        margin_chart["live_available_margin"].append({"ts": r["dt"].isoformat(), "value": float(available)})
        margin_chart["live_free_margin_pct"].append({"ts": r["dt"].isoformat(), "value": float(max(0.0, 100.0 - used_pct))})

    slippage_chart = {"signed_bps": [], "abs_bps": [], "rolling_signed_bps": []}
    if not matched_df.empty:
        matched_df = matched_df.copy()
        matched_df["real_time"] = pd.to_datetime(matched_df.get("real_time"), errors="coerce", utc=True)
        matched_df = matched_df.sort_values("real_time")
        matched_df["rolling_signed"] = pd.to_numeric(matched_df.get("signed_slippage_bps"), errors="coerce").rolling(15, min_periods=3).mean()
        for _, r in matched_df.iterrows():
            ts = r.get("real_time")
            if pd.isna(ts):
                continue
            slippage_chart["signed_bps"].append({"ts": ts.isoformat(), "value": _safe_float(r.get("signed_slippage_bps"))})
            slippage_chart["abs_bps"].append({"ts": ts.isoformat(), "value": _safe_float(r.get("abs_slippage_bps"))})
            slippage_chart["rolling_signed_bps"].append({"ts": ts.isoformat(), "value": _safe_float(r.get("rolling_signed"))})

    bt_state = _compute_position_state(tv_df, "Type", "Size (qty)", "Price USDT", "Entry short", "Exit short") if not tv_df.empty else {"qty": 0.0, "entry": 0.0}
    live_state = _compute_position_state(live_line, "Ф’ючерси / Напрямок", "qty", "price", "Відкрити", "Закрити") if not live_line.empty else {"qty": 0.0, "entry": 0.0}

    bt_realized = float(backtest_line["Net P&L USDT"].sum()) if not backtest_line.empty else 0.0
    live_realized = float(live_line["net_pnl"].sum()) if not live_line.empty else 0.0
    bt_last_price = float(tv_df["Price USDT"].iloc[-1]) if not tv_df.empty else 0.0
    live_last_price = float(live_line["price"].iloc[-1]) if not live_line.empty else 0.0
    bt_floating = (bt_state.get("entry", 0.0) - bt_last_price) * bt_state.get("qty", 0.0) if bt_state.get("qty", 0.0) > 0 else 0.0
    live_floating = (live_state.get("entry", 0.0) - live_last_price) * live_state.get("qty", 0.0) if live_state.get("qty", 0.0) > 0 else 0.0
    bt_cum = [p["value"] for p in pnl_chart["backtest"]]
    live_cum = [p["value"] for p in pnl_chart["live"]]
    bt_mtm_curve = bt_cum + ([bt_realized + bt_floating] if bt_cum else [])
    live_mtm_curve = live_cum + ([live_realized + live_floating] if live_cum else [])

    avg_diff = float(live_state.get("entry", 0.0) - bt_state.get("entry", 0.0))
    avg_color = "neutral"
    if bt_state.get("entry", 0.0) > 0 and live_state.get("entry", 0.0) > 0:
        avg_color = "red" if live_state["entry"] < bt_state["entry"] else "green" if live_state["entry"] > bt_state["entry"] else "neutral"

    interval_seconds = meta.get("inspect", {}).get("bar_interval_seconds")
    poll_interval_ms = int(max(5000, int(interval_seconds or 60) * 1000 + 2000))

    stats = {
        "backtest": {
            "realized_pnl": bt_realized,
            "floating_pnl": bt_floating,
            "realized_mdd": _compute_mdd(bt_cum),
            "mtm_dd": _compute_mdd(bt_mtm_curve),
            "current_position_size": bt_state.get("qty", 0.0),
            "current_average_price": bt_state.get("entry", 0.0),
        },
        "live": {
            "realized_pnl": live_realized,
            "floating_pnl": live_floating,
            "realized_mdd": _compute_mdd(live_cum),
            "mtm_dd": _compute_mdd(live_mtm_curve),
            "current_position_size": live_state.get("qty", 0.0),
            "current_average_price": live_state.get("entry", 0.0),
            "current_available_margin": margin_chart["live_available_margin"][-1]["value"] if margin_chart["live_available_margin"] else base_equity,
            "current_used_margin_pct": margin_chart["live_margin_used_pct"][-1]["value"] if margin_chart["live_margin_used_pct"] else 0.0,
            "current_free_margin_pct": margin_chart["live_free_margin_pct"][-1]["value"] if margin_chart["live_free_margin_pct"] else 100.0,
        },
        "comparison": {
            "avg_price_diff": avg_diff,
            "avg_price_diff_color": avg_color,
            "symbol": meta.get("inspect", {}).get("symbol"),
            "bar_interval": meta.get("inspect", {}).get("bar_interval_label"),
            "selected_window": {
                "start": meta.get("inspect", {}).get("start"),
                "end": meta.get("inspect", {}).get("end"),
            },
            "matched_rows": int(len(matched_df)),
            "matched_share": float(len(matched_df) / len(live_line)) if len(live_line) else 0.0,
            "mean_abs_slippage": float(pd.to_numeric(matched_df.get("abs_slippage_bps"), errors="coerce").mean()) if not matched_df.empty else 0.0,
            "mean_signed_slippage": float(pd.to_numeric(matched_df.get("signed_slippage_bps"), errors="coerce").mean()) if not matched_df.empty else 0.0,
        },
    }

    debug_info = {
        "selected_backtest_file_path": tv_path,
        "inferred_symbol": meta.get("inspect", {}).get("symbol"),
        "inferred_start": meta.get("inspect", {}).get("start"),
        "inferred_end": meta.get("inspect", {}).get("end"),
        "inferred_bar_interval": meta.get("inspect", {}).get("bar_interval_label"),
        "last_refresh_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "current_polling_interval_ms": poll_interval_ms,
        "counts": {
            "backtest_rows": int(len(tv_df)),
            "live_rows": int(len(live_line)),
            "matched_rows": int(len(matched_df)),
            "unmatched_backtest_rows": int(len(unmatched_tv)),
            "unmatched_live_rows": int(len(unmatched_real)),
        },
        "status": meta.get("status", {}),
        "stderr_tail": (meta.get("stderr") or "")[-2000:],
    }

    payload = {
        "run_id": run_id,
        "inspect": meta.get("inspect", {}),
        "pnl_chart": pnl_chart,
        "margin_chart": margin_chart,
        "slippage_chart": slippage_chart,
        "stats": stats,
        "live_state": live_state,
        "backtest_state": bt_state,
        "poll_interval_ms": poll_interval_ms,
        "debug": debug_info,
        "fallback_canvas_url": fallback_canvas_url,
    }
    log.info(
        "bt_live_validation dataset run_id=%s pnl(back=%d,live=%d) margin(back=%d,live=%d) slippage=%d",
        run_id,
        len(pnl_chart["backtest"]),
        len(pnl_chart["live"]),
        len(margin_chart["backtest_margin_used_pct"]),
        len(margin_chart["live_margin_used_pct"]),
        len(slippage_chart["signed_bps"]),
    )
    return payload


@app.get("/api/backtest_live_validation/files")
def backtest_live_validation_files():
    return _list_validation_source_files()


@app.get("/api/backtest_live_validation/live_sessions")
def backtest_live_validation_live_sessions():
    sessions: List[Dict[str, Any]] = []
    roots = _live_results_roots()
    for root in roots:
        if not os.path.isdir(root):
            continue
        for entry in sorted(os.scandir(root), key=lambda e: e.name.lower()):
            if not entry.is_dir() or not _looks_like_live_session_dir(entry.path):
                continue
            if os.path.abspath(root) == os.path.abspath(LIVE_REPO_REPORTS_DIR) and not _has_live_runner_artifact(entry.path):
                continue
            try:
                summary = _build_live_session_summary(entry.path, light=True)
                summary["root"] = root
                sessions.append(summary)
            except Exception:
                sessions.append(
                    {
                        "name": entry.name,
                        "path": entry.path,
                        "root": root,
                        "exchange": None,
                        "timeframe": None,
                        "status": "unknown",
                        "updated_at": None,
                    }
                )
    sessions = _annotate_live_sessions(sessions)
    return {"root": LIVE_RESULTS_DIR, "roots": roots, "sessions": sessions}


@app.post("/api/backtest_live_validation/live_session/inspect")
def backtest_live_validation_live_session_inspect(payload: Dict[str, Any] = Body(default={})):  # noqa: B008
    path = payload.get("path")
    if not path:
        raise HTTPException(400, "path is required")
    session_dir = _resolve_live_session_path(path)
    summary = _build_live_session_summary(session_dir)
    return {
        "path": summary["path"],
        "name": summary["name"],
        "exchange": summary.get("exchange"),
        "timeframe": summary.get("timeframe"),
        "status": summary.get("status"),
        "started_at": summary.get("started_at"),
        "updated_at": summary.get("updated_at"),
        "open_legs": summary.get("open_legs", 0),
        "filled_orders": summary.get("filled_orders", 0),
        "last_debug_event": summary.get("last_debug_event"),
        "last_equity_ts": summary.get("last_equity_ts"),
    }


@app.get("/api/backtest_live_validation/live_session/status")
def backtest_live_validation_live_session_status(path: str = Query(default="")):
    if not path:
        raise HTTPException(400, "path is required")
    session_dir = _resolve_live_session_path(path)
    return _build_live_session_summary(session_dir)


@app.get("/api/backtest_live_validation/live_session/chart")
def backtest_live_validation_live_session_chart(
    path: str = Query(default=""),
    backtest_path: str = Query(default=""),
    tv_path: str = Query(default=""),
    max_points: int = Query(default=1800),
):
    if not path:
        raise HTTPException(400, "path is required")
    session_dir = _resolve_live_session_path(path)
    snapshot_payload = _live_snapshot_chart_payload(session_dir)
    if snapshot_payload is not None:
        return snapshot_payload
    sources: Dict[str, Any] = {}
    warnings: List[str] = []
    sqlite_snapshot_live = _sqlite_live_equity_snapshot_series(session_dir)
    if sqlite_snapshot_live:
        live, live_source = sqlite_snapshot_live, "session.sqlite:equity"
    else:
        live, live_source = _load_series_file_with_source(session_dir, ["live_equity.csv", "live_pnl.csv", "equity.csv", "equity_curve.csv"])
        sqlite_live = _sqlite_live_equity_series(session_dir)
        live, live_source = _prefer_broader_live_series(
            live,
            live_source,
            sqlite_live,
            "session.sqlite:equity" if sqlite_live else None,
        )
    if live:
        live = _minute_close_series(live)
    if not live:
        live = _sqlite_live_order_series(session_dir)
        if live:
            live = _minute_close_series(live)
            live_source = "session.sqlite:orders"
            warnings.append("Live chart uses filled order notional only; this is approximate and not real PnL.")
    backtest, backtest_source = _load_series_file_with_source(session_dir, ["backtest_equity.csv", "backtest_pnl.csv", "equity_backtest.csv"])
    backtest_realized, backtest_realized_source = _load_series_column_file_with_source(
        session_dir,
        ["backtest_equity.csv", "backtest_pnl.csv", "equity_backtest.csv"],
        ["realized_pnl", "realized_value", "realized_equity"],
    )
    if backtest_realized and backtest_realized_source == "backtest_equity.csv":
        base_realized = backtest_realized[0]["value"]
        backtest_realized = [{**point, "value": float(point["value"] - base_realized)} for point in backtest_realized]
    live_realized, live_realized_source = _live_realized_pnl_series(session_dir)
    selected_tv_path = backtest_path or tv_path
    backtest_price = _tv_backtest_price_series(selected_tv_path)
    backtest_price_source = "TradingView CSV:Price USDT" if backtest_price else None
    if not backtest_price:
        backtest_price, backtest_price_source = _load_series_file_with_source(
            session_dir,
            ["backtest_price.csv", "price_backtest.csv", "backtest_mark.csv"],
        )
    distance, distance_source = _load_series_file_with_source(session_dir, ["distance.csv", "absolute_distance.csv"])
    if not distance and live and backtest:
        bt_map = {row["ts"]: row["value"] for row in backtest}
        computed: List[Dict[str, Any]] = []
        for row in live:
            ts = row["ts"]
            if ts in bt_map:
                computed.append({"ts": ts, "value": abs(float(row["value"]) - float(bt_map[ts]))})
        distance = computed
        if distance:
            distance_source = "computed"
    price_bars, price_bars_source = _live_ohlcv_bars_from_npz(session_dir)
    csv_price_bars, csv_price_source = _live_ohlcv_bars_from_csv(session_dir)
    if csv_price_bars:
        price_bars = _merge_price_bars(price_bars, csv_price_bars)
        price_bars_source = "+".join([s for s in (price_bars_source, csv_price_source) if s])
    if _has_price_bar_gap(price_bars):
        telemetry_price_bars, telemetry_price_source = _live_telemetry_ohlcv_bars(session_dir)
        if telemetry_price_bars:
            price_bars = _merge_price_bars(price_bars, telemetry_price_bars)
            price_bars, short_gap_fills = _fill_short_price_bar_gaps(price_bars)
            price_bars_source = "+".join([s for s in (price_bars_source, telemetry_price_source) if s])
            warnings.append("Compact OHLCV artifacts have time gaps; missing 1m price bars were filled from telemetry mark data.")
            if short_gap_fills:
                price_bars_source = "+".join([s for s in (price_bars_source, "short_flat_gap_fill") if s])
                warnings.append(f"Filled {short_gap_fills} short residual price-bar gap(s) with flat close bars.")
    mark = _live_mark_points_from_telemetry(session_dir)
    markers = _live_event_markers(session_dir)
    telemetry_paths = _live_telemetry_paths(session_dir)
    telemetry_is_heavy = _live_telemetry_total_bytes(telemetry_paths) > MAX_TELEMETRY_BYTES_FOR_CHART
    if telemetry_is_heavy:
        warnings.append("Telemetry JSONL is too large for request-time full parsing; chart uses compact OHLCV artifacts plus latest telemetry mark line.")
    anchor_time = None
    anchor_price: Optional[float] = None
    if mark:
        anchor_time = mark[-1]["ts"]
        anchor_price = float(mark[-1]["value"])
        sources["mark"] = "latest live telemetry jsonl:1m" if telemetry_is_heavy else "live telemetry jsonl:1m"
    elif live:
        anchor_time = live[-1]["ts"]
        anchor_price = float(live[-1]["value"])
    labels = _live_strategy_labels(session_dir, anchor_time, anchor_price)
    price_lines = _live_target_price_lines(session_dir)
    payload: Dict[str, Any] = {}
    if live:
        payload["live"] = live
        sources["live"] = live_source
    if backtest:
        payload["backtest"] = backtest
        sources["backtest"] = backtest_source
    if live_realized:
        payload["live_realized"] = live_realized
        sources["live_realized"] = live_realized_source
    if backtest_realized:
        payload["backtest_realized"] = backtest_realized
        sources["backtest_realized"] = backtest_realized_source
    if backtest_price:
        payload["backtest_price"] = backtest_price
        sources["backtest_price"] = backtest_price_source
    if distance:
        payload["distance"] = distance
        sources["distance"] = distance_source
    if mark:
        payload["mark"] = mark
    if price_bars:
        payload["price_bars"] = price_bars
        sources["price_bars"] = price_bars_source
    if markers:
        payload["markers"] = markers
    if labels:
        payload["labels"] = labels
    if price_lines:
        payload["price_lines"] = price_lines
    if sources:
        payload["sources"] = sources
    if warnings:
        payload["warnings"] = warnings
        payload["approximate"] = True
    _downsample_live_chart_payload(payload, _chart_point_budget(max_points))
    return payload


@app.get("/api/backtest_live_validation/live_session/table")
def backtest_live_validation_live_session_table(path: str = Query(default=""), kind: str = Query(default="open_positions")):
    if not path:
        raise HTTPException(400, "path is required")
    if kind not in LIVE_SESSION_TABLE_KINDS:
        raise HTTPException(400, "invalid kind")
    session_dir = _resolve_live_session_path(path)
    return {"rows": _session_table_rows(session_dir, kind)}


@app.post("/api/backtest_live_validation/upload")
async def backtest_live_validation_upload(file: UploadFile = File(...)):
    os.makedirs(VALIDATION_UPLOADS_DIR, exist_ok=True)
    token = f"upload_{uuid.uuid4().hex}"
    ext = os.path.splitext(file.filename or "upload.csv")[1] or ".csv"
    dst = os.path.join(VALIDATION_UPLOADS_DIR, f"{token}{ext}")
    data = await file.read()
    with open(dst, "wb") as fh:
        fh.write(data)
    inspect = _inspect_tv_path(dst)
    return {"upload_token": token, "path": dst, "inspect": inspect}


@app.post("/api/backtest_live_validation/inspect")
def backtest_live_validation_inspect(payload: Dict[str, Any] = Body(default={})):  # noqa: B008
    path = payload.get("path")
    if not path:
        raise HTTPException(400, "path is required")
    abs_path = os.path.abspath(path)
    if not _is_allowed_validation_path(abs_path):
        raise HTTPException(400, "path is outside allowed roots")
    if not os.path.exists(abs_path):
        raise HTTPException(404, "file not found")
    return _inspect_tv_path(abs_path)


@app.post("/api/backtest_live_validation/run")
def backtest_live_validation_run(payload: Dict[str, Any] = Body(default={})):  # noqa: B008
    tv_path = payload.get("path")
    if not tv_path:
        raise HTTPException(400, "path is required")
    tv_path = os.path.abspath(tv_path)
    if not _is_allowed_validation_path(tv_path):
        raise HTTPException(400, "path is outside allowed roots")
    if not os.path.exists(tv_path):
        raise HTTPException(404, "file not found")

    run_match = bool(payload.get("run_match", True))
    auto_fetch_live = bool(payload.get("auto_fetch_live", True))
    selected_live_path = payload.get("live_path")
    debug = bool(payload.get("debug", False))
    run_id = f"validation_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_dir = _validation_run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(VALIDATION_REPORTS_DIR, exist_ok=True)

    inspect = _inspect_tv_path(tv_path)
    extract_script = os.path.join(BT_ROOT, "extract_bingx_window_from_tv.py")
    python_bin = sys.executable or "python3"
    cmd = [
        python_bin,
        extract_script,
        "--tv",
        tv_path,
        "--out-root",
        VALIDATION_REPORTS_DIR,
        "--label",
        run_id,
    ]
    if run_match:
        cmd.append("--run-match")
    log.info("bt_live_validation run selected_file=%s symbol=%s timeframe=%s start=%s end=%s", tv_path, inspect.get("symbol"), inspect.get("bar_interval_label"), inspect.get("start"), inspect.get("end"))
    log.info("bt_live_validation extract command: %s", " ".join(cmd))

    symbol = inspect.get("symbol") or "symbol"
    symbol_safe = symbol.replace("-", "_")
    proc = None
    stdout = ""
    stderr = ""
    extract_returncode = 0
    if auto_fetch_live:
        max_attempts = 3
        for attempt in range(1, max_attempts + 1):
            try:
                proc = subprocess.run(cmd, cwd=BT_ROOT, capture_output=True, text=True)
            except Exception as e:
                raise HTTPException(status_code=500, detail={"error": "extract launch failed", "message": str(e), "cmd": cmd})
            stderr = proc.stderr or ""
            stdout = proc.stdout or ""
            extract_returncode = int(proc.returncode)
            if proc.returncode == 0:
                break
            if attempt < max_attempts and _is_retryable_extract_error(stdout, stderr):
                log.warning("bt_live_validation extract retrying attempt=%s/%s due to retryable error", attempt, max_attempts)
                time.sleep(2)
                continue
            break
        if not proc or proc.returncode != 0:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "extract failed",
                    "stdout": stdout[-4000:],
                    "stderr": stderr[-4000:],
                    "hint": "BingX API may be temporarily unavailable; retry shortly if code 106551/system error appears. You can also select a live session and run with auto_fetch_live disabled.",
                },
            )
    else:
        stdout = "[info] auto_fetch_live disabled; using local live session artifacts when available"
        if selected_live_path:
            live_match = _find_live_match_ready_csv(str(selected_live_path), symbol)
            if live_match:
                dst = os.path.join(run_dir, f"{symbol_safe}_trade_history_for_match.csv")
                try:
                    shutil.copy2(live_match, dst)
                    stdout += f"\\n[info] reused live match csv: {live_match}"
                except Exception as e:
                    stderr += f"\\n[warn] failed to reuse live match csv: {e}"
            else:
                try:
                    session_dir = _resolve_live_session_path(str(selected_live_path))
                    dst = os.path.join(run_dir, f"{symbol_safe}_trade_history_for_match.csv")
                    generated = _write_live_orders_match_csv_from_sqlite(session_dir, dst, symbol)
                    if generated:
                        stdout += f"\\n[info] generated live match csv from session sqlite: {generated}"
                    else:
                        stderr += "\\n[warn] selected live session has no reusable match csv or filled sqlite orders"
                except HTTPException:
                    raise
                except Exception as e:
                    stderr += f"\\n[warn] failed to generate live match csv from session sqlite: {e}"

    match_ready_candidates = [
        os.path.join(run_dir, f"{symbol}_trade_history_for_match.csv"),
        os.path.join(run_dir, f"{symbol_safe}_trade_history_for_match.csv"),
    ]
    match_ready_candidates.extend(sorted(glob.glob(os.path.join(run_dir, "*_trade_history_for_match.csv"))))
    resolved_match_ready = next((p for p in match_ready_candidates if os.path.exists(p)), match_ready_candidates[0])
    meta = {
        "run_id": run_id,
        "run_dir": run_dir,
        "tv_path": tv_path,
        "inspect": inspect,
        "cmd": cmd,
        "stdout": stdout,
        "stderr": stderr,
        "status": {"extract_returncode": extract_returncode, "run_match": run_match, "debug": debug, "auto_fetch_live": auto_fetch_live},
        "match_ready_csv": resolved_match_ready,
        "matched_csv": os.path.join(run_dir, f"{run_id}_matched_orders.csv"),
        "unmatched_real_csv": os.path.join(run_dir, f"{run_id}_unmatched_real_orders.csv"),
        "unmatched_tv_csv": os.path.join(run_dir, f"{run_id}_unmatched_tv_packs.csv"),
        "summary_csv": os.path.join(run_dir, f"{run_id}_summary.csv"),
        "slippage_csv": os.path.join(run_dir, f"{run_id}_slippage_breakdown.csv"),
    }
    json.dump(meta, open(os.path.join(run_dir, "meta.json"), "w"), indent=2)

    return {
        "run_id": run_id,
        "run_dir": run_dir,
        "inspect": inspect,
        "output_paths": {
            "match_ready_csv": meta["match_ready_csv"],
            "matched_csv": meta["matched_csv"],
            "summary_csv": meta["summary_csv"],
            "slippage_csv": meta["slippage_csv"],
        },
        "status": meta["status"],
    }


@app.get("/api/backtest_live_validation/run/{run_id}")
def backtest_live_validation_run_details(run_id: str):
    return _load_validation_dataset(run_id)


@app.get("/api/backtest_live_validation/run/{run_id}/poll")
def backtest_live_validation_poll(run_id: str):
    data = _load_validation_dataset(run_id)
    return {
        "run_id": run_id,
        "poll_interval_ms": data.get("poll_interval_ms"),
        "pnl_chart": {
            "backtest": data.get("pnl_chart", {}).get("backtest", []),
            "live": data.get("pnl_chart", {}).get("live", []),
        },
        "margin_chart": {
            "live_margin_used_pct": data.get("margin_chart", {}).get("live_margin_used_pct", []),
            "live_available_margin": data.get("margin_chart", {}).get("live_available_margin", []),
            "live_free_margin_pct": data.get("margin_chart", {}).get("live_free_margin_pct", []),
        },
        "stats": {"live": data.get("stats", {}).get("live", {}), "comparison": data.get("stats", {}).get("comparison", {})},
        "debug": data.get("debug", {}),
        "fallback_canvas_url": data.get("fallback_canvas_url"),
    }


@app.get("/api/backtest_live_validation/run/{run_id}/files/{fn:path}")
def backtest_live_validation_run_file(run_id: str, fn: str):
    run_dir = _validation_run_dir(run_id)
    p = os.path.abspath(os.path.join(run_dir, fn))
    if not _is_within(p, run_dir) or not os.path.isfile(p):
        raise HTTPException(404, "not found")
    return FileResponse(p)


@app.get("/api/live_results/{name}/files/{fn:path}")
def live_file(name: str, fn: str):
    base = os.path.join(LIVE_RESULTS_DIR, name)
    p = os.path.join(base, fn)
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    return FileResponse(p)
