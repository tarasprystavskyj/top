# tuner_plan_avaai_3m.py
# План для 3m-стратегії (30d/90d), побудований на базі tuner_plan_avaai_3090.py.
# Підтримуються alias:
#   'min-atr' -> strategy_params.min_atr_ratio
#   'min-mom' -> strategy_params.min_momentum_sum

def _seq(lo, hi, step):
    xs, x = [], float(lo)
    if lo <= hi:
        while x <= hi + 1e-12:
            xs.append(round(x, 10))
            x += step
    else:
        while x >= hi - 1e-12:
            xs.append(round(x, 10))
            x -= step
    return xs

def default_plan(limit_bars=None):
    # 3m: ~480 барів/день, тож 30d ≈ 14_400 барів, 90d ≈ 43_200.
    is_30d = (limit_bars or 0) <= 16000

    if is_30d:
        rays = [
            # tp/sl
            ("rays", {"strategy_params.tp_atr_mult": _seq(2.0, 5.0, 0.20)}),
            ("rays", {"strategy_params.sl_atr_mult": _seq(0.50, 1.20, 0.05)}),

            # вхідні фільтри
            ("rays", {"min-atr": _seq(0.015, 0.060, 0.005)}),
            ("rays", {"min-mom": _seq(0.008, 0.080, 0.004)}),

            # вихід по heat
            ("rays", {"strategy_params.heat_exit_threshold": _seq(0.40, 0.95, 0.05)}),
        ]

        grid1 = ("grid", {
            "strategy_params.tp_atr_mult": "around:0.20",
            "strategy_params.sl_atr_mult": "around:0.05",
            "min-atr": "around:0.005",
            "min-mom": "around:0.004",
            "strategy_params.heat_exit_threshold": "around:0.05",
        })

        grid2 = ("grid", {
            "strategy_params.tp_atr_mult": "around:0.10",
            "strategy_params.sl_atr_mult": "around:0.02",
            "min-atr": "around:0.003",
            "min-mom": "around:0.003",
            "strategy_params.heat_exit_threshold": "around:0.02",
        })
        return rays + [grid1, grid2]

    # 90d — стискаємо пошук і шліфуємо довкола найкращого
    rays = [
        ("rays", {"strategy_params.tp_atr_mult": _seq(2.0, 5.0, 0.25)}),
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.50, 1.20, 0.05)}),
        ("rays", {"min-atr": _seq(0.015, 0.060, 0.005)}),
        ("rays", {"min-mom": _seq(0.008, 0.080, 0.004)}),
        ("rays", {"strategy_params.heat_exit_threshold": _seq(0.40, 0.95, 0.05)}),
    ]

    grid1 = ("grid", {
        "strategy_params.tp_atr_mult": "around:0.15",
        "strategy_params.sl_atr_mult": "around:0.04",
        "min-atr": "around:0.004",
        "min-mom": "around:0.003",
        "strategy_params.heat_exit_threshold": "around:0.04",
    })
    grid2 = ("grid", {
        "strategy_params.tp_atr_mult": "around:0.07",
        "strategy_params.sl_atr_mult": "around:0.02",
        "min-atr": "around:0.002",
        "min-mom": "around:0.002",
        "strategy_params.heat_exit_threshold": "around:0.02",
    })
    return rays + [grid1, grid2]
