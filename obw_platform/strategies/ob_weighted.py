
from .base import StrategyBase
def clamp(v, lo, hi): return max(lo, min(hi, v))

class OBWeighted(StrategyBase):
    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self.top_n = int(self.cfg.get("top_n", 4))

    def _weighted_ob(self, row):
        rsi = float(row.get("rsi") or 0.0)
        stoch = float(row.get("stochastic") or 0.0)
        mfi = float(row.get("mfi") or 0.0)
        hcp = float(row.get("highclose_pct") or 0.0)
        wr = float(self.cfg.get("w_rsi", 0.4))
        ws = float(self.cfg.get("w_stoch", 0.2))
        wm = float(self.cfg.get("w_mfi", 0.2))
        wh = float(self.cfg.get("w_hcp", 0.1))
        denom = wr+ws+wm+wh if (wr+ws+wm+wh)>0 else 1.0
        return clamp((wr*rsi + ws*stoch + wm*mfi + wh*hcp)/denom, 0.0, 100.0)

    def universe(self, t, md_slice):
        min_qv24 = float(self.cfg.get("min_qv_24h", 100000.0))
        min_qv1h = float(self.cfg.get("min_qv_1h", 10000.0))
        return [s for s,r in md_slice.items()
                if (r.get("qv_24h") or 0.0)>=min_qv24 and (r.get("quote_volume") or 0.0)>=min_qv1h and (r.get("close") or 0.0)>0]

    def rank(self, t, md_slice, symbols):
        symbols = [s for s in symbols if md_slice[s].get("close",0)>0]
        symbols.sort(key=lambda s: self._weighted_ob(md_slice[s]), reverse=True)
        return symbols[: self.top_n]

    def entry_signal(self, t, sym, row, ctx):
        min_ob = float(self.cfg.get("min_ob", 85.0))
        max_atr_ratio = float(self.cfg.get("max_atr_ratio", 0.03))
        risk_pct = float(self.cfg.get("risk_pct", 0.04))
        atrr = float(row.get("atr_ratio") or 0.0)
        if not (atrr > 0 and atrr <= max_atr_ratio):
            return None
        obw = self._weighted_ob(row)
        if obw >= min_ob:
            price = float(row["close"])
            stop_price = price * (1.0 + risk_pct)
            return {"side":"SHORT","reason":f"obw={obw:.1f}","stop_price":stop_price,
                    "take_profit":None,"max_hold_hours":int(self.cfg.get("hold_hours", 60))}
        return None

    def manage_position(self, t, sym, pos, row, ctx):
        max_hold = int(pos.meta.get("max_hold_hours", self.cfg.get("hold_hours", 60)))
        if (t - pos.entry_time).total_seconds() >= max_hold*3600:
            return {"action":"EXIT","reason":"time_exit"}
        return {"action":"HOLD","reason":"hold_ok"}
