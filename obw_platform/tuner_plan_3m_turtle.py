# Target: 3m TF, ~3600–7200 bars; MDD-first tuning for BingX "TURTLE…" контест-універс
# Works with: auto_tuner_rays2grid_v3_fix.py
# Strategy cfg: cfg_t3m_turtle.yaml
# Пріоритети: 1) мінімізувати max drawdown; 2) утримати/трохи підвищити equity_end; 3) зберегти адекватну кількість угод.

GRID_VALUES_ARE_DELTAS = True

def _seq(lo, hi, step):
    xs = []; x = float(lo)
    while x <= hi + 1e-12:
        xs.append(round(x, 10)); x += step
    return xs

def _choices(*vals):
    return list(vals)

def default_plan(limit_bars=None):
    # -------- PHASE A: RAYS (грубий пошук у «безпечній» зоні) --------
    rays = [
        # 1) sizing — дрібніше, щоб зменшити MDD
        ("rays", {"strategy_params.position_notional": _choices(1.2, 1.6, 2.0, 2.4)}),

        # 2) фільтри входу — вищі вимоги до імпульсу/волатильності
        ("rays", {"strategy_params.min_momentum_sum": _seq(0.020, 0.050, 0.005)}),
        ("rays", {"strategy_params.min_atr_ratio":     _seq(0.008, 0.016, 0.002)}),

        # 3) ризик: ширший SL (у ATR), помірні TP (щоб не ловити хвости дампів)
        ("rays", {"strategy_params.sl_atr_mult": _seq(1.10, 1.60, 0.10)}),
        ("rays", {"strategy_params.tp_atr_mult": _seq(2.00, 3.20, 0.20)}),

        # 4) heat-exit (раннє закриття, якщо імпульс «погас»)
        ("rays", {"exit_on_heat": _choices(True)}),
        ("rays", {"heat_exit_threshold": _seq(0.86, 0.94, 0.02)}),
        ("rays", {"heat_exit_min_rr":   _seq(1.20, 1.60, 0.10)}),

        # 5) часткове фіксування — трохи раніше, щоб зменшити ризик
        ("rays", {"strategy_params.partial_tp_frac": _choices(0.5, 0.33)}),
        ("rays", {"strategy_params.partial_trigger_frac_of_tp": _choices(0.45, 0.50, 0.55)}),

        # 6) HTF-bias: обов’язково, у «enforce»; невелика гістерезис/підтвердження
        ("rays", {"strategy_params.htf_bias.enabled": _choices(True)}),
        ("rays", {"strategy_params.htf_bias.mode":    _choices("enforce")}),
        ("rays", {"strategy_params.htf_bias.tf":      _choices("30m", "1h")}),
        ("rays", {"strategy_params.htf_bias.break_min":      _seq(0.0020, 0.0050, 0.0010)}),
        ("rays", {"strategy_params.htf_bias.confirm_bars":   _choices(2, 3)}),
        ("rays", {"strategy_params.htf_bias.hysteresis_bars":_choices(2, 4, 6)}),
        ("rays", {"strategy_params.htf_bias.cooldown_bars":  _choices(6, 8, 12)}),
        ("rays", {"strategy_params.htf_bias.rank_boost_long":  _seq(0.2, 0.8, 0.2)}),
        ("rays", {"strategy_params.htf_bias.rank_boost_short": _seq(0.2, 0.8, 0.2)}),
        ("rays", {"strategy_params.htf_bias.entry_gate":     _choices(True)}),
        ("rays", {"strategy_params.htf_bias.mom_confirm_min":_seq(0.00, 0.02, 0.01)}),
        ("rays", {"strategy_params.htf_bias.heat_relax_rr":  _choices(0.0, 0.05)}),
    ]

    # -------- PHASE B: GRID (coarse — локальна обвідка навколо кращих променів) --------
    grid_coarse = ("grid", {
        "strategy_params.position_notional": [-0.4, 0.0, +0.4],
        "strategy_params.min_momentum_sum":  [-0.005, 0.0, +0.005],
        "strategy_params.min_atr_ratio":     [-0.002, 0.0, +0.002],
        "strategy_params.sl_atr_mult":       [-0.20, 0.0, +0.20],
        "strategy_params.tp_atr_mult":       [-0.20, 0.0, +0.20],

        "exit_on_heat":            "fix",
        "heat_exit_threshold":     [-0.02, 0.0, +0.02],
        "heat_exit_min_rr":        [-0.10, 0.0, +0.10],

        "strategy_params.partial_tp_frac":              "fix",
        "strategy_params.partial_trigger_frac_of_tp":   [-0.05, 0.0, +0.05],

        # HTF-bias дельти
        "strategy_params.htf_bias.break_min":        [-0.001, 0.0, +0.001],
        "strategy_params.htf_bias.confirm_bars":     [-1, 0, +1],
        "strategy_params.htf_bias.hysteresis_bars":  [-2, 0, +2],
        "strategy_params.htf_bias.cooldown_bars":    [-2, 0, +2],
        "strategy_params.htf_bias.rank_boost_long":  [-0.2, 0.0, +0.2],
        "strategy_params.htf_bias.rank_boost_short": [-0.2, 0.0, +0.2],
        "strategy_params.htf_bias.mom_confirm_min":  [-0.01, 0.0, +0.01],
        "strategy_params.htf_bias.heat_relax_rr":    [-0.05, 0.0, +0.05],

        "strategy_params.htf_bias.enabled":  "fix",
        "strategy_params.htf_bias.mode":     "fix",
        "strategy_params.htf_bias.entry_gate":"fix",
        "strategy_params.htf_bias.tf":       "fix",
    })

    # -------- PHASE C: POLISH (звуження біля локального оптимуму) --------
    grid_polish = ("grid", {
        "strategy_params.position_notional": [-0.2, 0.0, +0.2],
        "strategy_params.min_momentum_sum":  [-0.002, 0.0, +0.002],
        "strategy_params.min_atr_ratio":     [-0.001, 0.0, +0.001],
        "strategy_params.sl_atr_mult":       [-0.10, 0.0, +0.10],
        "strategy_params.tp_atr_mult":       [-0.10, 0.0, +0.10],

        "heat_exit_threshold":     [-0.01, 0.0, +0.01],
        "heat_exit_min_rr":        [-0.05, 0.0, +0.05],
        "strategy_params.partial_trigger_frac_of_tp": [-0.02, 0.0, +0.02],

        "strategy_params.htf_bias.hysteresis_bars": [-1, 0, +1],
        "strategy_params.htf_bias.cooldown_bars":   [-1, 0, +1],
    })

    return rays + [grid_coarse, grid_polish]

