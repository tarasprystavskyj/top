# FastAPI MVP backend
import os, json, uuid, subprocess, threading, queue, time, shutil, yaml, glob, itertools, copy
from typing import Any, Dict, Optional, List
from fastapi import FastAPI, HTTPException, Body, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel
import logging

log = logging.getLogger(__name__)

try:
    # Prefer the variant with additional comments if available
    from obw_platform.engine.visualize_results_1 import plot_equity_curves as _viz_plot
except Exception:  # pragma: no cover - best effort fallback
    try:
        from obw_platform.engine.visualize_results import plot_equity_curves as _viz_plot
    except Exception:  # pragma: no cover - missing dependency (e.g. matplotlib)
        _viz_plot = None

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
BT_VERSION_FILE = os.path.join(DATA_ROOT, "backtester_version.yaml")
BACKTESTER_SCRIPTS = [
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
    "backtester_core_speed3_veto_universe_2.py": {"plots": True, "time_range": True, "export_csv": True},
    "backtester_core_speed3_veto_universe.py": {"plots": True},
    "backtester_core_speed3.py": {"plots": True},
    "backtester_core_speed3_veto.py": {"plots": True},
}

DEFAULT_CORS_ORIGINS = [
    "http://localhost:3000",
    "http://127.0.0.1:3000",
    "http://0.0.0.0:3000",
    "https://localhost:3000",
    "https://127.0.0.1:3000",
    "https://0.0.0.0:3000",
]


def _load_cors_origins() -> List[str]:
    origins: List[str] = []
    seen = set()
    for origin in DEFAULT_CORS_ORIGINS:
        if not origin or origin in seen:
            continue
        origins.append(origin)
        seen.add(origin)
    for env_name in ("CORS_ALLOW_ORIGINS", "FRONTEND_ORIGINS"):
        raw = os.getenv(env_name)
        if not raw:
            continue
        for item in raw.split(","):
            value = item.strip()
            if not value or value in seen:
                continue
            origins.append(value)
            seen.add(value)
    return origins

