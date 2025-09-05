
# tuner_plan_avaai.py — refreshed after latest RAYS runs on cfg_avaai_t5m5000_3.yaml
# Centers & hints from results:
#   - strategy_params.adx_threshold: best around 25 (25 > 20 >> 15)
#   - min_momentum_sum: best near 0.022 (0.022 > 0.024 in equity; DD acceptable)
# Keep scanning TP/SL, ATR filter, liquidity, width (top-n), sizing.
#
# Compatible with: auto_tuner_rays2grid_v3_fix.py (RAYS -> GRID refinement)
# Notation:
#   - "rays" steps take explicit value lists
#   - "grid" uses "around:<step>" to refine around each param's current *best*
# Aliases the runner understands:
#   min-mom <-> min_momentum_sum, min-atr <-> min_atr_ratio, top-n <-> strategy_params.top_n

def _seq(start, stop, step):
    vals, x = [], float(start)
    if step == 0: step = (stop - start) / 10.0 if stop != start else 1.0
    if start <= stop:
        while x <= float(stop) + 1e-12:
            vals.append(round(x, 10)); x += float(step)
    else:
        while x >= float(stop) - 1e-12:
            vals.append(round(x, 10)); x -= float(abs(step))
    return vals

def default_plan(limit_bars: int = None):
    # Fast sanity when limit_bars is tiny
    if limit_bars is not None and limit_bars < 400:
        return [
            ("rays", {"strategy_params.adx_threshold": [22,24,25,26,28]}),
            ("rays", {"min-mom": [0.020,0.021,0.022,0.023,0.024]}),
            ("rays", {"strategy_params.tp_atr_mult": [3.4,3.7,3.9,4.1]}),
            ("rays", {"strategy_params.sl_atr_mult": [0.98,1.02,1.06]}),
            ("rays", {"min-atr": [0.0004,0.0006,0.0008]}),
            ("rays", {"top-n": [6,7,8]}),
            ("rays", {"strategy_params.min_qv_24h": [150000, 250000, 400000]}),
            ("rays", {"strategy_params.min_qv_1h":  [8000, 12000, 20000]}),
            ("rays", {"position_notional": [40,60,80]}),
            ("rays", {"portfolio.max_notional_frac": [0.6,0.7,0.8]}),
            ("rays", {"side": ["LONG","BOTH"]}),
            ("rays", {"open_on_heat": [False]}),
        ]

    # Main horizon (≈5000 bars)
    rays = [
        # 1) Structure/filters near the discovered sweet spots
        ("rays", {"strategy_params.adx_threshold": [22,24,25,26,28]}),
        ("rays", {"min-mom": [0.018,0.020,0.021,0.022,0.023,0.024,0.026]}),
        ("rays", {"min-atr": _seq(0.0003, 0.0010, 0.0001)}),

        # 2) Exit/stop geometry
        ("rays", {"strategy_params.tp_atr_mult": _seq(3.3, 4.3, 0.1)}),
        ("rays", {"strategy_params.sl_atr_mult": _seq(0.96, 1.12, 0.02)}),

        # 3) Liquidity gates (loosen slightly to widen allow-list if it's capped)
        ("rays", {"strategy_params.min_qv_24h": [150000, 200000, 300000, 500000]}),
        ("rays", {"strategy_params.min_qv_1h":  [8000, 10000, 15000, 20000]}),

        # 4) Universe width & side
        ("rays", {"top-n": [6,7,8,9,10]}),
        ("rays", {"side": ["LONG","BOTH"]}),

        # 5) Sizing controls
        ("rays", {"position_notional": [40, 50, 60, 80]}),
        ("rays", {"portfolio.max_notional_frac": [0.6, 0.7, 0.8]}),

        # 6) Structural length if applicable
        ("rays", {"strategy_params.length": [8,10,12,14,16]}),

        # Force classic entry
        ("rays", {"open_on_heat": [False]}),
    ]

    # GRID-1: medium refinement around best combo
    grid1 = ("grid", {
        "strategy_params.adx_threshold": "around:1",
        "min-mom": "around:0.001",
        "min-atr": "around:0.0001",

        "strategy_params.tp_atr_mult": "around:0.10",
        "strategy_params.sl_atr_mult": "around:0.02",

        "strategy_params.min_qv_24h": "around:100000",
        "strategy_params.min_qv_1h":  "around:5000",

        "top-n": "around:1",
        "position_notional": "around:10",
        "portfolio.max_notional_frac": "around:0.05",

        "strategy_params.length": "around:2",
        "open_on_heat": [False],
    })

    # GRID-2: fine refinement
    grid2 = ("grid", {
        "strategy_params.adx_threshold": "around:0.5",
        "min-mom": "around:0.0005",
        "min-atr": "around:0.00005",

        "strategy_params.tp_atr_mult": "around:0.05",
        "strategy_params.sl_atr_mult": "around:0.01",

        "strategy_params.min_qv_24h": "around:50000",
        "strategy_params.min_qv_1h":  "around:2500",

        "top-n": "around:1",
        "position_notional": "around:5",
        "portfolio.max_notional_frac": "around:0.02",

        "strategy_params.length": "around:1",
        "open_on_heat": [False],
    })

    return rays + [grid1, grid2]
