# Target: 3m TF, ~3600 bars (≈3d), з фокусом на поведінку на «pump & dump».
# Підтримує авто-тюнінг через auto_tuner_rays2grid_v3_fix.py
GRID_VALUES_ARE_DELTAS = True

def _seq(lo, hi, step):
    xs = []; x = float(lo)
    while x <= hi + 1e-12:
        xs.append(round(x, 10)); x += step
    return xs

def _choices(*vals):
    return list(vals)

def default_plan(limit_bars=None):
    # ---------- PHASE A: RAYS (широкі промені) ----------
    rays = [
        # sizing / universe
        ("rays", {"strategy_params.position_notional": _choices(2.25, 3.0, 4.5)}),

        # entry-фільтри (звужено для «швидких» рухів)
        ("rays", {"strategy_params.min_momentum_sum": _seq(0.012, 0.028, 0.004)}),
        ("rays", {"strategy_params.min_atr_ratio":     _seq(0.006, 0.012, 0.002)}),

        # risk-плечі (вужче; щоб менше ловити хвости дампів)
        ("rays", {"strategy_params.tp_atr_mult": _seq(2.6, 3.4, 0.2)}),
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.85, 1.05, 0.05)}),

        # heat-exit (ранній вихід, коли імпульс “вмер”)
        ("rays", {"exit_on_heat": _choices(True, False)}),
        ("rays", {"heat_exit_threshold": _seq(0.82, 0.90, 0.01)}),
        ("rays", {"heat_exit_min_rr":   _seq(1.10, 1.30, 0.05)}),

        # частковий TP (закріпити профіт на піку)
        ("rays", {"strategy_params.partial_tp_frac": _choices(0.5, 0.33)}),
        ("rays", {"strategy_params.partial_trigger_frac_of_tp": _choices(0.45, 0.50, 0.55)}),

        # HTF-bias (короткий sweep; допомагає не ловити контр-тренд)
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

    # ---------- PHASE B: GRID (coarse, як дельти від найкращих rays) ----------
    grid_coarse = ("grid", {
        "strategy_params.min_momentum_sum":  [-0.004, 0.0, +0.004],
        "strategy_params.min_atr_ratio":     [-0.002, 0.0, +0.002],
        "strategy_params.tp_atr_mult":       [-0.20, 0.0, +0.20],
        "strategy_params.sl_atr_mult":       [-0.10, 0.0, +0.10],

        "exit_on_heat":            "fix",
        "heat_exit_threshold":     [-0.02, 0.0, +0.02],
        "heat_exit_min_rr":        [-0.10, 0.0, +0.10],

        "strategy_params.partial_tp_frac":              "fix",
        "strategy_params.partial_trigger_frac_of_tp":   [-0.05, 0.0, +0.05],

        # HTF-bias дельти
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

    # ---------- PHASE C: POLISH (фінішне ущільнення) ----------
    grid_polish = ("grid", {
        "strategy_params.min_momentum_sum":  [-0.002, 0.0, +0.002],
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
