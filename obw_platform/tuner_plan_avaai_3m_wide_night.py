# tuner_plan_avaai_3m_night_fast.py
# Compact overnight tuner plan for 3m strategy.
# Target: finish in one night at ~3s/point. Centered around current best:
#   tp_atr_mult≈4.0, sl_atr_mult≈0.40, min_atr≈0.015, min_mom≈0.064,
#   min_qv_24h≈0, min_qv_1h≈1000, heat_thr≈0.90, heat_rr≈1.10, top_n≈4.
#
# Aliases supported by your tuner runtime:
#   'min-atr' -> strategy_params.min_atr_ratio
#   'min-mom' -> strategy_params.min_momentum_sum

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
    # 3m: ~480 bars/day => 30d ≈ 14_400. Keep plan tight so it finishes overnight.
    # Rough point budget: Rays sum of lengths (~<60) + a small Grid (~<=200 combos).

    # ---------------- Phase A: focused 1-D rays (exploration) ----------------
    rays = [
        # TP/SL around current best (narrow ranges)
        ("rays", {"strategy_params.tp_atr_mult": _seq(3.2, 4.8, 0.20)}),   # 3.2..4.8 step 0.2 (9)
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.35, 0.55, 0.02)}),  # 0.35..0.55 step 0.02 (11)

        # Entry filters: allow a tad looser ATR on 3m; momentum clustered near current
        ("rays", {"min-atr": _seq(0.010, 0.022, 0.002)}),                  # (7)
        ("rays", {"min-mom": _seq(0.040, 0.080, 0.004)}),                  # (11)

        # Liquidity thresholds (kept tiny to make HEAT informative w/o blowing budget)
        ("rays", {"strategy_params.min_qv_24h": _choices(0, 100000, 200000)}),  # (3)
        ("rays", {"strategy_params.min_qv_1h":  _choices(0, 1000, 5000)}),      # (3)

        # State exit (heat)
        ("rays", {"strategy_params.heat_exit_threshold": _seq(0.75, 0.95, 0.05)}),  # (5)
        ("rays", {"strategy_params.heat_exit_min_rr": _choices(1.05, 1.10, 1.15)}), # (3)

        # Portfolio breadth
        ("rays", {"strategy_params.top_n": _choices(4, 6, 8)}),                  # (3) 
    ]

    # ---------------- Phase B: small neighborhood grid (polish) --------------
    # Keep grid tiny (explicit choices), centered around the best from Rays.
    # NOTE: We intentionally grid only the most sensitive params to cap combos.
    grid = ("grid", {
        "strategy_params.tp_atr_mult":         [ -0.20, 0.0, +0.20 ],   # relative nudge around best
        "strategy_params.sl_atr_mult":         [ -0.02, 0.0, +0.02 ],
        "min-atr":                             [ -0.002, 0.0, +0.002 ],
        "min-mom":                             [ -0.004, 0.0, +0.004 ],
        "strategy_params.heat_exit_threshold": [ -0.05, 0.0, +0.05 ],
        # Fix/minimize other dims in grid to contain multiplicative explosion:
        "strategy_params.min_qv_24h":          "fix",
        "strategy_params.min_qv_1h":           "fix",
        "strategy_params.heat_exit_min_rr":    "fix",
        "strategy_params.top_n":               "fix",
    })

    # Your tuner should interpret numeric lists here as deltas around best,
    # and "fix" as "keep the best-found value".
    return rays + [grid]
