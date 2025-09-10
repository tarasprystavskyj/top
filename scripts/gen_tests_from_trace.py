# читає calls.db, відновлює аргументи і генерує pytest-тести
import os, json, sqlite3, importlib, pathlib, pickle
from typing import Any
import pandas as pd; import numpy as np

ROOT = "cache/trace"
DB = os.path.join(ROOT, "calls.db")
OUT = "tests/generated"

def _load_obj(j: str):
    o = json.loads(j)
    if isinstance(o, dict) and o.get("__artifact__"):
        kind, path = o["kind"], o["path"]
        if kind=="parquet": return pd.read_parquet(path)
        if kind=="npz":     return np.load(path)["arr"]
        if kind=="pickle":
            with open(path,"rb") as f: return pickle.load(f)
    return o

def main():
    os.makedirs(OUT, exist_ok=True)
    con = sqlite3.connect(DB)
    rows = con.execute("SELECT module, qualname, args_json, kwargs_json FROM calls ORDER BY id DESC").fetchall()
    buckets = {}
    for module, qual, a, k in rows:
        buckets.setdefault((module, qual), []).append((a,k))
    for (module, qual), calls in buckets.items():
        mod = module
        fn_name = qual.split(".")[-1]
        safe = qual.replace(".","_").replace("<","_").replace(">","_")
        path = os.path.join(OUT, f"test_{safe}.py")
        with open(path, "w", encoding="utf-8") as f:
            f.write("import importlib, pickle, json\n")
            f.write("import pandas as pd, numpy as np\n")
            f.write(f"mod = importlib.import_module('{mod}')\n")
            f.write(f"fn = getattr(mod, '{fn_name}')\n\n")
            for i,(aj,kj) in enumerate(calls[:10]):  # до 10 кейсів на функцію
                f.write(f"def test_{safe}_{i}():\n")
                f.write(f"    args = {aj}\n")
                f.write(f"    kwargs = {kj}\n")
                f.write("    def materialize(x):\n")
                f.write("        import json, pandas as pd, numpy as np, pickle\n")
                f.write("        if isinstance(x, dict) and x.get('__artifact__'):\n")
                f.write("            if x['kind']=='parquet': return pd.read_parquet(x['path'])\n")
                f.write("            if x['kind']=='npz': import numpy as _np; return _np.load(x['path'])['arr']\n")
                f.write("            if x['kind']=='pickle': return pickle.load(open(x['path'],'rb'))\n")
                f.write("        if isinstance(x, list): return [materialize(v) for v in x]\n")
                f.write("        if isinstance(x, dict): return {k: materialize(v) for k,v in x.items()}\n")
                f.write("        return x\n")
                f.write("    args = materialize(args)\n")
                f.write("    kwargs = materialize(kwargs)\n")
                f.write("    fn(*args, **kwargs)\n\n")
    print(f"Generated tests into {OUT}")

if __name__ == '__main__':
    main()
