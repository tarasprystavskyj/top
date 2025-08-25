#!/usr/bin/env bash
# УВАГА - якщо не працює - підготувати файл до запуску
#sed -i 's/\r$//' grid_search_rays.sh   # прибрати CRLF, якщо файл редагувався у Windows
#bash -n grid_search_rays.sh            # перевірити синтаксис
#chmod +x grid_search_rays.sh
#./grid_search_rays.sh   
set -e; set -u; if (set -o 2>/dev/null | grep -q pipefail); then set -o pipefail; fi

# базовий конфіг: можна передати своїй як $1
CFG="${1:-configs/rays_pos_1440_rays_best.yaml}"
LIMIT=1440
python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix df_tp_1440 --limit-bars $LIMIT --param tp --range 3.30:3.40:0.02; CFG="df_tp_1440_rays_best.yaml"
python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix df_sl_1440 --limit-bars $LIMIT --param sl --range 1.08:1.14:0.02; CFG="df_sl_1440_rays_best.yaml"
python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix df_minmom_1440 --limit-bars $LIMIT --param min-mom --values 0.020,0.021,0.022; CFG="df_minmom_1440_rays_best.yaml"
python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix df_minatr_1440 --limit-bars $LIMIT --param min-atr --values 0.0006,0.0008,0.0010,0.0012; CFG="df_minatr_1440_rays_best.yaml"
python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix df_pos_1440 --limit-bars $LIMIT --param position_notional --range 80:110:5; CFG="df_pos_1440_rays_best.yaml"
python3 grid_runner_ultrafast_2.py --mode rays --cfg "$CFG" --out-prefix df_topn_1440 --limit-bars $LIMIT --param top-n --values 10,12,14; CFG="df_topn_1440_rays_best.yaml"


echo "FINAL CFG => $CFG"
# (optional) одразу збудувати графіки фінального YAML:
python3 backtester_core_speed3_veto.py --cfg "$CFG" --limit-bars "$LIMIT" --plots plots_1440
