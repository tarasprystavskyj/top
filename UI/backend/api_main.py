# FastAPI MVP backend
import os, json, uuid, subprocess, threading, queue, time, shutil, yaml, glob, itertools, copy, re
from typing import Any, Dict, Optional, List
from fastapi import FastAPI, HTTPException, Body, Query, UploadFile, File, Form
from fastapi.responses import FileResponse
from pydantic import BaseModel
import logging
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
BT_VERSION_FILE = os.path.join(DATA_ROOT, "backtester_version.yaml")
BACKTESTER_SCRIPTS = [
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
    "backtester_core_speed3_veto_universe_2.py": {"plots": True, "time_range": True, "export_csv": True},
    "backtester_core_speed3_veto_universe.py": {"plots": True},
    "backtester_core_speed3.py": {"plots": True},
    "backtester_core_speed3_veto.py": {"plots": True},
}


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
    debug = bool(payload.get("debug", False))
    run_id = f"validation_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    run_dir = _validation_run_dir(run_id)
    os.makedirs(run_dir, exist_ok=True)
    os.makedirs(VALIDATION_REPORTS_DIR, exist_ok=True)

    inspect = _inspect_tv_path(tv_path)
    extract_script = os.path.join(BT_ROOT, "extract_bingx_window_from_tv.py")
    cmd = [
        "python",
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

    proc = subprocess.run(cmd, cwd=BT_ROOT, capture_output=True, text=True)
    stderr = proc.stderr or ""
    stdout = proc.stdout or ""
    if proc.returncode != 0:
        raise HTTPException(status_code=500, detail={"error": "extract failed", "stdout": stdout[-4000:], "stderr": stderr[-4000:]})

    symbol = inspect.get("symbol") or "symbol"
    symbol_safe = symbol.replace("-", "_")
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
        "status": {"extract_returncode": proc.returncode, "run_match": run_match, "debug": debug},
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
