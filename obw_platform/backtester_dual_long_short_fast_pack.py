# Compatibility shim for auto_tuner_dual_fast_pack.py
try:
    from backtester_dual_core_dynamic_v5 import simulate, pick_symbol_block
except Exception:
    from backtester_dual_core_dynamic_v2 import simulate, pick_symbol_block
