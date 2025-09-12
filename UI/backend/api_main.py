# FastAPI MVP backend
import os, json, uuid, subprocess, threading, queue, time, shutil, yaml, glob, itertools, copy
from typing import Any, Dict, Optional, List
from fastapi import FastAPI, HTTPException, Body, Query
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
DATA_ROOT = os.path.join(APP_ROOT, "data")
MAIN_CONFIG_DIR = os.path.join(DATA_ROOT, "configs")
OBW_CONFIG_DIR = os.path.abspath(os.path.join(APP_ROOT, "..", "obw_platform", "configs"))
CONFIG_DIRS = [MAIN_CONFIG_DIR, OBW_CONFIG_DIR]
RUNS_DIR = os.path.join(DATA_ROOT, "runs")
UNIVERSE_DIR = os.path.abspath(os.path.join(APP_ROOT, "..", "obw_platform", "universe"))
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
    "backtester_core_speed3_veto_universe_2.py": {"plots": True},
    "backtester_core_speed3_veto_universe.py": {"plots": True},
    "backtester_core_speed3.py": {"plots": True},
    "backtester_core_speed3_veto.py": {"plots": True},
}

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
    return df.to_dict(orient="records")


def _make_live_equity_png(base_dir):
    """Save viz_equity_vs_time.png into the live session dir, return path or None."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import matplotlib.dates as mdates

    session_db = os.path.join(base_dir, "session.sqlite")
    df = _session_equity_df(session_db)
    if df is None or df.empty:
        return None
    out_png = os.path.join(base_dir, "viz_equity_vs_time.png")
    plt.figure(figsize=(8, 4))
    plt.plot(df["ts"], df["equity"])
    ax = plt.gca()
    # show hours alongside the date for readability
    ax.xaxis.set_major_locator(mdates.HourLocator())
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%m-%d %H:%M'))
    plt.xticks(rotation=45)
    plt.title("Live Equity vs Time")
    plt.xlabel("Time")
    plt.ylabel("Equity")
    plt.tight_layout()
    plt.savefig(out_png)
    plt.close()
    return out_png

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

os.makedirs(MAIN_CONFIG_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)
os.makedirs(UNIVERSE_DIR, exist_ok=True)

job_q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
jobs: Dict[str, Dict[str, Any]] = {}
lock = threading.Lock()

def worker():
    while True:
        job = job_q.get()
        if job is None: break
        jid = job["job_id"]
        with lock:
            jobs[jid]["status"] = "running"
        try:
            if job["kind"] == "backtest":
                run_backtest(job)
            elif job["kind"] == "grid":
                run_grid(job)
            with lock:
                jobs[jid]["status"] = "done"
        except Exception as e:
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
):
    """Build a command line for the selected backtester script.

    Only include CLI flags that the target backtester supports.  This keeps
    older implementations (e.g. speed2) from failing with "unrecognized"
    arguments when newer flags like ``--plots`` are present.
    """
    # run inside obw_platform so relative paths in configs resolve correctly
    bt_script = script or load_backtester_version()
    cmd = [
        "python3",
        bt_script,
        "--cfg",
        cfg_path,
        "--limit-bars",
        str(limit_bars),
    ]

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
    repo_root = os.path.abspath(os.path.join(APP_ROOT, ".."))
    bt_root = os.path.join(repo_root, "obw_platform")
    for fn in ("summary.csv", "trades.csv"):
        try:
            os.remove(os.path.join(bt_root, fn))
        except FileNotFoundError:
            pass
    bt_script = meta.get("backtester") or load_backtester_version()
    cmd = cmd_backtester(cfg_path, meta["limit_bars"], meta.get("cache_db"), out_dir, bt_script)
    with open(logs, "w") as lf:
        p = subprocess.Popen(cmd, cwd=bt_root, stdout=lf, stderr=lf)
        p.wait()
    if p.returncode != 0:
        raise RuntimeError(f"backtester failed with code {p.returncode}")
    for fn in ("summary.csv", "trades.csv"):
        srcp = os.path.join(bt_root, fn)
        dstp = os.path.join(out_dir, fn)
        if os.path.exists(srcp):
            shutil.copyfile(srcp, dstp)
    save_backtester_version(bt_script)
    # Generate extra visualization plots if possible
    if _viz_plot is not None:
        try:
            _viz_plot(
                trades_csv=os.path.join(out_dir, "trades.csv"),
                summary_csv=os.path.join(out_dir, "summary.csv"),
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
        cmd = cmd_backtester(cfg_path, req.get("limit_bars", 5000), req.get("cache_db"))
        with open(logs, "w") as lf:
            p = subprocess.Popen(
                cmd,
                cwd=os.path.join(os.path.abspath(os.path.join(APP_ROOT, "..")), "obw_platform"),
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
    jid = str(uuid.uuid4())
    jobs[jid] = {"status":"queued","meta": req.model_dump(),"kind":"backtest"}
    out_dir = os.path.join(RUNS_DIR, jid); os.makedirs(out_dir, exist_ok=True)
    meta = {
        "cfg_name": req.cfg_name,
        "limit_bars": req.limit_bars,
        "started_at": time.time(),
        "backtester": req.backtester or load_backtester_version(),
    }
    with open(os.path.join(out_dir, "meta.json"), "w") as f:
        json.dump(meta, f)
    job_q.put({"job_id": jid, "meta": req.model_dump(), "kind":"backtest"})
    return {"job_id": jid}

@app.get("/api/jobs/{job_id}/status")
def status(job_id: str):
    j = jobs.get(job_id)
    if not j: raise HTTPException(404,"job not found")
    return {"status": j["status"], "message": j.get("message")}

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
    return {"summary": summary, "trades": trades, "artifacts": arts}

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
    jobs[jid] = {"status":"queued","meta":{"req": req.model_dump()}, "kind":"grid"}
    job_q.put({"job_id": jid, "meta":{"req": req.model_dump()}, "kind":"grid"})
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
        _make_live_equity_png(base)
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
    backtest = {"artifacts": {}, "summary": None}
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
        except Exception:
            log.exception("live_result %s: failed to parse trades.csv", name)

    if cfg_path and os.path.exists(cache_db):
        repo_root = os.path.abspath(os.path.join(APP_ROOT, ".."))
        bt_root = os.path.join(repo_root, "obw_platform")
        # Ensure previous outputs from obw_platform don't bleed into results
        for fn in ("summary.csv", "trades.csv"):
            try:
                os.remove(os.path.join(bt_root, fn))
            except FileNotFoundError:
                pass
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
        )
        with open(logs, "w") as lf:
            p = subprocess.Popen(cmd, cwd=bt_root, stdout=lf, stderr=lf)
            p.wait()
        if p.returncode == 0:
            # Copy summary/trades so we can post-process them
            src_summary = os.path.join(bt_root, "summary.csv")
            src_trades = os.path.join(bt_root, "trades.csv")
            dst_summary = os.path.join(base, "bt_summary.csv")
            dst_trades = os.path.join(base, "bt_trades.csv")
            if os.path.exists(src_summary):
                shutil.copyfile(src_summary, dst_summary)
            if os.path.exists(src_trades):
                shutil.copyfile(src_trades, dst_trades)
            if _viz_plot and os.path.exists(dst_trades):
                try:
                    _viz_plot(
                        trades_csv=dst_trades,
                        summary_csv=dst_summary if os.path.exists(dst_summary) else None,
                        show=False,
                        save_dir=bt_plots,
                        file_prefix="bt_viz",
                    )
                except Exception:
                    log.exception(
                        "live_result %s: failed to generate backtest viz plots", name
                    )
            # Collect backtest artifacts
            bt_arts = {}
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
            bt_summary = None
            if os.path.exists(dst_summary):
                try:
                    import csv
                    with open(dst_summary) as f:
                        rows = list(csv.DictReader(f))
                        if rows:
                            bt_summary = rows[0]
                except Exception:
                    # If parsing fails we simply keep the summary as ``None``
                    # so the caller knows no usable data was produced.
                    log.exception("live_result %s: failed to parse bt_summary", name)
            bt_trades = []
            if os.path.exists(dst_trades):
                try:
                    import csv
                    with open(dst_trades) as f:
                        bt_trades = list(csv.DictReader(f))
                except Exception:
                    log.exception("live_result %s: failed to parse bt_trades", name)
                    bt_trades = []
            bt_logs = None
            if os.path.exists(logs):
                bt_logs = f"/api/live_results/{name}/files/{os.path.basename(logs)}"
            backtest = {
                "artifacts": bt_arts,
                "summary": bt_summary,
                "trades": bt_trades,
                "logs": bt_logs,
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


@app.get("/api/live_results/{name}/files/{filename:path}")
def live_result_file(name: str, filename: str):
    base = os.path.join(LIVE_RESULTS_DIR, name)
    p = os.path.join(base, filename)
    if not os.path.isfile(p):
        raise HTTPException(404, "not found")
    return FileResponse(p)
