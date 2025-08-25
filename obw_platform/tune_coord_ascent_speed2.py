
#!/usr/bin/env python3
import argparse, yaml, copy, subprocess, sys, time, pathlib, pandas as pd

ROOT = pathlib.Path(__file__).parent
BT = ROOT / "backtester_core_speed2.py"

def _to_yamlable(x):
    import numbers
    if isinstance(x, dict):
        return {k: _to_yamlable(v) for k,v in x.items()}
    elif isinstance(x, (list, tuple)):
        return [ _to_yamlable(v) for v in x ]
    elif isinstance(x, pathlib.Path):
        return str(x)
    elif hasattr(x, 'item'):
        try:
            return _to_yamlable(x.item())
        except Exception:
            return str(x)
    elif isinstance(x, numbers.Number):
        return float(x)
    else:
        return x

def run_bt(cfg_obj, limit_bars=500, timeout=15):
    tmp = ROOT / f"_tmp_{int(time.time()*1000)%100000}.yaml"
    yaml.safe_dump(_to_yamlable(cfg_obj), open(tmp,"w"), sort_keys=False)
    cmd = [sys.executable, str(BT), "--cfg", str(tmp), "--limit-bars", str(limit_bars)]
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate(timeout=timeout)
    s = pd.read_csv(ROOT/"summary.csv")
    return s

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit", type=int, default=500)
    ap.add_argument("--passes", type=int, default=1)
    args = ap.parse_args()

    base = yaml.safe_load(open(args.cfg).read())
    base["open_on_heat"] = False
    base["session"]["open_every_bar"] = True

    grids = {
        "side": ["BOTH","LONG","SHORT"],
        "min_atr_ratio": [0.0010, 0.0012, 0.0015],
        "min_momentum_sum": [0.00, 0.015, 0.02, 0.03],
        "tp_atr_mult": [2.2, 2.6, 3.0, 3.4],
        "sl_atr_mult": [0.9, 1.0, 1.1],
        "session.open_hour_kyiv": [0,1,2],
    }
    order = ["side","min_atr_ratio","min_momentum_sum","tp_atr_mult","sl_atr_mult","session.open_hour_kyiv"]

    best = base
    hist_rows = []
    for _ in range(args.passes):
        for key in order:
            rows = []
            for val in grids[key]:
                cfg = copy.deepcopy(best)
                if key.startswith("session."):
                    cfg["session"][key.split('.',1)[1]] = val
                elif key in {"min_atr_ratio","min_momentum_sum"}:
                    cfg[key] = val
                else:
                    cfg["strategy_params"][key] = val
                s = run_bt(cfg, limit_bars=args.limit)
                rec = s.to_dict(orient="records")[0]
                rec["param"] = key; rec["value"] = val
                rows.append(rec)
            df = pd.DataFrame(rows).sort_values(["equity_end","trades","profit_factor"], ascending=[False, False, False])
            best_val = df.iloc[0]["value"]
            if key.startswith("session."):
                best["session"][key.split('.',1)[1]] = best_val
            elif key in {"min_atr_ratio","min_momentum_sum"}:
                best[key] = best_val
            else:
                best["strategy_params"][key] = best_val
            df["chosen"] = df["value"]==best_val
            hist_rows.append(df)

    hist = pd.concat(hist_rows, ignore_index=True)
    hist.to_csv(ROOT/"coord_history_speed2.csv", index=False)
    import yaml as _y
    (ROOT/"tuned_coord_speed2.yaml").write_text(_y.safe_dump(best, sort_keys=False))
    print("Saved tuned YAML:", ROOT/"tuned_coord_speed2.yaml")

if __name__ == "__main__":
    main()
