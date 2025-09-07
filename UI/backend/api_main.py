# FastAPI MVP backend
import os, json, uuid, subprocess, threading, queue, time, shutil, yaml, glob, itertools, copy
from typing import Any, Dict, Optional, List
from fastapi import FastAPI, HTTPException, Body
from pydantic import BaseModel

APP_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DATA_ROOT = os.path.join(APP_ROOT, "data")
CONFIG_DIR = os.path.join(DATA_ROOT, "configs")
RUNS_DIR = os.path.join(DATA_ROOT, "runs")

os.makedirs(CONFIG_DIR, exist_ok=True)
os.makedirs(RUNS_DIR, exist_ok=True)

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

def cmd_backtester(cfg_path, limit_bars, cache_db=None):
    script = "backtester_core_speed3_veto_universe_2.py"
    cmd = ["python3", script, "--cfg", cfg_path, "--limit-bars", str(limit_bars)]
    if cache_db:
        os.environ["CACHE_DB_OVERRIDE"] = cache_db
    return cmd

def run_backtest(job):
    jid = job["job_id"]
    meta = job["meta"]
    out_dir = os.path.join(RUNS_DIR, jid); os.makedirs(out_dir, exist_ok=True)
    src = os.path.join(CONFIG_DIR, meta["cfg_name"])
    if not os.path.isfile(src): raise RuntimeError("Config not found")
    cfg_obj = yaml.safe_load(open(src,"r").read())
    merged = apply_overrides(cfg_obj, meta.get("override") or {})
    cfg_path = os.path.join(out_dir, "cfg_merged.yaml")
    with open(cfg_path,"w") as f: yaml.safe_dump(merged, f, sort_keys=False)
    logs = os.path.join(out_dir, "logs.txt")
    cmd = cmd_backtester(cfg_path, meta["limit_bars"], meta.get("cache_db"))
    with open(logs,"w") as lf:
        p = subprocess.Popen(cmd, cwd=os.path.abspath(os.path.join(APP_ROOT, "..")), stdout=lf, stderr=lf)
        p.wait()
    # copy summary/trades if found nearby
    for root, dirs, files in os.walk(os.path.abspath(os.path.join(APP_ROOT, ".."))):
        for fn in files:
            if fn in ("summary.csv","trades.csv"):
                srcp = os.path.join(root, fn)
                dstp = os.path.join(out_dir, fn)
                try: shutil.copyfile(srcp, dstp)
                except: pass

def run_grid(job):
    jid = job["job_id"]
    req = job["meta"]["req"]
    base_cfg = yaml.safe_load(open(os.path.join(CONFIG_DIR, req["cfg_name"]),"r").read())
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
        cmd = cmd_backtester(cfg_path, req.get("limit_bars",5000), req.get("cache_db"))
        with open(logs,"w") as lf:
            p = subprocess.Popen(cmd, cwd=os.path.abspath(os.path.join(APP_ROOT, "..")), stdout=lf, stderr=lf)
            p.wait()

app = FastAPI()

@app.get("/api/health")
def health(): return {"ok": True}

@app.get("/api/configs")
def configs():
    res = []
    for p in sorted(glob.glob(os.path.join(CONFIG_DIR,"*.yaml"))):
        st = os.stat(p)
        res.append({"name": os.path.basename(p), "path": p, "updated_at": st.st_mtime})
    return res

@app.get("/api/configs/{name}")
def config_get(name: str):
    p = os.path.join(CONFIG_DIR, name)
    if not os.path.isfile(p): raise HTTPException(404, "not found")
    txt = open(p,"r").read()
    try: parsed = yaml.safe_load(txt)
    except Exception as e: parsed = {"_error": str(e)}
    return {"name": name, "yaml_text": txt, "parsed": parsed, "schema": {"title":"Config"}}

@app.put("/api/configs/{name}")
def config_put(name: str, body: Dict[str, Any] = Body(...)):
    p = os.path.join(CONFIG_DIR, name)
    txt = body.get("yaml_text")
    if not isinstance(txt,str): raise HTTPException(400,"yaml_text must be string")
    try: yaml.safe_load(txt)
    except Exception as e: raise HTTPException(400, f"YAML error: {e}")
    open(p,"w").write(txt)
    return {"ok": True}

@app.post("/api/backtest")
def backtest(req: BacktestReq):
    jid = str(uuid.uuid4())
    jobs[jid] = {"status":"queued","meta": req.model_dump(),"kind":"backtest"}
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
    for fn in ("summary.csv","trades.csv","cfg_merged.yaml","logs.txt"):
        p = os.path.join(out_dir, fn)
        if os.path.exists(p): arts[fn]=p
    summary = {}
    if "summary.csv" in arts:
        import csv
        with open(arts["summary.csv"]) as f:
            rows = list(csv.DictReader(f))
            if rows: summary = rows[0]
    trades = []
    if "trades.csv" in arts:
        import csv
        with open(arts["trades.csv"]) as f:
            trades = list(csv.DictReader(f))[:500]
    return {"summary": summary, "trades": trades, "artifacts": arts}

@app.get("/api/runs")
def runs(limit: int = 50):
    items = []
    for d in sorted(os.listdir(RUNS_DIR), reverse=True)[:limit]:
        p = os.path.join(RUNS_DIR, d)
        if os.path.isdir(p):
            items.append({"job_id": d})
    return items

@app.post("/api/grid")
def grid(req: GridReq):
    jid = str(uuid.uuid4())
    jobs[jid] = {"status":"queued","meta":{"req": req.model_dump()}, "kind":"grid"}
    job_q.put({"job_id": jid, "meta":{"req": req.model_dump()}, "kind":"grid"})
    return {"job_id": jid}
