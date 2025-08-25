#!/usr/bin/env bash
# УВАГА - якщо не працює - підготувати файл до запуску
#sed -i 's/\r$//' grid_search_rays.sh   # прибрати CRLF, якщо файл редагувався у Windows
#bash -n grid_search_rays.sh            # перевірити синтаксис
#chmod +x grid_search_rays.sh
#./grid_search_rays.sh   

set -e; set -u; if (set -o 2>/dev/null | grep -q pipefail); then set -o pipefail; fi

# базовий конфіг: можна передати своїй як $1
CFG="${1:-configs/cfg_avaai_base.yaml}"
LIMIT=1440

python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix rays_tp_1440 --limit-bars "$LIMIT" --param tp --range 3.2:3.8:0.1
CFG="rays_tp_1440_rays_best.yaml"

python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix rays_sl_1440 --limit-bars "$LIMIT" --param sl --range 1.00:1.15:0.025
CFG="rays_sl_1440_rays_best.yaml"

python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix rays_minmom_1440 --limit-bars "$LIMIT" --param min-mom --range 0.019:0.022:0.001
CFG="rays_minmom_1440_rays_best.yaml"

python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix rays_minatr_1440 --limit-bars "$LIMIT" --param min-atr --values 0,0.0005,0.001,0.0015
CFG="rays_minatr_1440_rays_best.yaml"

python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix rays_pos_1440 --limit-bars "$LIMIT" --param position_notional --range 70:100:5
CFG="rays_pos_1440_rays_best.yaml"

python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix rays_topn_1440 --limit-bars "$LIMIT" --param top-n --values 10,12,14
CFG="rays_topn_1440_rays_best.yaml"

echo "FINAL CFG => $CFG"

# (optional) одразу збудувати графіки фінального YAML:
python3 backtester_core_speed3_veto.py --cfg "$CFG" --limit-bars "$LIMIT" --plots plots_1440
