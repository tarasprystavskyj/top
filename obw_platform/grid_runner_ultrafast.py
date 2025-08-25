#!/usr/bin/env python3
import argparse, yaml, copy, subprocess, sys, re, time, pathlib, csv

ROOT = pathlib.Path(__file__).parent
BT = ROOT / "backtester_core_speed3_veto.py"

alias_param_map = {
    'min_momentum_sum': 'min-mom',
    'min_atr_ratio': 'min-atr',
    'position_notional': 'position_notional',
    'tp_atr_mult': 'tp',
    'sl_atr_mult': 'sl',
    'top_n': 'top-n',
}

def _parse_range(s):
    parts = [p.strip() for p in str(s).split(":") if p.strip()!='']
    if len(parts) not in (2,3):
        raise SystemExit("Range must be start:end[:step]")
    start = float(parts[0]); end = float(parts[1])
    step = float(parts[2]) if len(parts)==3 else (end-start)/10.0 if end!=start else 1.0
    if step == 0:
        raise SystemExit("Step in range cannot be 0")
    if (end - start) * step < 0:
        step = -step
    vals = []
    i = 0; cur = start
    eps = abs(step) * 1e-6
    while (cur <= end + eps) if step>0 else (cur >= end - eps):
        vals.append(cur)
        i += 1
        cur = start + i*step
    return [str(v) for v in vals]

def _parse_int_range(s):
    parts = [p.strip() for p in str(s).split(":") if p.strip()!='']
    if len(parts) not in (2,3):
        raise SystemExit("int range must be start:end[:step]")
    start = int(float(parts[0])); end = int(float(parts[1])); step = int(float(parts[2])) if len(parts)==3 else max(1, int(abs(end-start)/10) or 1)
    if step == 0: step = 1
    if (end - start) * step < 0: step = -step
    vals = []
    i=0; cur=start
    while (cur <= end) if step>0 else (cur >= end):
        vals.append(str(int(cur)))
        i+=1; cur=start+i*step
    return vals

def _vals_csv(s):
    return [x.strip() for x in s.split(',') if x.strip()!='']

def _run_once(cfg_obj, limit_bars=500, timeout=60):
    tmp = ROOT / f"_tmp_{int(time.time()*1000)%100000}.yaml"
    yaml.safe_dump(cfg_obj, open(tmp,"w"), sort_keys=False)
    cmd = [sys.executable, str(BT), "--cfg", str(tmp), "--limit-bars", str(limit_bars)]
    p = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    out, _ = p.communicate(timeout=timeout)
    m_eq = re.search(r"equity_end=([0-9]+\.[0-9]+)", out)
    m_pf = re.search(r"pf=([0-9]+\.[0-9]+)", out)
    m_tr = re.search(r"trades=([0-9]+)", out)
    m_dd = re.search(r"max_dd=(-?[0-9]+\.[0-9]+)", out)
    m_mo = re.search(r"mono=(-?[0-9]+\.[0-9]+)", out)
    m_el = re.search(r"elapsed_sec=([0-9]+\.[0-9]+)", out)
    eq = float(m_eq.group(1)) if m_eq else None
    pf = float(m_pf.group(1)) if m_pf else None
    tr = int(m_tr.group(1)) if m_tr else None
    dd = float(m_dd.group(1)) if m_dd else None
    mo = float(m_mo.group(1)) if m_mo else None
    el = float(m_el.group(1)) if m_el else None
    return eq, pf, tr, dd, mo, el, out

def _apply_param(cfg, key, val):
    if key == "side":
        cfg.setdefault("strategy_params",{})["side"] = str(val)
    elif key == "tp":
        cfg.setdefault("strategy_params",{})["tp_atr_mult"] = float(val)
    elif key == "sl":
        cfg.setdefault("strategy_params",{})["sl_atr_mult"] = float(val)
    elif key == "min-atr":
        cfg["min_atr_ratio"] = float(val)
    elif key == "min-mom":
        cfg["min_momentum_sum"] = float(val)
    elif key == "position_notional":
        cfg.setdefault("portfolio", {})["position_notional"] = float(val)
    elif key == "top-n":
        cfg.setdefault("strategy_params",{})["top_n"] = int(float(val))
    else:
        raise ValueError("Unknown key: "+key)

