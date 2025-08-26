# strategies/breakout_avaai_adapter.py
# Adapter for backtester_core_speed3.py: provides a simple entry_signal(t, sym, row, ctx)
# Uses dp6h + dp12h momentum to choose LONG/SHORT when side='BOTH' and bias='AUTO'.
# IMPORTANT: Gating (minimum ATR, momentum, liquidity) is handled by the backtester prefilters.
# This adapter only picks side; TP/SL are ATR multiples handled in the core.
#
# Row keys available (from the core):
#   close, atr_ratio, dp6h, dp12h, quote_volume, qv_24h
#
# Suggested YAML keys:
#   strategy_class: strategies.breakout_avaai_adapter.BreakoutAVAAI
#   strategy_params:
#     top_n: 12
#     side: BOTH | LONG | SHORT   # core's side preference (gating path differs for SHORT)
#     tp_atr_mult: 2.6
#     sl_atr_mult: 1.0
#     bias: AUTO | LONG | SHORT   # adapter-side bias; AUTO uses momentum sign
#     mom_threshold: 0.0          # adapter-side threshold for AUTO flip
#
class BreakoutAVAAI:
    class _Sig:
        __slots__ = ("side",)

    def __init__(self, cfg):
        sp = (cfg or {}).get("strategy_params", {}) or {}
        # Adapter behavior
        self.bias = str(sp.get("bias", "AUTO")).upper()  # AUTO | LONG | SHORT
        # Use adapter threshold or fall back to core's min_momentum_sum
        self.mom_threshold = float(sp.get("mom_threshold", (cfg or {}).get("min_momentum_sum", 0.0)))
        # Core will also read 'side' and enforce gating path; we read it only as a fallback
        self.side_pref = str(sp.get("side", "BOTH")).upper()

    def entry_signal(self, t, sym, row, ctx=None):
        # Decide a side; actual gating is done in the core via prefilters.
        dp6 = float(row.get("dp6h", 0.0) or 0.0)
        dp12 = float(row.get("dp12h", 0.0) or 0.0)
        mom_sum = dp6 + dp12

        sig = self._Sig()

        if self.bias == "LONG":
            sig.side = "LONG"
            return sig
        if self.bias == "SHORT":
            sig.side = "SHORT"
            return sig

        # AUTO: pick side by momentum sign with a dead-zone around 0 (+/- mom_threshold)
        thr = abs(self.mom_threshold)
        if mom_sum >= thr:
            sig.side = "LONG"
        elif mom_sum <= -thr:
            sig.side = "SHORT"
        else:
            # In the dead-zone, follow the core preference (BOTH->LONG path, SHORT->SHORT path)
            sig.side = "LONG" if self.side_pref in ("BOTH","LONG") else "SHORT"
        return sig
