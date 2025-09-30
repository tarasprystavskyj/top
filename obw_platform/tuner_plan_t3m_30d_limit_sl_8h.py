# tuner_plan_t3m_30d_limit_sl_8h_pf.py
# Target: 8h window on 3m bars; push PF↑ with reasonable DD and trade count.
# Works with auto_tuner_rays2grid_v3_fix.py

GRID_VALUES_ARE_DELTAS = True

def _seq(lo, hi, step):
    xs = []
    x = float(lo)
    while x <= hi + 1e-12:
        xs.append(round(x, 10))
        x += step
    return xs

def _choices(*vals):
    return list(vals)

def default_plan(limit_bars=None):
    """
    Keys map to your YAML under strategy_params / risk / heat sections, e.g.:
      strategy_params.tp_atr_mult, strategy_params.sl_atr_mult,
      strategy_params.min_momentum_sum, strategy_params.min_atr_ratio,
      exit_on_heat, heat_exit_threshold, heat_exit_min_rr, etc.
    """

    # -------- Phase A: RAYS (широкі “промені”) --------
    rays = [
        # universe/ranking sizing
        ("rays", {"top-n": _choices(8, 10, 12)}),              # якщо використовуєш top-n у YAML
        ("rays", {"strategy_params.position_notional": _choices(2.25, 3.0, 4.5)}),

        # entries: ключові тригери на LTF
        ("rays", {"strategy_params.min_momentum_sum": _seq(0.010, 0.030, 0.005)}),
        ("rays", {"strategy_params.min_atr_ratio":     _seq(0.006, 0.012, 0.002)}),

        # risk-legs
        ("rays", {"strategy_params.tp_atr_mult": _seq(2.6, 3.6, 0.2)}),
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.85, 1.10, 0.05)}),

        # heat / early-exit (вмикаємо механізм «тепла» лише коли корисно)
        ("rays", {"exit_on_heat": _choices(True, False)}),
        ("rays", {"heat_exit_threshold": _seq(0.82, 0.90, 0.01)}),
        ("rays", {"heat_exit_min_rr":   _seq(1.10, 1.30, 0.05)}),

        # частковий TP: скільки та коли (стратегія повинна це зчитувати)
        ("rays", {"strategy_params.partial_tp_frac": _choices(0.5, 0.33)}),
        ("rays", {"strategy_params.partial_trigger_frac_of_tp": _choices(0.45, 0.50, 0.55)}),

        # HTF-bias (див. твій bias-план; робимо короткий sweep)
        ("rays", {"strategy_params.htf_bias.enabled": _choices(True)}),
        ("rays", {"strategy_params.htf_bias.tf": _choices("30m", "1h")}),
        ("rays", {"strategy_params.htf_bias.mode": _choices("tilt", "enforce")}),
        ("rays", {"strategy_params.htf_bias.break_min": _seq(0.0015, 0.0045, 0.0005)}),
        ("rays", {"strategy_params.htf_bias.confirm_bars": _choices(1, 2, 3)}),
        ("rays", {"strategy_params.htf_bias.hysteresis_bars": _choices(2, 4, 6)}),
        ("rays", {"strategy_params.htf_bias.cooldown_bars": _choices(4, 8, 12)}),
        ("rays", {"strategy_params.htf_bias.rank_boost_short": _seq(0.3, 0.9, 0.3)}),
        ("rays", {"strategy_params.htf_bias.rank_boost_long":  _seq(0.3, 0.9, 0.3)}),
        ("rays", {"strategy_params.htf_bias.entry_gate": _choices(False, True)}),
        ("rays", {"strategy_params.htf_bias.mom_confirm_min": _seq(0.0, 0.020, 0.005)}),
        ("rays", {"strategy_params.htf_bias.heat_relax_rr": _choices(0.0, 0.05, 0.10)}),
    ]

    # -------- Phase B: GRID (coarse — дельти навколо кращих із rays) --------
    grid_coarse = ("grid", {
        "strategy_params.min_momentum_sum":  [-0.005, 0.0, +0.005],
        "strategy_params.min_atr_ratio":     [-0.002, 0.0, +0.002],
        "strategy_params.tp_atr_mult":       [-0.25, 0.0, +0.25],
        "strategy_params.sl_atr_mult":       [-0.10, 0.0, +0.10],

        "exit_on_heat":            "fix",
        "heat_exit_threshold":     [-0.02, 0.0, +0.02],
        "heat_exit_min_rr":        [-0.10, 0.0, +0.10],

        "strategy_params.partial_tp_frac":              "fix",
        "strategy_params.partial_trigger_frac_of_tp":   [-0.05, 0.0, +0.05],

        # HTF bias deltas
        "strategy_params.htf_bias.break_min":        [-0.0005, 0.0, +0.0005],
        "strategy_params.htf_bias.confirm_bars":     [-1, 0, +1],
        "strategy_params.htf_bias.hysteresis_bars":  [-2, 0, +2],
        "strategy_params.htf_bias.cooldown_bars":    [-2, 0, +2],
        "strategy_params.htf_bias.rank_boost_short": [-0.2, 0.0, +0.2],
        "strategy_params.htf_bias.rank_boost_long":  [-0.2, 0.0, +0.2],
        "strategy_params.htf_bias.mom_confirm_min":  [-0.005, 0.0, +0.005],
        "strategy_params.htf_bias.heat_relax_rr":    [-0.05, 0.0, +0.05],

        # locks
        "strategy_params.htf_bias.enabled":  "fix",
        "strategy_params.htf_bias.mode":     "fix",
        "strategy_params.htf_bias.entry_gate":"fix",
        "strategy_params.htf_bias.tf":       "fix",
    })

    # -------- Phase C: POLISH (фінішне ущільнення) --------
    grid_polish = ("grid", {
        "strategy_params.min_momentum_sum":  [-0.0025, 0.0, +0.0025],
        "strategy_params.min_atr_ratio":     [-0.001, 0.0, +0.001],
        "strategy_params.tp_atr_mult":       [-0.10, 0.0, +0.10],
        "strategy_params.sl_atr_mult":       [-0.05, 0.0, +0.05],

        "heat_exit_threshold":     [-0.01, 0.0, +0.01],
        "heat_exit_min_rr":        [-0.05, 0.0, +0.05],
        "strategy_params.partial_trigger_frac_of_tp": [-0.02, 0.0, +0.02],

        "strategy_params.htf_bias.hysteresis_bars": [-1, 0, +1],
        "strategy_params.htf_bias.cooldown_bars":   [-1, 0, +1],
    })

    return rays + [grid_coarse, grid_polish]
