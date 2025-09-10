# Python 3.8
import os, io, re, sys, json, time, uuid, zlib, hashlib, pickle, queue, atexit, inspect, functools, threading, traceback, asyncio, sqlite3, platform, subprocess
from typing import Any, Mapping
from datetime import datetime

try:
    import pandas as pd
    import numpy as np
except Exception:
    pd = None; np = None

DEFAULT_ROOT = os.environ.get("TRACE_ROOT", "cache/trace")

def _ensure_dirs(root=DEFAULT_ROOT):
    os.makedirs(root, exist_ok=True)
    os.makedirs(os.path.join(root, "artifacts"), exist_ok=True)
    return root

def _sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()

def _git_rev():
    try:
        return subprocess.check_output(["git","rev-parse","HEAD"], stderr=subprocess.DEVNULL).decode().strip()
    except Exception:
        return None

class TraceStore:
    def __init__(self, root=DEFAULT_ROOT, sqlite_name="calls.db", max_arg_mb=8, redact_keys=None, sample_rate=1.0):
        self.root = _ensure_dirs(root)
        self.art_dir = os.path.join(self.root, "artifacts")
        self.db_path = os.path.join(self.root, sqlite_name)
        self.max_arg_bytes = int(max_arg_mb * 1024 * 1024)
        self.redact_keys = set(redact_keys or [])
        self.sample_rate = float(sample_rate)
        self.q = queue.Queue(maxsize=10000)
        self._stop = threading.Event()
        self._th = threading.Thread(target=self._writer, daemon=True)
        self._init_db()
        self._th.start()
        atexit.register(self.close)

    def _init_db(self):
        con = sqlite3.connect(self.db_path)
        con.execute("PRAGMA journal_mode=WAL;")
        con.execute("""
        CREATE TABLE IF NOT EXISTS calls (
          id INTEGER PRIMARY KEY,
          call_id TEXT,
          ts TEXT,
          duration_ms REAL,
          module TEXT,
          qualname TEXT,
          git_rev TEXT,
          py_version TEXT,
          platform TEXT,
          pid INTEGER,
          tid INTEGER,
          cfg_hash TEXT,
          args_json TEXT,
          kwargs_json TEXT,
          ret_json TEXT,
          exc TEXT
        );
        """)
        con.commit(); con.close()

    def close(self):
        self._stop.set()
        try:
            self.q.put_nowait(None)
        except Exception:
            pass
        if self._th.is_alive():
            self._th.join(timeout=1.5)

    def _writer(self):
        con = sqlite3.connect(self.db_path)
        while not self._stop.is_set():
            item = self.q.get()
            if item is None:
                break
            con.execute("""INSERT INTO calls
                (call_id, ts, duration_ms, module, qualname, git_rev, py_version, platform, pid, tid, cfg_hash,
                 args_json, kwargs_json, ret_json, exc)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", item)
            con.commit()
        con.close()

    # ---------------- serialization -----------------

    def _redact(self, obj: Any):
        try:
            if isinstance(obj, Mapping):
                return {k: ("***" if k in self.redact_keys else self._redact(v)) for k, v in obj.items()}
            if isinstance(obj, (list, tuple)):
                return [self._redact(x) for x in obj]
            return obj
        except Exception:
            return repr(obj)

    def _artifact_path(self, sha: str, ext: str) -> str:
        return os.path.join(self.art_dir, f"{sha}.{ext}")

    def _save_artifact(self, obj: Any):
        # pandas
        if pd is not None and isinstance(obj, pd.DataFrame):
            b = obj.to_parquet(index=True)
            # NOTE: pandas returns None for to_parquet to file; so write buffer:
        # fall back: write to file directly
        try:
            if pd is not None and isinstance(obj, pd.DataFrame):
                sha = _sha256_bytes(pickle.dumps(("pd", obj.head(3).to_dict(), obj.shape)))  # quick sha seed
                path = self._artifact_path(sha, "parquet")
                if not os.path.exists(path):
                    obj.to_parquet(path, index=True)
                return {"__artifact__": True, "kind": "parquet", "sha": sha, "path": path, "meta": {"rows": int(obj.shape[0]), "cols": int(obj.shape[1])}}
        except Exception:
            pass

        # numpy
        try:
            if np is not None and isinstance(obj, np.ndarray):
                sha = _sha256_bytes(obj.tobytes())
                path = self._artifact_path(sha, "npz")
                if not os.path.exists(path):
                    np.savez_compressed(path, arr=obj)
                return {"__artifact__": True, "kind": "npz", "sha": sha, "path": path, "meta": {"shape": obj.shape, "dtype": str(obj.dtype)}}
        except Exception:
            pass

        # generic pickle
        try:
            raw = pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL)
            sha = _sha256_bytes(raw)
            path = self._artifact_path(sha, "pkl")
            if not os.path.exists(path):
                with open(path,"wb") as f: f.write(raw)
            return {"__artifact__": True, "kind": "pickle", "sha": sha, "path": path}
        except Exception:
            return {"summary": repr(obj)[:256]}

    def _safe_json(self, obj: Any):
        obj = self._redact(obj)
        try:
            s = json.dumps(obj, default=str)
            if len(s.encode("utf-8")) <= self.max_arg_bytes:
                return s
        except Exception:
            pass
        # too big or not JSON-serializable -> artifact
        art = self._save_artifact(obj)
        return json.dumps(art)

    # ---------------- public API -----------------

    def log_call(self, *, module: str, qualname: str, start_ns: int, cfg_hash: str, args: Any, kwargs: Any, ret: Any=None, exc: str=None):
        if self.sample_rate < 1.0:
            import random
            if random.random() > self.sample_rate:
                return
        ts = datetime.utcnow().isoformat()
        dur_ms = (time.time_ns() - start_ns) / 1e6
        item = (
            str(uuid.uuid4()), ts, dur_ms, module, qualname, _git_rev(), sys.version.split()[0],
            f"{platform.system()}-{platform.release()}", os.getpid(), threading.get_ident(),
            cfg_hash,
            self._safe_json(args),
            self._safe_json(kwargs),
            self._safe_json(ret) if exc is None else None,
            exc
        )
        try:
            self.q.put_nowait(item)
        except queue.Full:
            # drop silently to avoid blocking hot path
            pass

# global singleton (lazy)
_TRACE = None
def get_store(cfg: Mapping=None) -> TraceStore:
    global _TRACE
    if _TRACE is None:
        tc = (cfg or {}).get("trace_calls", {})
        _TRACE = TraceStore(
            root=tc.get("root_dir", DEFAULT_ROOT),
            sqlite_name=tc.get("sqlite_name", "calls.db"),
            max_arg_mb=tc.get("max_arg_size_mb", 8),
            redact_keys=tc.get("redact_keys", ["api_key","secret","password"]),
            sample_rate=tc.get("sample_rate", 1.0),
        )
    return _TRACE

def trace_call(cfg: Mapping=None):
    store = get_store(cfg)
    def _decor(fn):
        is_coro = asyncio.iscoroutinefunction(fn)
        qual = f"{fn.__module__}.{fn.__qualname__}"

        @functools.wraps(fn)
        def _wrap(*args, **kwargs):
            start = time.time_ns()
            try:
                ret = fn(*args, **kwargs)
                store.log_call(module=fn.__module__, qualname=qual, start_ns=start,
                               cfg_hash=str(hash(str(cfg))), args=args, kwargs=kwargs, ret=ret, exc=None)
                return ret
            except Exception:
                store.log_call(module=fn.__module__, qualname=qual, start_ns=start,
                               cfg_hash=str(hash(str(cfg))), args=args, kwargs=kwargs, ret=None, exc=traceback.format_exc())
                raise

        @functools.wraps(fn)
        async def _awrap(*args, **kwargs):
            start = time.time_ns()
            try:
                ret = await fn(*args, **kwargs)
                store.log_call(module=fn.__module__, qualname=qual, start_ns=start,
                               cfg_hash=str(hash(str(cfg))), args=args, kwargs=kwargs, ret=ret, exc=None)
                return ret
            except Exception:
                store.log_call(module=fn.__module__, qualname=qual, start_ns=start,
                               cfg_hash=str(hash(str(cfg))), args=args, kwargs=kwargs, ret=None, exc=traceback.format_exc())
                raise

        return _awrap if is_coro else _wrap
    return _decor

def auto_instrument_module(module, cfg: Mapping=None, include=None, exclude=None):
    import types, inspect
    for name, obj in inspect.getmembers(module):
        if inspect.isfunction(obj) and obj.__module__ == module.__name__:
            if include and name not in include: continue
            if exclude and name in exclude: continue
            setattr(module, name, trace_call(cfg)(obj))
        elif inspect.isclass(obj):
            for mname, mobj in inspect.getmembers(obj, inspect.isfunction):
                if include and f"{name}.{mname}" not in include: continue
                if exclude and f"{name}.{mname}" in exclude: continue
                setattr(obj, mname, trace_call(cfg)(mobj))
