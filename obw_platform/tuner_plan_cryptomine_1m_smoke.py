#!/usr/bin/env python3
# tuner_plan_cryptomine_1m_smoke.py
# Super-narrow plan: should finish quickly (few minutes) to verify wiring.
#
# Note: GRID_VALUES_ARE_DELTAS=True means numeric lists in grid are interpreted as deltas around current.
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    # 3 rays + 1 tiny grid (~3*3 + 3*3*3 = ~33 backtests)
    return [
        ("rays", {"tp":     ["around:0.10"]}),   # tpPercent
        ("rays", {"subtp":  ["around:0.10"]}),   # subSellTPPercent
        ("rays", {"cb":     ["around:0.05"]}),   # callbackPercent
        ("grid", {
            "tp":    "around:0.05",
            "subtp": "around:0.05",
            "cb":    "around:0.02",
        }),
    ]
