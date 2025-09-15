# tuner_plan_avaai_3090.py
# 30d -> 90d план: тюнимо тільки запитані параметри.
# Підтримує alias 'min-atr' -> strategy_params.min_atr_ratio,
# 'min-mom' -> strategy_params.min_momentum_sum.

def _seq(lo, hi, step):
    xs, x = [], float(lo)
    if lo <= hi:
        while x <= hi + 1e-12:
            xs.append(round(x, 10)); x += step
    else:
        while x >= hi - 1e-12:
            xs.append(round(x, 10)); x -= step
    return xs

def default_plan(limit_bars=None):
    # Якщо вікно невелике (тест швидкий) — трохи грубіші кроки
    is_30d = (limit_bars or 0) <= 9000  # ~8640 барів
    if is_30d:
        rays = [
            # tp/sl
            ("rays", {"strategy_params.tp_atr_mult": _seq(2.0, 5.0, 0.2)}),
            ("rays", {"strategy_params.sl_atr_mult": _seq(0.50, 1.20, 0.05)}),

            # фільтри входу
            ("rays", {"min-atr": _seq(0.02, 0.05, 0.005)}),        # == strategy_params.min_atr_ratio
            ("rays", {"min-mom": _seq(0.01, 0.07, 0.005)}),        # == strategy_params.min_momentum_sum

            # вихід по heat
            ("rays", {"strategy_params.heat_exit_threshold": _seq(0.40, 0.95, 0.05)}),
        ]

        grid1 = ("grid", {
            "strategy_params.tp_atr_mult": "around:0.20",
            "strategy_params.sl_atr_mult": "around:0.05",
            "min-atr": "around:0.005",
            "min-mom": "around:0.005",
            "strategy_params.heat_exit_threshold": "around:0.05",
        })

        grid2 = ("grid", {
            "strategy_params.tp_atr_mult": "around:0.10",
            "strategy_params.sl_atr_mult": "around:0.02",
            "min-atr": "around:0.002",
            "min-mom": "around:0.002",
            "strategy_params.heat_exit_threshold": "around:0.02",
        })
        return rays + [grid1, grid2]

    # 90d — стискаємо пошук та шліфуємо довкола кращого
    rays = [
        ("rays", {"strategy_params.tp_atr_mult": _seq(2.0, 5.0, 0.25)}),
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.50, 1.20, 0.05)}),
        ("rays", {"min-atr": _seq(0.02, 0.05, 0.005)}),
        ("rays", {"min-mom": _seq(0.01, 0.07, 0.005)}),
        ("rays", {"strategy_params.heat_exit_threshold": _seq(0.40, 0.95, 0.05)}),
    ]

    grid1 = ("grid", {
        "strategy_params.tp_atr_mult": "around:0.15",
        "strategy_params.sl_atr_mult": "around:0.04",
        "min-atr": "around:0.004",
        "min-mom": "around:0.004",
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
