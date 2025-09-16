# tuner_plan_avaai_3m_wide_night.py
# Wide exploratory tuner plan for 3m strategy (overnight run).
# Built on top of the standard 3m plan, but with broader ranges to search
# for higher profitability and to naturally increase candidate coverage.
#
# Aliases your tuner runtime supports:
#   'min-atr' -> strategy_params.min_atr_ratio
#   'min-mom' -> strategy_params.min_momentum_sum
#
# Structure: list of steps like ("rays", {...}) for 1-D sweeps,
# and ("grid", {...}) for local refinements around the best results.

def _seq(lo, hi, step):
    xs, x = [], float(lo)
    if lo <= hi:
        while x <= hi + 1e-12:
            xs.append(round(x, 10)); x += step
    else:
        while x >= hi - 1e-12:
            xs.append(round(x, 10)); x -= step
    return xs

def _choices(*vals):
    return list(vals)

def default_plan(limit_bars=None):
    # 3m: ~480 bars/day => 30d ≈ 14_400; 90d ≈ 43_200.
    is_30d = (limit_bars or 0) <= 16000

    # --- Phase A: Wide rays (exploration) -----------------------------------
    # Broadened ranges to cover more profitable regimes.
    rays = [
        # Take-profit / Stop-loss (ATR-based)
        # ("rays", {"strategy_params.tp_atr_mult": _seq(2.0, 6.0, 0.20)}),
        # ("rays", {"strategy_params.sl_atr_mult": _seq(0.40, 1.40, 0.05)}),

        #Entry filters (looser on 3m to allow more candidates)
        # ("rays", {"min-atr": _seq(0.010, 0.030, 0.002)}),          # 3m ATR ratio often lower
        # ("rays", {"min-mom": _seq(0.000, 0.060, 0.004)}),          # includes 0 to allow momentum-free entries

        #Liquidity thresholds (to make heat informative and control slippage)
        # ("rays", {"strategy_params.min_qv_24h": _choices(0, 100000, 200000, 300000)}),
        # ("rays", {"strategy_params.min_qv_1h":  _choices(0, 5000, 10000, 20000)}),

        #Exit on heat (state exit); scan for best stability
        # ("rays", {"strategy_params.heat_exit_threshold": _seq(0.90, 0.95, 0.05)}),
        # ("rays", {"strategy_params.heat_exit_min_rr": _choices(1.00, 1.05, 1.10, 1.20)}),

        # Portfolio breadth
        ("rays", {"strategy_params.top_n": _choices(6, 8, 10, 12)}),
    ]

    # --- Phase B: Local grids around the best --------------------------------
    # Two progressively tighter grids to polish around the top configs.
    grid1 = ("grid", {
        "strategy_params.tp_atr_mult":           "around:0.20",
        "strategy_params.sl_atr_mult":           "around:0.05",
        "min-atr":                               "around:0.002",
        "min-mom":                               "around:0.004",
        "strategy_params.min_qv_24h":            "around:100000",
        "strategy_params.min_qv_1h":             "around:5000",
        "strategy_params.heat_exit_threshold":   "around:0.05",
        "strategy_params.heat_exit_min_rr":      "around:0.05",
        "strategy_params.top_n":                 "around:2",
    })

    grid2 = ("grid", {
         "strategy_params.tp_atr_mult":           "around:0.10",
        # "strategy_params.sl_atr_mult":           "around:0.02",
        # "min-atr":                               "around:0.001",
        # "min-mom":                               "around:0.002",
        # "strategy_params.min_qv_24h":            "around:50000",
        # "strategy_params.min_qv_1h":             "around:2500",
        # "strategy_params.heat_exit_threshold":   "around:0.02",
        # "strategy_params.heat_exit_min_rr":      "around:0.02",
        # "strategy_params.top_n":                 "around:1",
    })

    return rays + [grid1, grid2]