def rays(args, base):
    prefix = args.out_prefix or 'rays'
    if args.param != "min-atr":
        base["min_atr_ratio"] = 0.0
    if args.param != "min-mom":
        base["min_momentum_sum"] = 0.0
    base["open_on_heat"] = False
    base.setdefault("session",{}).setdefault("open_every_bar", True)

    args.param = alias_param_map.get(args.param, args.param)

    if args.range:
        values = _parse_range(args.range)
    else:
        values = _vals_csv(args.values)

    print(f"[rays] param={args.param} values={values}  (others disabled to 0 where applicable)")
    best = {"eq": None, "val": None, "pf": None, "tr": None, "dd": None, "mo": None, "el": None}
    rows = []

    for v in values:
        cfg = copy.deepcopy(base)
        _apply_param(cfg, args.param, v)
        eq, pf, tr, dd, mo, el, out = _run_once(cfg, limit_bars=args.limit_bars, timeout=args.timeout)
        print(f"  {args.param}={v:<8} -> equity_end={eq}  pf={pf}  max_dd={dd}  mono={mo}  trades={tr}  elapsed={el}s")
        rows.append({"param": args.param, "value": v, "equity_end": eq, "profit_factor": pf, "trades": tr, "max_dd": dd, "monotonicity": mo, "elapsed_sec": el})
        if eq is None: continue
        if best["eq"] is None or eq > best["eq"]:
            best = {"eq": eq, "val": v, "pf": pf, "tr": tr, "dd": dd, "mo": mo, "el": el}

    out_csv = ROOT / f"{prefix}_rays_results.csv"
    with open(out_csv, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=["param","value","equity_end","profit_factor","trades","max_dd","monotonicity","elapsed_sec"])
        wr.writeheader(); wr.writerows(rows)

    best_cfg = copy.deepcopy(base)
    if best['val'] is not None:
        _apply_param(best_cfg, args.param, best['val'])
    out_yaml = ROOT / f"{prefix}_rays_best.yaml"
    out_yaml.write_text(yaml.safe_dump(best_cfg, sort_keys=False))

    print("=== RAYS RESULT ===")
    print(f"best {args.param}={best['val']}  equity_end={best['eq']}  pf={best['pf']}  max_dd={best['dd']}  mono={best['mo']}  trades={best['tr']}  elapsed={best['el']}s")
    print(f"[saved] {out_csv}")
    print(f"[saved] {out_yaml}")

