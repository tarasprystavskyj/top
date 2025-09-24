# tuner_plan_htf_bias_3m_30d.py
# Goal: After an HTF regime break, bias selection towards shorts (or longs) for
# a configurable number of bars. Enforce/encourage side via ranking and entry
# filters, with confirmation & hysteresis to avoid whipsaws.
#
# >>> All knobs live inside breakout_avaai_full_with_universe_7.py <<<
#
# Expected strategy_params in YAML/strategy (suggested names):
#   htf_bias.enabled: bool
#   htf_bias.tf: str                 # e.g. "30m" or "1h"
#   htf_bias.break_min: float        # abs(HTF delta) threshold to detect regime break
#   htf_bias.confirm_bars: int       # bars of confirmation on HTF to accept break
#   htf_bias.hysteresis_bars: int    # bars to lock the bias to avoid flip-flop
#   htf_bias.cooldown_bars: int      # minimal bars to keep bias active after break
#   htf_bias.mode: "enforce"|"tilt"  # hard filter vs. ranking boost
#   htf_bias.rank_boost_short: float # added score for SHORT when bias=short
#   htf_bias.rank_boost_long: float  # added score for LONG when bias=long
#   htf_bias.entry_gate: bool        # if True, block the opposite side in entry_signal
#   htf_bias.mom_confirm_min: float  # optional LTF momentum confirm (same sign)
#   htf_bias.heat_relax_rr: float    # lower heat_exit_min_rr while bias is active (0 = off)
#
# Plan shape:
#   Phase A (RAYS): sweep key bias switches & core thresholds.
#   Phase B (GRID coarse): multi-dim deltas around best rays.
#   Phase C (POLISH): fine tune hysteresis/cooldown & rank boosts.
#
# The auto_tuner will treat GRID lists as DELTAS around the current best.
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
    # -------- Phase A: RAYS (find robust bias regime & gating) --------
    rays = [
        ("rays", {"strategy_params.htf_bias.enabled": _choices(True)}),

        # regime break sensitivity
        ("rays", {"strategy_params.htf_bias.break_min": _seq(0.0015, 0.0060, 0.0005)}),
        ("rays", {"strategy_params.htf_bias.confirm_bars": _choices(1, 2, 3)}),

        # persistence / stability
        ("rays", {"strategy_params.htf_bias.hysteresis_bars": _choices(2, 4, 6, 8)}),
        ("rays", {"strategy_params.htf_bias.cooldown_bars": _choices(4, 6, 10, 14)}),

        # enforcement style
        ("rays", {"strategy_params.htf_bias.mode": _choices("tilt", "enforce")}),
        ("rays", {"strategy_params.htf_bias.entry_gate": _choices(False, True)}),

        # rank boosts (when bias active)
        ("rays", {"strategy_params.htf_bias.rank_boost_short": _seq(0.3, 1.2, 0.15)}),
        ("rays", {"strategy_params.htf_bias.rank_boost_long":  _seq(0.3, 1.2, 0.15)}),

        # optional momentum confirmation on LTF (same sign as bias)
        ("rays", {"strategy_params.htf_bias.mom_confirm_min": _seq(0.0, 0.020, 0.005)}),

        # optional heat relaxation to ride the break
        ("rays", {"strategy_params.htf_bias.heat_relax_rr": _choices(0.0, 0.05, 0.10, 0.15)}),

        # typical HTF choices (keep compact)
        ("rays", {"strategy_params.htf_bias.tf": _choices("30m", "1h")}),
    ]

    # -------- Phase B: GRID (coarse, deltas around best rays) --------
    # Use deltas so the grid centers on best-from-rays. The tuner clamps ranges.
    grid_coarse = ("grid", {
        "strategy_params.htf_bias.break_min":        [-0.0010, -0.0005, 0.0, +0.0005, +0.0010],
        "strategy_params.htf_bias.confirm_bars":     [-1, 0, +1],
        "strategy_params.htf_bias.hysteresis_bars":  [-2, 0, +2],
        "strategy_params.htf_bias.cooldown_bars":    [-2, 0, +4],

        "strategy_params.htf_bias.rank_boost_short": [-0.3, 0.0, +0.3],
        "strategy_params.htf_bias.rank_boost_long":  [-0.3, 0.0, +0.3],
        "strategy_params.htf_bias.mom_confirm_min":  [-0.005, 0.0, +0.005],
        "strategy_params.htf_bias.heat_relax_rr":    [-0.05, 0.0, +0.05],

        # locks (no delta)
        "strategy_params.htf_bias.enabled":      "fix",
        "strategy_params.htf_bias.mode":         "fix",
        "strategy_params.htf_bias.entry_gate":   "fix",
        "strategy_params.htf_bias.tf":           "fix",
    })

    # -------- Phase C: POLISH (tighten stability / whipsaw resistance) --------
    grid_polish = ("grid", {
        "strategy_params.htf_bias.hysteresis_bars": [-1, 0, +1],
        "strategy_params.htf_bias.cooldown_bars":   [-2, 0, +2],
        "strategy_params.htf_bias.confirm_bars":    [-1, 0, +1],
        "strategy_params.htf_bias.break_min":       [-0.0005, 0.0, +0.0005],

        "strategy_params.htf_bias.rank_boost_short": [-0.15, 0.0, +0.15],
        "strategy_params.htf_bias.rank_boost_long":  [-0.15, 0.0, +0.15],
        "strategy_params.htf_bias.mom_confirm_min":  [-0.0025, 0.0, +0.0025],
        "strategy_params.htf_bias.heat_relax_rr":    [-0.02, 0.0, +0.02],

        "strategy_params.htf_bias.enabled":    "fix",
        "strategy_params.htf_bias.mode":       "fix",
        "strategy_params.htf_bias.entry_gate": "fix",
        "strategy_params.htf_bias.tf":         "fix",
    })

    return rays + [grid_coarse, grid_polish]
