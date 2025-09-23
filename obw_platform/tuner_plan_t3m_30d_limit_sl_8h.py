# tuner_plan_t3m_30d_limit_sl_8h.py

def _seq(lo, hi, step):
    xs = []
    x = lo
    while x <= hi + 1e-12:
        xs.append(round(x, 10)); x += step
    return xs

def default_plan(limit_bars=None):
    rays = [
        ("rays", {"strategy_params.tp_atr_mult": _seq(3.2, 4.8, 0.1)}),
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.30, 0.55, 0.01)}),
        ("rays", {"strategy_params.min_atr_ratio": _seq(0.012, 0.022, 0.001)}),
        ("rays", {"strategy_params.min_momentum_sum": _seq(0.05, 0.11, 0.005)}),
        ("rays", {"strategy_params.heat_exit_threshold": _seq(0.90, 0.99, 0.01)}),
        ("rays", {"strategy_params.heat_exit_min_rr":
                  [1.05,1.07,1.10,1.13,1.15,1.18,1.20,1.22,1.25]}),
        ("rays", {"strategy_params.length": [9,10,11]}),
        ("rays", {"strategy_params.volume_length": [16,20,24]}),
        # macd_filter фікс:
        ("fix",  {"strategy_params.macd_filter": 1}),
        ("select_best", {"topk": 1})  # лишаємо BEST для grid-ів
    ]

    grid4d = ("grid_around_best", {
        # абсолютні дельти від BEST:
        "strategy_params.tp_atr_mult":       [-0.6, -0.3, 0.0, +0.3, +0.6],
        "strategy_params.sl_atr_mult":       [-0.06, -0.03, 0.0, +0.03, +0.06],
        "strategy_params.min_atr_ratio":     [-0.003, -0.0015, 0.0, +0.0015, +0.003],
        "strategy_params.min_momentum_sum":  [-0.02, -0.01, 0.0, +0.01, +0.02],
        # решта фікс з BEST
        "strategy_params.length":            "fix",
        "strategy_params.volume_length":     "fix",
        "strategy_params.macd_filter":       "fix",
        "select_topk": 40  # Top-40 до наступної фази
    })

    heat2d = ("grid_around_each_of_topk", {
        "strategy_params.heat_exit_threshold": [-0.02, 0.0, +0.02, +0.04],
        "strategy_params.heat_exit_min_rr":    [-0.05, 0.0, +0.05, +0.10],
    })

    micro_polish = ("micro_polish_topk", {
        "topk": 15,  # або 12, якщо хочеш ще швидше
        "strategy_params.length":             [-2, -1, 0, +1, +2],
        "strategy_params.volume_length":      [-6, -3, 0, +3, +6],
        # macd_filter = fix 1
    })

    return rays + [grid4d, heat2d, micro_polish]