def grid(args, base):
    prefix = args.out_prefix or 'grid'
    base["open_on_heat"] = False
    base.setdefault("session",{}).setdefault("open_every_bar", True)

    def _vals(s): 
        return [x.strip() for x in s.split(",") if x.strip()!=""]

    lists = {}
    lists["side"] = _vals(args.side) if args.side else [base.get("strategy_params",{}).get("side","BOTH")]

    # top_n
    if args.top_n_range:
        lists["top-n"] = _parse_int_range(args.top_n_range)
    elif args.top_n:
        lists["top-n"] = _vals(args.top_n)
    else:
        lists["top-n"] = [str(base.get("strategy_params",{}).get("top_n", 8))]

    # numerics
    lists["tp"]  = _parse_range(args.tp_range)  if args.tp_range  else (_vals(args.tp)  if args.tp  else [str(base.get("strategy_params",{}).get("tp_atr_mult", 2.6))])
    lists["sl"]  = _parse_range(args.sl_range)  if args.sl_range  else (_vals(args.sl)  if args.sl  else [str(base.get("strategy_params",{}).get("sl_atr_mult", 1.0))])
    lists["min-atr"] = _parse_range(args.min_atr_range) if args.min_atr_range else (_vals(args.min_atr) if args.min_atr else [str(base.get("min_atr_ratio", 0.0))])
    lists["min-mom"] = _parse_range(args.min_mom_range) if args.min_mom_range else (_vals(args.min_mom) if args.min_mom else [str(base.get("min_momentum_sum", 0.0))])

    if args.position_notional_range:
        lists["position_notional"] = _parse_range(args.position_notional_range)
    elif args.position_notional:
        lists["position_notional"] = _vals(args.position_notional)
    else:
        pn_base = str(base.get("portfolio", {}).get("position_notional", 20.0))
        lists["position_notional"] = [pn_base]

    if args.param:
        p = alias_param_map.get(args.param, args.param)
        override_vals = _parse_range(args.range) if args.range else (_vals(args.values) if args.values else None)
        keymap = {"tp":"tp","sl":"sl","min-atr":"min-atr","min-mom":"min-mom","position_notional":"position_notional","top-n":"top-n"}
        if p in keymap and override_vals:
            lists[keymap[p]] = override_vals

    keys = ["side","top-n","min-atr","min-mom","tp","sl","position_notional"]
    import itertools
    combos = list(itertools.product(*[lists[k] for k in keys]))
    print(f"[grid] combos={len(combos)} over keys={keys}")
    best = {"eq": None, "vec": None, "pf": None, "tr": None, "dd": None, "mo": None, "el": None}
    rows = []

    for vec in combos:
        cfg = copy.deepcopy(base)
        for k, v in zip(keys, vec):
            _apply_param(cfg, k, v)
        eq, pf, tr, dd, mo, el, out = _run_once(cfg, limit_bars=args.limit_bars, timeout=args.timeout)
        print(f"  combo {dict(zip(keys, vec))} -> equity_end={eq} pf={pf} max_dd={dd} mono={mo} trades={tr} elapsed={el}s")
        rec = {k:v for k,v in zip(keys, vec)}
        rec.update({"equity_end": eq, "profit_factor": pf, "trades": tr, "max_dd": dd, "monotonicity": mo, "elapsed_sec": el})
        rows.append(rec)
        if eq is None: continue
        if best["eq"] is None or eq > best["eq"]:
            best = {"eq": eq, "vec": vec, "pf": pf, "tr": tr, "dd": dd, "mo": mo, "el": el}

    out_csv = ROOT / f"{prefix}_grid_results.csv"
    with open(out_csv, "w", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=keys+["equity_end","profit_factor","trades","max_dd","monotonicity","elapsed_sec"])
        wr.writeheader(); wr.writerows(rows)

    out_yaml = ROOT / f"{prefix}_grid_best.yaml"
    if best["vec"] is None:
        print("=== GRID RESULT ===\nno successful runs")
        print(f"[saved] {out_csv}")
        return

    best_cfg = copy.deepcopy(base)
    for k, v in zip(keys, best["vec"]):
        _apply_param(best_cfg, k, v)
    out_yaml.write_text(yaml.safe_dump(best_cfg, sort_keys=False))

    print("=== GRID RESULT ===")
    print("best:", dict(zip(keys, best["vec"])))
    print(f"equity_end={best['eq']} pf={best['pf']} max_dd={best['dd']} mono={best['mo']} trades={best['tr']} elapsed={best['el']}s")
    print(f"[saved] {out_csv}")
    print(f"[saved] {out_yaml}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["rays","grid"], required=True)
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=500)
    ap.add_argument("--timeout", type=int, default=60)
    ap.add_argument("--out-prefix", default="", help="Prefix for CSV/YAML outputs; default 'rays' or 'grid')")

    # Rays
    ap.add_argument("--param", help="one of: side, min-atr, min-mom, tp, sl, position_notional, top-n")
    ap.add_argument("--range", help="RAYS ONLY: numeric range start:end:step (e.g. 2.0:3.4:0.2)")
    ap.add_argument("--values", help="comma-separated values for --param")

    # Grid
    ap.add_argument("--side", help="BOTH,LONG,SHORT")
    ap.add_argument("--tp", help="e.g. 2.2,2.6,3.0")
    ap.add_argument("--sl", help="e.g. 0.9,1.0,1.1")
    ap.add_argument("--min-atr", help="e.g. 0,0.0010,0.0012")
    ap.add_argument("--min-mom", help="e.g. 0,0.01,0.015,0.02")
    ap.add_argument("--position_notional", help="e.g. 60,80,100")
    ap.add_argument("--top-n", help="e.g. 3,5,8,12")

    ap.add_argument("--tp-range", help="e.g. 2.0:3.4:0.2")
    ap.add_argument("--sl-range", help="e.g. 0.8:1.2:0.1")
    ap.add_argument("--min-atr-range", help="e.g. 0:0.002:0.0002")
    ap.add_argument("--min-mom-range", help="e.g. 0:0.03:0.005")
    ap.add_argument("--position_notional-range", help="e.g. 40:120:20")
    ap.add_argument("--top-n-range", help="integers start:end:step, e.g. 3:15:2")

    # Aliases (snake_case) for convenience
    ap.add_argument("--min_momentum_sum-range", dest="min_mom_range")
    ap.add_argument("--min_atr_ratio-range", dest="min_atr_range")

    args = ap.parse_args()
    base = yaml.safe_load(open(args.cfg).read())

    if args.mode == "rays":
        if not args.param or (not args.values and not args.range):
            raise SystemExit("--mode rays requires --param and (--values or --range)")
        rays(args, base)
    else:
        grid(args, base)

if __name__ == "__main__":
    main()