# --- helpers: cache DB discovery -------------------------------------------
def _list_cache_db_files() -> List[Dict[str, str]]:
    """Return available cache DB files under ``CACHE_DB_DIR``.

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
        if not lower.endswith((".db", ".sqlite", ".sqlite3")):
            continue
        rel_path = os.path.relpath(entry.path, REPO_ROOT)
        entries.append({"name": name, "path": rel_path})
    return entries


def _is_within(path: str, root: str) -> bool:
    try:
        common = os.path.commonpath([os.path.abspath(path), os.path.abspath(root)])
    except ValueError:
        return False
    return common == os.path.abspath(root)


def resolve_cache_db(value: Optional[str]) -> Optional[str]:
    """Resolve a cache DB selector value to an absolute path.

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

    # 1) try snapshots from equity table
    try:
        cols = [r[1] for r in cur.execute("PRAGMA table_info(equity);").fetchall()]
    except Exception:
        cols = []
    if cols:
        try:
            df = pd.read_sql("SELECT * FROM equity ORDER BY 1;", con)
            if not df.empty:
                tcol = next((c for c in df.columns if c.lower() in ("ts", "ts_utc", "time", "timestamp")), None)
                vcol = next((c for c in df.columns if c.lower() in ("equity", "equity_usdt", "value")), None)
                if tcol and vcol:
                    df = df[[tcol, vcol]].rename(columns={tcol: "ts", vcol: "equity"})
                    df["ts"] = pd.to_datetime(df["ts"], errors="coerce", utc=True)
                    df = df.dropna(subset=["ts", "equity"]).sort_values("ts")
                    if len(df) >= 2:
                        con.close()
                        return df
        except Exception:
            pass

    # 2) reconstruct from closed trades
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

    # initial_equity
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

    # extract closed trades
    cols = [r[1] for r in cur.execute(f"PRAGMA table_info({tbl});").fetchall()]
    has_status = "status" in cols
    has_fees = "fees_paid" in cols
    sel = "side, qty, entry_fill, exit_fill, exit_fill_ts" + (
        ", fees_paid" if has_fees else ""
    )
    where = "WHERE exit_fill IS NOT NULL AND exit_fill_ts IS NOT NULL"
    if has_status:
        where = "WHERE status='CLOSED' AND exit_fill IS NOT NULL AND exit_fill_ts IS NOT NULL"
    import pandas as pd, numpy as np
    df = pd.read_sql(
        f"SELECT {sel} FROM {tbl} {where} ORDER BY exit_fill_ts;", con
    )
    con.close()
    if df.empty:
        return None
    df["ts"] = pd.to_datetime(df["exit_fill_ts"], errors="coerce", utc=True)
    for c in ("qty", "entry_fill", "exit_fill"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if has_fees:
        df["fees_paid"] = pd.to_numeric(df["fees_paid"], errors="coerce").fillna(0.0)
    df["pnl"] = np.where(
        df["side"].str.upper() == "LONG",
        (df["exit_fill"] - df["entry_fill"]) * df["qty"],
        (df["entry_fill"] - df["exit_fill"]) * df["qty"],
    )
    if has_fees:
        df["pnl"] = df["pnl"] - df["fees_paid"]
    eq = (init_eq + df["pnl"].cumsum()).rename("equity")
    out = pd.DataFrame({"ts": df["ts"], "equity": eq})
    out = out.dropna(subset=["ts"]).sort_values("ts")
    return out


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
    sel = "symbol, side, qty, entry_fill, entry_fill_ts, exit_fill, exit_fill_ts"
    if has_fees:
        sel += ", fees_paid"
    where = "WHERE exit_fill IS NOT NULL AND exit_fill_ts IS NOT NULL"
    if has_status:
        where = (
            "WHERE status='CLOSED' AND exit_fill IS NOT NULL "
            "AND exit_fill_ts IS NOT NULL"
        )
    df = pd.read_sql(
        f"SELECT {sel} FROM {tbl} {where} ORDER BY exit_fill_ts;", con
    )
    con.close()
    if df.empty:
        return None
    for c in ("qty", "entry_fill", "exit_fill", "fees_paid"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    df["realised_pnl"] = np.where(
        df["side"].str.upper() == "LONG",
        (df["exit_fill"] - df["entry_fill"]) * df["qty"],
        (df["entry_fill"] - df["exit_fill"]) * df["qty"],
    )
    if has_fees and "fees_paid" in df:
        df["realised_pnl"] = df["realised_pnl"] - df["fees_paid"]
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

    # Convert trades to DataFrame for return histogram
    df = pd.DataFrame(trades)
    for c in ("entry_fill", "exit_fill", "qty", "fees_paid"):
        if c in df:
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    ret = np.where(
        df["side"].str.upper() == "LONG",
        (df["exit_fill"] - df["entry_fill"]) / df["entry_fill"],
        (df["entry_fill"] - df["exit_fill"]) / df["entry_fill"],
    )
    plt.figure(); plt.hist(ret, bins=30)
    plt.title("Distribution of Returns per Trade")
    plt.xlabel("Return per trade"); plt.ylabel("Count")
    plt.tight_layout(); plt.savefig(os.path.join(base_dir, "returns_hist.png"), dpi=140); plt.close()

    eq_curve = eq_df["equity"].to_numpy()
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
    cmd = ["python3", bt_script, "--cfg", cfg_path]
    if time_from and BACKTESTER_CAPABILITIES.get(bt_script, {}).get("time_range"):
        cmd += ["--time-from", time_from]
    if time_to and BACKTESTER_CAPABILITIES.get(bt_script, {}).get("time_range"):
        cmd += ["--time-to", time_to]
    if not (time_from or time_to):
        cmd += ["--limit-bars", str(limit_bars)]

    if cache_db:
        cmd += ["--cache_db", cache_db]
    if symbols_file:
        cmd += ["--symbols-file", symbols_file]
    if allow_symbols:
        if isinstance(allow_symbols, (list, tuple)):
            allow_symbols = ",".join(allow_symbols)
        cmd += ["--allow-symbols", allow_symbols]

    # Only add --plots if the selected backtester advertises support for it
    if plots_dir and BACKTESTER_CAPABILITIES.get(bt_script, {}).get("plots"):
        cmd += ["--plots", plots_dir]
    if export_csv and BACKTESTER_CAPABILITIES.get(bt_script, {}).get("export_csv"):
        cmd += ["--export-csv"]
    if debug:
        cmd += ["--debug"]
    return cmd

def find_config(name: str) -> Optional[str]:
    for d in CONFIG_DIRS:
        p = os.path.join(d, name)
        if os.path.isfile(p):
            return p
    return None


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
    with open(cfg_path, "w") as f:
        yaml.safe_dump(merged, f, sort_keys=False)
    logs = os.path.join(out_dir, "logs.txt")
    bt_script = meta.get("backtester") or load_backtester_version()
    cmd = cmd_backtester(
        cfg_path,
        meta["limit_bars"],
        meta.get("cache_db"),
        out_dir,
        bt_script,
        export_csv=True,
        debug=bool(meta.get("debug")),
    )
    cmd_str = " ".join(cmd)
    with lock:
        jobs.setdefault(jid, {}).setdefault("meta", {})
        jobs[jid]["cmd"] = cmd_str
        jobs[jid]["meta"].setdefault("cache_db", meta.get("cache_db"))
    with open(logs, "w") as lf:
        lf.write(f"[cmd] {cmd_str}\n")
        lf.flush()
        p = subprocess.Popen(cmd, cwd=BT_ROOT, stdout=lf, stderr=lf)
        p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"backtester failed with code {p.returncode}")
    save_backtester_version(bt_script)
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

_cors_origins = _load_cors_origins()
if _cors_origins:
    if "*" in _cors_origins:
        allow_origins = ["*"]
    else:
        allow_origins = _cors_origins
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allow_origins,
        allow_methods=["*"],
        allow_headers=["*"],
    )

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
        "capabilities": BACKTESTER_CAPABILITIES,
    }

