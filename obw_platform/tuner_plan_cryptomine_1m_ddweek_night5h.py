#!/usr/bin/env python3
# tuner_plan_cryptomine_1m_ddweek_night5h.py
# Night plan (~5h). Starts from RoyalQ-style baseline and explores safety-first.
# Uses DELTAS around baseline values in your YAML.
#
# Targets:
# - avoid configs that stay in drawdown for long (filter in tuner)
# - keep DD under ~50% (filter in tuner)
#
GRID_VALUES_ARE_DELTAS = True

def default_plan(limit_bars: int = None):
    return [
        # ---- Phase 1: exits ----
        ("rays", {"tp":    [-0.15, -0.08, 0.0, +0.08, +0.15]}),
        ("rays", {"subtp": [-0.40, -0.20, 0.0, +0.20, +0.40]}),  # your screenshot hints subtp can be lower (e.g. 0.5%)
        ("rays", {"cb":    [-0.10, -0.05, 0.0, +0.05, +0.10]}),

        # ---- Phase 2: DCA pacing (crucial for “recover within a week”) ----
        ("rays", {"lin-drop": [-0.30, -0.15, 0.0, +0.15, +0.30]}),
        ("rays", {"fills":    [-3, -1, 0, +1, +3]}),
        
        # ---- Phase 3: ladder shape (drops + multipliers) ----
        ("rays", {"d1": [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ("rays", {"d2": [-0.08, -0.04, 0.0, +0.04, +0.08]}),
        ("rays", {"d3": [-0.12, -0.06, 0.0, +0.06, +0.12]}),
        ("rays", {"m2": [-0.40, -0.20, 0.0, +0.20, +0.40]}),
        ("rays", {"m4": [-0.80, -0.40, 0.0, +0.40, +0.80]}),
        ("rays", {"m5": [-1.00, -0.50, 0.0, +0.50, +1.00]}),

        # ---- Phase 4: focused grid around the best ----
        ("grid", {
            "tp":       "around:0.10",
            "subtp":    "around:0.20",
            "cb":       "around:0.05",
            "lin-drop": "around:0.15",
            "fills":    "around:2",
        }),
    ]
