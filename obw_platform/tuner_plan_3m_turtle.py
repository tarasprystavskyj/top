# Target: 3m TF, ~3600–7200 bars; MDD-first tuning for BingX "TURTLE…" контест-універс
# Works with: auto_tuner_rays2grid_v3_fix.py
# Strategy cfg: cfg_t3m_turtle.yaml
# Пріоритети: 1) мінімізувати max drawdown; 2) утримати/трохи підвищити equity_end; 3) зберегти адекватну кількість угод.
import os, math, yaml

GRID_VALUES_ARE_DELTAS = True

def _seq(lo, hi, step):
    xs = []; x = float(lo)
    while x <= hi + 1e-12:
        xs.append(round(x, 10)); x += step
    return xs

def _choices(*vals):
    return list(vals)

def default_plan(limit_bars=None):
    def _pct_step(base, pct_span=0.15, steps=6, minv=None, maxv=None):
        """symmetric range around base with % span and given #steps (odd -> includes base)."""
        b = float(base)
        lo, hi = b * (1 - pct_span), b * (1 + pct_span)
        if minv is not None: lo = max(lo, minv)
        if maxv is not None: hi = min(hi, maxv)
        if steps < 2 or lo >= hi: return [round(b, 10)]
        step = (hi - lo) / (steps - 1)
        return _seq(lo, hi, step)

    def _tight_bool(default=True):  # keep behavior but allow on/off probe
        return _choices(default) if default else _choices(False, True)

    def _load_cfg(path):
        with open(path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    # ---------- load base config ----------
    CFG_PATH = os.environ.get("OBW_CFG_PATH", "configs/cfg_t3m_turtle.yaml")
    try:
        CFG = _load_cfg(CFG_PATH)
    except Exception:
        CFG = {}

    SP = CFG.get("strategy_params", {})
    HTF = SP.get("htf_bias", {})

    def _get(dct, key, default):
        return dct.get(key, default)

    # -------- PHASE A: RAYS (грубий пошук у «безпечній» зоні) --------
    position_notional = _get(SP, "position_notional", 2.0)
    min_momentum_sum = _get(SP, "min_momentum_sum", 0.025)
    min_atr_ratio     = _get(SP, "min_atr_ratio", 0.012)
    sl_atr_mult       = _get(SP, "sl_atr_mult", 0.30)
    tp_atr_mult       = _get(SP, "tp_atr_mult", 4.2)

    partial_tp_frac   = _get(SP, "partial_tp_frac", 0.5)
    partial_trig_frac = _get(SP, "partial_trigger_frac_of_tp", 0.50)

    exit_on_heat      = CFG.get("exit_on_heat", True)
    heat_thr          = _get(CFG, "heat_exit_threshold", 0.88)
    heat_min_rr       = _get(CFG, "heat_exit_min_rr", 1.3)

    htf_enabled       = _get(HTF, "enabled", True)
    htf_mode          = _get(HTF, "mode", "enforce")
    htf_tf            = _get(HTF, "tf", "30m")
    htf_break_min     = _get(HTF, "break_min", 0.003)
    htf_confirm_bars  = _get(HTF, "confirm_bars", 2)
    htf_hyst_bars     = _get(HTF, "hysteresis_bars", 2)
    htf_cooldown      = _get(HTF, "cooldown_bars", 8)
    htf_rank_boost_l  = _get(HTF, "rank_boost_long", 0.4)
    htf_rank_boost_s  = _get(HTF, "rank_boost_short", 0.4)
    htf_entry_gate    = _get(HTF, "entry_gate", True)
    htf_mom_min       = _get(HTF, "mom_confirm_min", 0.01)
    htf_heat_relax_rr = _get(HTF, "heat_relax_rr", 0.0)

    # ---------- rays: tight around base ----------
    rays = [
        # 1) sizing — трохи дрібніше для DD
        ("rays", {"strategy_params.position_notional": _pct_step(position_notional, 0.12, 5, 0.6, 4.0)}),

        # 2) фільтри входу — вузько біля базових
        ("rays", {"strategy_params.min_momentum_sum": _pct_step(min_momentum_sum, 0.20, 7, 0.005, 0.08)}),
        ("rays", {"strategy_params.min_atr_ratio":     _pct_step(min_atr_ratio,     0.20, 7, 0.004, 0.05)}),

        # 3) ризик: SL/TP «біля» базових (turtle: памп/дамп)
        ("rays", {"strategy_params.sl_atr_mult": _pct_step(sl_atr_mult, 0.25, 7, 0.12, 1.2)}),
        ("rays", {"strategy_params.tp_atr_mult": _pct_step(tp_atr_mult, 0.20, 7, 1.0, 8.0)}),

        # 4) heat-exit
        ("rays", {"exit_on_heat": _tight_bool(exit_on_heat)}),
        ("rays", {"heat_exit_threshold": _pct_step(heat_thr, 0.06, 5, 0.70, 0.98)}),
        ("rays", {"heat_exit_min_rr":   _pct_step(heat_min_rr, 0.20, 5, 0.8, 2.5)}),

        # 5) часткове фіксування
        ("rays", {"strategy_params.partial_tp_frac": _choices(
            max(0.2, round(partial_tp_frac - 0.17, 3)),
            max(0.25, round(partial_tp_frac - 0.10, 3)),
            round(partial_tp_frac, 3),
            min(0.66, round(partial_tp_frac + 0.10, 3))
        )}),
        ("rays", {"strategy_params.partial_trigger_frac_of_tp": _choices(
            max(0.35, round(partial_trig_frac - 0.10, 3)),
            round(partial_trig_frac, 3),
            min(0.65, round(partial_trig_frac + 0.10, 3))
        )}),

        # 6) HTF-bias (enforce)
        ("rays", {"strategy_params.htf_bias.enabled": _choices(True)}),
        ("rays", {"strategy_params.htf_bias.mode":    _choices("enforce")}),
        ("rays", {"strategy_params.htf_bias.tf":      _choices(htf_tf, "1h")}),
        ("rays", {"strategy_params.htf_bias.break_min":      _pct_step(htf_break_min, 0.30, 5, 0.0005, 0.02)}),
        ("rays", {"strategy_params.htf_bias.confirm_bars":   _choices(max(1, htf_confirm_bars-1), htf_confirm_bars, htf_confirm_bars+1)}),
        ("rays", {"strategy_params.htf_bias.hysteresis_bars":_choices( max(1, htf_hyst_bars-1), htf_hyst_bars, htf_hyst_bars+2)}),
        ("rays", {"strategy_params.htf_bias.cooldown_bars":  _choices( max(4, htf_cooldown-2), htf_cooldown, htf_cooldown+4)}),
        ("rays", {"strategy_params.htf_bias.rank_boost_long":  _pct_step(htf_rank_boost_l, 0.35, 5, 0.0, 1.0)}),
        ("rays", {"strategy_params.htf_bias.rank_boost_short": _pct_step(htf_rank_boost_s, 0.35, 5, 0.0, 1.0)}),
        ("rays", {"strategy_params.htf_bias.entry_gate":     _tight_bool(htf_entry_gate)}),
        ("rays", {"strategy_params.htf_bias.mom_confirm_min":_pct_step(htf_mom_min, 0.50, 5, 0.0, 0.05)}),
        ("rays", {"strategy_params.htf_bias.heat_relax_rr":  _choices(htf_heat_relax_rr, 0.05)}),
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
