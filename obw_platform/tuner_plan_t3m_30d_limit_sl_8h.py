# tuner_plan_t3m_30d_limit_sl_8h.py
# План під cfg_t3m_30d_newbest_full_limit_sl.yaml (3m, 30d ≈ 14_400 барів)
# Підтримує лише режими ("rays", {...}) та ("grid", {...}) з "fix"/дельтами.

def _seq(lo, hi, step):
    xs, x = [], float(lo)
    while x <= hi + 1e-12:
        xs.append(round(x, 10)); x += step
    return xs

def _choices(*vals):
    return list(vals)

def default_plan(limit_bars=None):
    # ---------------- Phase A: 1-D RAYS (оглядово, але вузько по length/macd/volume) ----------------
    rays = [
        # TP/SL — ширший огляд
        ("rays", {"strategy_params.tp_atr_mult": _seq(3.2, 4.8, 0.1)}),     # 17
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.30, 0.55, 0.01)}),  # 26

        # Entry-фільтри
        ("rays", {"strategy_params.min_atr_ratio": _seq(0.012, 0.022, 0.001)}),   # 11
        ("rays", {"strategy_params.min_momentum_sum": _seq(0.050, 0.110, 0.005)}),# 13

        # Heat-вихід (грубо)
        ("rays", {"strategy_params.heat_exit_threshold": _seq(0.90, 0.99, 0.01)}),# 10
        ("rays", {"strategy_params.heat_exit_min_rr": _choices(1.05,1.07,1.10,1.13,1.15,1.18,1.20,1.22,1.25)}),

        # Вузькі поля
        ("rays", {"strategy_params.length": _choices(9,10,11)}),
        ("rays", {"strategy_params.volume_length": _choices(16,20,24)}),
        # macd_filter фіксуємо як у конфігу
        ("rays", {"strategy_params.macd_filter": _choices(1)}),
    ]

    # ---------------- Phase B1: COARSE GRID навколо BEST (4D) ----------------
    # Усі списки — це ДЕЛЬТИ навколо BEST; "fix" = залишити значення BEST.
    grid_coarse = ("grid", {
        "strategy_params.tp_atr_mult":       [0.0, +0.3, +0.6],
        "strategy_params.sl_atr_mult":       [0.0, +0.03, +0.06],
        "strategy_params.min_atr_ratio":     [0.0, +0.0015, +0.003],
        "strategy_params.min_momentum_sum":  [0.0, +0.01, +0.02],

        # все інше фіксуємо
        "strategy_params.length":        "fix",
        "strategy_params.volume_length": "fix",
        "strategy_params.macd_filter":   "fix",
        "strategy_params.heat_exit_threshold": "fix",
        "strategy_params.heat_exit_min_rr":    "fix",
    })

    # ---------------- Phase B2: HEAT POLISH (2D) ----------------
    grid_heat = ("grid", {
        "strategy_params.heat_exit_threshold": [0.0, +0.02, +0.04],
        "strategy_params.heat_exit_min_rr":    [0.0, +0.05, +0.10],

        "strategy_params.tp_atr_mult":      "fix",
        "strategy_params.sl_atr_mult":      "fix",
        "strategy_params.min_atr_ratio":    "fix",
        "strategy_params.min_momentum_sum": "fix",
        "strategy_params.length":           "fix",
        "strategy_params.volume_length":    "fix",
        "strategy_params.macd_filter":      "fix",
    })

    # ---------------- Phase C: MICRO для length/volume (вузький спред) ----------------
    grid_micro = ("grid", {
        "strategy_params.length":        [0, +1, +2],
        "strategy_params.volume_length": [0, +3, +6],

        "strategy_params.tp_atr_mult":       "fix",
        "strategy_params.sl_atr_mult":       "fix",
        "strategy_params.min_atr_ratio":     "fix",
        "strategy_params.min_momentum_sum":  "fix",
        "strategy_params.heat_exit_threshold":"fix",
        "strategy_params.heat_exit_min_rr":   "fix",
        "strategy_params.macd_filter":        "fix",
    })

    return rays + [grid_coarse, grid_heat, grid_micro]