@app.get("/api/configs")
def configs():
    out: Dict[str, Dict[str, Any]] = {}
    for d in CONFIG_DIRS:
        for p in sorted(glob.glob(os.path.join(d, "*.yaml"))):
            st = os.stat(p)
            name = os.path.basename(p)
            out[name] = {"name": name, "path": p, "updated_at": st.st_mtime}
    return list(out.values())


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

@app.post("/api/backtest")
def backtest(req: BacktestReq):
    req_meta = req.model_dump()
    cache_label = (req_meta.get("cache_db") or "").strip() or None
    resolved_cache = resolve_cache_db(cache_label) if cache_label else None
    if cache_label and not resolved_cache:
        raise HTTPException(400, f"cache db not found: {cache_label}")
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
        "backtester": req_meta.get("backtester") or load_backtester_version(),
    }
    if resolved_cache:
        meta["cache_db"] = resolved_cache
    if cache_label and cache_label != resolved_cache:
        meta["cache_db_label"] = cache_label
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
    ]
    for fn in ("summary.csv", "trades.csv", "cfg_merged.yaml", "logs.txt", *plot_files):
        p = os.path.join(out_dir, fn)
        if os.path.exists(p):
            arts[fn] = f"/api/jobs/{job_id}/artifacts/{fn}"
    summary = {}
    if "summary.csv" in arts:
        import csv
        with open(os.path.join(out_dir, "summary.csv")) as f:
            rows = list(csv.DictReader(f))
            if rows: summary = rows[0]
    trades = []
    if "trades.csv" in arts:
        import csv
        with open(os.path.join(out_dir, "trades.csv")) as f:
            trades = list(csv.DictReader(f))[:500]
    resp: Dict[str, Any] = {"summary": summary, "trades": trades, "artifacts": arts}
    job_info = jobs.get(job_id) or {}
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
        pass
    arts: Dict[str, str] = {}
    plot_files = [
        "returns_hist.png",
        "equity_by_trade.png",
        "equity_by_time.png",
        "drawdown_by_trade.png",
        "viz_equity_vs_trade.png",
        "viz_dd_vs_trade.png",
        "viz_equity_vs_time.png",
    ]
    for fn in plot_files:
        p = os.path.join(base, fn)
        if os.path.exists(p):
            arts[fn] = f"/api/live_results/{name}/files/{fn}"

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
    live_range = None
    live_trades: List[Dict[str, Any]] = []
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
                allow_syms = snap.get("symbols_whitelist") or snap.get("universe", {}).get("allow")
                sym_file = snap.get("universe", {}).get("file")
                if sym_file and sym_file != "<cli>":
                    symbols_file = sym_file
            con.close()
        except Exception:
            log.exception("live_result %s: failed to read session db", name)
        try:
            df = _session_equity_df(session_db)
            if df is not None and not df.empty:
                live_range = {
                    "start": df["ts"].iloc[0].isoformat(),
                    "end": df["ts"].iloc[-1].isoformat(),
                }
        except Exception:
            pass
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
            time_from=live_range["start"] if live_range else None,
            time_to=live_range["end"] if live_range else None,
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


@app.get("/api/live_results/{name}/files/{fn:path}")
def live_file(name: str, fn: str):
    base = os.path.join(LIVE_RESULTS_DIR, name)
    p = os.path.join(base, fn)
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    return FileResponse(p)
