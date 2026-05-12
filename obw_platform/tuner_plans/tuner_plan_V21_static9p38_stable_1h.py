#!/usr/bin/env python3
"""V21 static9p38 stability-focused one-hour tuning plan.

Designed for `auto_tuner_dual_fast_pack.py` on the ENA 30s NPZ. Values are
deltas from `V21_strict_trend_stable_live_static9p38.yaml`.
"""

GRID_VALUES_ARE_DELTAS = True


def default_plan(limit_bars: int = None):
    return [
        # Protect the confirmed live behavior first: tune exits narrowly.
        ("rays", {
            "strategy_params_long.tpPercent": [-0.08, -0.05, -0.03, -0.015, 0.0, 0.015, 0.03, 0.05],
        }),
        ("rays", {
            "strategy_params_short.tpPercent": [-0.06, -0.04, -0.02, -0.01, 0.0, 0.01, 0.02, 0.04],
        }),
        ("rays", {
            "strategy_params_long.subSellTPPercent": [-0.12, -0.08, -0.05, -0.025, 0.0, 0.025, 0.05, 0.08],
        }),
        ("rays", {
            "strategy_params_short.subSellTPPercent": [-0.10, -0.07, -0.04, -0.02, 0.0, 0.02, 0.04, 0.07],
        }),
        ("rays", {
            "strategy_params_long.callbackPercent": [-0.03, -0.02, -0.01, -0.005, 0.0, 0.005, 0.01, 0.02],
        }),
        ("rays", {
            "strategy_params_short.callbackPercent": [-0.04, -0.025, -0.015, -0.0075, 0.0, 0.0075, 0.015, 0.025],
        }),

        # DCA spacing and exposure caps. Keep ranges narrow because V21 already
        # has good live behavior and the goal is not to rediscover a new algo.
        ("rays", {
            "strategy_params_long.linearDropPercent": [-0.02, -0.012, -0.006, 0.0, 0.006, 0.012, 0.02],
        }),
        ("rays", {
            "strategy_params_short.linearRisePercent": [-0.05, -0.035, -0.02, -0.01, 0.0, 0.01, 0.02, 0.035],
        }),
        ("rays", {
            "strategy_params_long.maxLongInvestPct": [-0.35, -0.20, -0.10, 0.0, 0.10, 0.20],
        }),
        ("rays", {
            "strategy_params_short.maxShortInvestPct": [-0.45, -0.25, -0.10, 0.0, 0.10, 0.20],
        }),

        # Small leg-local interactions.
        ("grid", {
            "strategy_params_long.tpPercent": "around:0.015",
            "strategy_params_long.subSellTPPercent": "around:0.025",
            "strategy_params_long.callbackPercent": "around:0.0075",
            "strategy_params_long.linearDropPercent": "around:0.006",
        }),
        ("grid", {
            "strategy_params_short.tpPercent": "around:0.0125",
            "strategy_params_short.subSellTPPercent": "around:0.025",
            "strategy_params_short.callbackPercent": "around:0.0075",
            "strategy_params_short.linearRisePercent": "around:0.01",
        }),
        ("grid", {
            "strategy_params_long.maxLongInvestPct": "around:0.10",
            "strategy_params_short.maxShortInvestPct": "around:0.10",
            "strategy_params_long.tpPercent": "around:0.01",
            "strategy_params_short.tpPercent": "around:0.01",
        }),
    ]
