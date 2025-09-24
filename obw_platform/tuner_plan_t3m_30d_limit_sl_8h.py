# tuner_plan_t3m_30d_limit_sl_8h_v2.py
# План для cfg_t3m_30d_newbest_full_limit_sl.yaml (TF=3m, horizon≈30d / 14_400 bars)
# Ідея: швидкий “огляд” rays → грубий grid навколо best → полірування heat → мікро для length/volume.
# Усі "grid" списки трактуються як ДЕЛЬТИ навколо BEST; "fix" = лишити значення BEST.

def _seq(lo, hi, step):
    xs, x = [], float(lo)
    while x <= hi + 1e-12:
        xs.append(round(x, 10)); x += step 
    return xs

def _choices(*vals):
    return list(vals)

def default_plan(limit_bars=None):
    # ---------------- Phase A: 1-D RAYS (ширший первинний огляд) ----------------
    rays = [
        # TP/SL – ширше в обидва боки
        ("rays", {"strategy_params.tp_atr_mult": _seq(3.0, 5.2, 0.1)}),      # 23
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.28, 0.60, 0.01)}),    # 33

        # Entry-фільтри
        ("rays", {"strategy_params.min_atr_ratio": _seq(0.010, 0.024, 0.001)}),    # 15
        ("rays", {"strategy_params.min_momentum_sum": _seq(0.040, 0.120, 0.005)}), # 17

        # Heat-вихід
        ("rays", {"strategy_params.heat_exit_threshold": _seq(0.90, 0.99, 0.01)}),  # 10
        ("rays", {"strategy_params.heat_exit_min_rr": _choices(1.05,1.07,1.10,1.13,1.15,1.18,1.20,1.22,1.25)}),

        # Короткі параметри (щоб не вибухати комбінаціями)
        ("rays", {"strategy_params.length": _choices(9,10,11,12,13)}),
        ("rays", {"strategy_params.volume_length": _choices(16,20,24,28)}),
        ("rays", {"strategy_params.macd_filter": _choices(1)}),  # як у конфігу
    ]

    # ---------------- Phase B1: COARSE GRID (навколо best; багатовимірний) ----------------
    # Важливо: використати ДЕЛЬТИ в обидва боки від best
    grid_coarse = ("grid", {
        "strategy_params.tp_atr_mult":       [-0.3, 0.0, +0.3, +0.6],
        "strategy_params.sl_atr_mult":       [-0.03, 0.0, +0.03, +0.06],
        "strategy_params.min_atr_ratio":     [-0.002, 0.0, +0.002, +0.004],
        "strategy_params.min_momentum_sum":  [-0.01, 0.0, +0.01, +0.02],

        # фіксуємо решту
        "strategy_params.length":                 "fix",
        "strategy_params.volume_length":          "fix",
        "strategy_params.macd_filter":            "fix",
        "strategy_params.heat_exit_threshold":    "fix",
        "strategy_params.heat_exit_min_rr":       "fix",
    })

    # ---------------- Phase B2: HEAT POLISH (лише heat-параметри) ----------------
    grid_heat = ("grid", {
        "strategy_params.heat_exit_threshold": [-0.02, 0.0, +0.02],
        "strategy_params.heat_exit_min_rr":    [-0.05, 0.0, +0.05, +0.10],

        "strategy_params.tp_atr_mult":       "fix",
        "strategy_params.sl_atr_mult":       "fix",
        "strategy_params.min_atr_ratio":     "fix",
        "strategy_params.min_momentum_sum":  "fix",
        "strategy_params.length":            "fix",
        "strategy_params.volume_length":     "fix",
        "strategy_params.macd_filter":       "fix",
    })

    # ---------------- Phase C: MICRO (тонке локальне підлаштування) ----------------
    grid_micro = ("grid", {
        "strategy_params.length":         [-2, -1, 0, +1, +2],
        "strategy_params.volume_length":  [-6, -3, 0, +3, +6],

        "strategy_params.tp_atr_mult":       "fix",
        "strategy_params.sl_atr_mult":       "fix",
        "strategy_params.min_atr_ratio":     "fix",
        "strategy_params.min_momentum_sum":  "fix",
        "strategy_params.heat_exit_threshold":"fix",
        "strategy_params.heat_exit_min_rr":   "fix",
        "strategy_params.macd_filter":        "fix",
    })

    return rays + [grid_coarse, grid_heat, grid_micro]
