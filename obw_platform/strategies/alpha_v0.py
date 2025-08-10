from .base import StrategyBase, Signal, Adjust
import numpy as np

class AlphaV0(StrategyBase):
    def universe(self, t, md_slice):
        min_qv24 = self.cfg.get("min_qv_24h", 100_000)
        min_qv1h = self.cfg.get("min_qv_1h", 10_000)
        keep = []
        for sym, row in md_slice.items():
            qv24 = row.get("qv_24h")
            qv1h = row.get("quote_volume")
            if qv24 is None or qv1h is None:
                continue
            if qv24 >= min_qv24 and qv1h >= min_qv1h:
                keep.append(sym)
        return keep

    def rank(self, t, md_slice, symbols):
        def score(sym):
            r = md_slice[sym]
            ob = r.get("overbought_index", 0.0) or 0.0
            dp6 = r.get("dp6h", 0.0) or 0.0
            dp12= r.get("dp12h", 0.0) or 0.0
            return 0.7*ob + 0.3*max(dp6+dp12, 0.0)
        symbols = [s for s in symbols if md_slice[s].get("close", 0) and md_slice[s]["close"]>0]
        symbols.sort(key=score, reverse=True)
        top_n = int(self.cfg.get("top_n", 5))
        return symbols[:top_n]

    def entry_signal(self, t, sym, row, ctx):
        side_pref = self.cfg.get("side", "SHORT")
        ob_min = float(self.cfg.get("min_ob", 80))
        risk_pct = float(self.cfg.get("risk_pct", 0.03))
        atrr= float(row.get("atr_ratio", 0.0) or 0.0)
        max_atr_ratio = float(self.cfg.get("max_atr_ratio", 0.05))
        dp6 = float(row.get("dp6h", 0.0) or 0.0)
        dp12= float(row.get("dp12h", 0.0) or 0.0)
        price = float(row["close"])

        if side_pref in ("SHORT","BOTH"):
            ob = row.get("overbought_index")
            if ob is not None and ob >= ob_min and dp6 >= 0 and dp12 >= 0 and atrr <= max_atr_ratio:
                stop_price = price * (1 + risk_pct)
                tp_mult = float(self.cfg.get("tp_atr_mult", 1.2))
                take_profit = price * (1 - tp_mult * atrr) if atrr>0 else None
                return Signal(side="SHORT", reason="OB+mom-short", stop_price=stop_price, take_profit=take_profit, max_hold_hours=self.cfg.get("hold_hours", 48))

        if side_pref in ("LONG","BOTH"):
            ob = row.get("overbought_index")
            if ob is not None and ob <= (100 - ob_min) and dp6 >= 0 and dp12 >= 0 and atrr <= max_atr_ratio:
                stop_price = price * (1 - risk_pct)
                tp_mult = float(self.cfg.get("tp_atr_mult", 1.2))
                take_profit = price * (1 + tp_mult * atrr) if atrr>0 else None
                return Signal(side="LONG", reason="OS+mom-long", stop_price=stop_price, take_profit=take_profit, max_hold_hours=self.cfg.get("hold_hours", 48))

        return None

    def manage_position(self, t, sym, pos, row, ctx):
        max_hold = int(pos.meta.get("max_hold_hours", self.cfg.get("hold_hours", 48)))
        if (t - pos.entry_time).total_seconds() >= max_hold*3600:
            return Adjust(action="EXIT", reason="time_exit")

        atrr = float(row.get("atr_ratio", 0.0) or 0.0)
        price = float(row.get("close", 0.0) or 0.0)
        if price <= 0:
            return Adjust(action="HOLD", reason="no_price")

        max_mae_mult = float(self.cfg.get("max_mae_atr_mult", 1.2))
        if pos.side == "LONG":
            ret = (price - pos.entry_price)/max(pos.entry_price, 1e-12)
            if ret < - max_mae_mult * atrr:
                return Adjust(action="EXIT", reason="mae_break")
        else:
            ret = (pos.entry_price - price)/max(pos.entry_price, 1e-12)
            if ret < - max_mae_mult * atrr:
                return Adjust(action="EXIT", reason="mae_break")

        dp6 = float(row.get("dp6h", 0.0) or 0.0)
        dp12= float(row.get("dp12h", 0.0) or 0.0)
        mom_sum = dp6 + dp12
        mom_flip = float(self.cfg.get("mom_flip_thresh", 0.0))
        if mom_sum < mom_flip:
            return Adjust(action="EXIT", reason="mom_flip")

        return Adjust(action="HOLD", reason="hold_ok")