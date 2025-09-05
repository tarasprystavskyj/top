#!/usr/bin/env python3
"""
tuner_plan_avaai.py — план тюнингу для cfg_avaai_t5m5000_3.yaml

Що робить:
- Крок 1 (RAYS): одиночні прогони по ключових параметрах (ADX, ATR-фільтр, сумарний моментум, top-n).
- Крок 2 (GRID, міні-пари): сітка невеликих пар взаємодій (ATR x MOM, TP x SL, top-n x ATR).
- Дизайн сумісний з вашим grid_runner_ultrafast_3.py.
- За замовчуванням просто друкує команди. Додайте --run, щоб виконати.

Приклади:
  python3 tuner_plan_avaai.py --cfg configs/cfg_avaai_t5m5000_3.yaml \
    --limit-bars 5000 --symbols universe_v5_avaai_5m_5000.txt \
    --plots plots_auto --driver backtester_core_speed3_veto_universe_2.py --prefix t5k_auto --run

Автор: auto-generated
"""
import argparse
import subprocess
import shlex
from typing import List

def build_cmd(base: str, **kw) -> str:
    parts = [base]
    for k, v in kw.items():
        if v is None: 
            continue
        if isinstance(v, bool):
            if v:
                parts.append(f"--{k}")
        else:
            parts.append(f"--{k.replace('_','-')} {shlex.quote(str(v))}")
    return " ".join(parts)

def rays(cfg: str, limit_bars: int, symbols: str, plots: str, driver: str, prefix: str) -> List[str]:
    base = "python3 grid_runner_ultrafast_3.py --mode rays"
    common = {
        "cfg": cfg,
        "limit-bars": limit_bars,
        "symbols-file": symbols,
        "plots": plots,
        "driver": driver,
    }
    cmds = []

    # 1) ADX threshold (навколо 20-28, попередньо 25 виглядав добре)
    cmds.append(build_cmd(base, **common,
        **{"out-prefix": f"{prefix}_rays_adx",
           "param": "strategy_params.adx_threshold",
           "values": "18,20,22,24,26,28"}))

    # 2) ATR-фільтр (навколо 0.02)
    cmds.append(build_cmd(base, **common,
        **{"out-prefix": f"{prefix}_rays_atr",
           "param": "min_atr_ratio",
           "values": "0.018,0.020,0.022,0.024,0.026"}))

    # 3) Сумарний моментум
    cmds.append(build_cmd(base, **common,
        **{"out-prefix": f"{prefix}_rays_mom",
           "param": "min_momentum_sum",
           "values": "0.018,0.020,0.022,0.024,0.026"}))

    # 4) TOP-N (у нас allow ~7; все ж проганяємо невелике вікно)
    cmds.append(build_cmd(base, **common,
        **{"out-prefix": f"{prefix}_rays_topn",
           "param": "top-n",
           "values": "6,7,8,10,12"}))

    return cmds

def grids(cfg: str, limit_bars: int, symbols: str, plots: str, driver: str, prefix: str) -> List[str]:
    base = "python3 grid_runner_ultrafast_3.py --mode grid"
    common = {
        "cfg": cfg,
        "limit-bars": limit_bars,
        "symbols-file": symbols,
        "plots": plots,
        "driver": driver,
    }
    cmds = []
    # Невеликі 3x3/4x3 гріди (обережно з перебором)
    # A) ATR x MOM
    cmds.append(build_cmd(base, **common,
        **{"out-prefix": f"{prefix}_grid_atr_mom",
           "min-atr-range": "0.018:0.026:0.004",
           "min-mom-range": "0.018:0.026:0.004"}))

    # B) TOPN x ATR
    cmds.append(build_cmd(base, **common,
        **{"out-prefix": f"{prefix}_grid_topn_atr",
           "top-n-range": "6:12:2",
           "min-atr-range": "0.018:0.026:0.004"}))

    # C) (опційно) TP/SL, лише якщо ваш runner мапить їх у ATR-множники усередині стратегії.
    # Розкоментуйте, якщо підтримується:
    # cmds.append(build_cmd(base, **common,
    #     **{"out-prefix": f"{prefix}_grid_tp_sl",
    #        "tp-range": "3.2:4.2:0.2",
    #        "sl-range": "0.96:1.12:0.04"}))

    return cmds

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cfg", required=True)
    ap.add_argument("--limit-bars", type=int, default=5000)
    ap.add_argument("--symbols", "--symbols-file", dest="symbols", required=True)
    ap.add_argument("--plots", default="plots_auto")
    ap.add_argument("--driver", default="backtester_core_speed3_veto_universe_2.py")
    ap.add_argument("--prefix", default="t5k_auto")
    ap.add_argument("--run", action="store_true", help="виконати команди (інакше лише друк)")
    args = ap.parse_args()

    cmds = []
    cmds += rays(args.cfg, args.limit_bars, args.symbols, args.plots, args.driver, args.prefix)
    cmds += grids(args.cfg, args.limit_bars, args.symbols, args.plots, args.driver, args.prefix)

    print("# ==== TUNER PLAN (commands) ====")
    for c in cmds:
        print(c)

    if args.run:
        for c in cmds:
            print(f"\n[run] {c}")
            rc = subprocess.call(c, shell=True)
            if rc != 0:
                print(f"[warn] command exited with code {rc}: {c}")

if __name__ == "__main__":
    main()
