"""tuner_plan_greedy_breakout_universe.py
Tuning plan for exit parameters of GreedyBreakoutUniverse strategy.
Focuses on optional exit rules like time in trade, indicator reversals and heat drop.
Compatible with auto_tuner_rays2grid_v3_fix.py.
"""

def default_plan(limit_bars: int = None):
    """Return tuning plan exploring new exit parameters.

    Parameters
    ----------
    limit_bars : int, optional
        If provided, can be used to adjust plan size (not used here).
    """
    rays = [
        ("rays", {"max-bars": [0, 24, 28, 32]}),
        ("rays", {"exit-macd": [False, True]}),
        ("rays", {"adx-exit": [0.0, 15.0, 20.0, 25.0]}),
        ("rays", {"rsi-exit-long": [0.0, 40.0, 45.0, 50.0]}),
        ("rays", {"rsi-exit-short": [0.0, 50.0, 55.0, 60.0]}),
        ("rays", {"heat-exit": [0.0, 0.12, 0.15, 0.18]}),
    ]

    grid1 = ("grid", {
        "max-bars": "around:4",
        "exit-macd": [False, True],
        "adx-exit": "around:2",
        "rsi-exit-long": "around:2",
        "rsi-exit-short": "around:2",
        "heat-exit": "around:0.02",
    })

    grid2 = ("grid", {
        "max-bars": "around:2",
        "exit-macd": [False, True],
        "adx-exit": "around:1",
        "rsi-exit-long": "around:1",
        "rsi-exit-short": "around:1",
        "heat-exit": "around:0.01",
    })

    return rays + [grid1, grid2]
