
from .base import StrategyBase, Signal, Adjust

class ShortTopGainersV12(StrategyBase):
    """
    SHORT-only strategy with two modes:
      - mode="breakdown" (default): short accelerating *losers* (max PF in our sweeps).
      - mode="fade_gainers":     short *top gainers* with overbought/volume filters (optional).
    Uses only cache-available features (dp6h, dp12h, atr_ratio, quote_volume, qv_24h, rsi, overbought_index).

    Key filters (from optimization on 1h):
      - min_vol_surge_mult ~ 1.2
      - min_atr_ratio ~ 0.01
      - min_momentum_sum ~ 0.06 (abs value)
      - time stop ~ 72h
      - MAE stop ~ 1.5 * ATR
      - optional momentum flip (mom_flip_thresh ~ 0.0..0.01)

    Universe:
      - qv_1h >= min_qv_1h
      - qv_24h >= min_qv_24h
    Rank:
      - mode="breakdown": prioritize most negative (dp6h+dp12h)
      - mode="fade_gainers": prioritize most positive (dp6h+dp12h)

    Entry (SHORT):
      - volume surge: quote_volume >= min_vol_surge_mult * (qv_24h / 24)
      - atr_ratio >= min_atr_ratio
      - mode="breakdown": (dp6h+dp12h) <= -min_momentum_sum
      - mode="fade_gainers": (dp6h+dp12h) >=  min_momentum_sum
                             and (rsi >= rsi_overbought or overbought_index >= obi_thresh if available)

    Manage:
      - time-based exit (max_hold_hours)
      - MAE in ATR
      - momentum flip exit: if (dp6h+dp12h) > -mom_flip_thresh
      - optional trailing: if profit >= trail_start_atr * ATR then tighten stop to trail_dist_atr * ATR
    """

    def universe(self, t, md_slice):
        min_qv24 = float(self.cfg.get("min_qv_24h", 200_000))
        min_qv1h = float(self.cfg.get("min_qv_1h", 10_000))
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
        mode = str(self.cfg.get("mode", "breakdown")).lower()
        scored = []
        for sym in symbols:
            row = md_slice[sym]
            dp6 = float(row.get("dp6h", 0.0) or 0.0)
            dp12= float(row.get("dp12h", 0.0) or 0.0)
            m = dp6 + dp12
            scored.append((sym, m))
        if mode == "fade_gainers":
            scored.sort(key=lambda x: x[1], reverse=True)  # strongest gainers first
        else:
            scored.sort(key=lambda x: x[1])                # most negative first
        top_n = int(self.cfg.get("top_n", 8))
        return [s for s,_ in scored[:top_n]]

    def entry_signal(self, t, sym, row, ctx):
        # SHORT only
        dp6 = float(row.get("dp6h", 0.0) or 0.0)
        dp12= float(row.get("dp12h", 0.0) or 0.0)
        mom_sum = dp6 + dp12

        atrr = float(row.get("atr_ratio", 0.0) or 0.0)
        qv1h = float(row.get("quote_volume", 0.0) or 0.0)
        qv24 = float(row.get("qv_24h", 0.0) or 0.0)
        rsi = float(row.get("rsi", 0.0) or 0.0)
        obi = float(row.get("overbought_index", 0.0) or 0.0)

        min_mom = float(self.cfg.get("min_momentum_sum", 0.06))
        min_atr = float(self.cfg.get("min_atr_ratio", 0.01))
        vol_mult= float(self.cfg.get("min_vol_surge_mult", 1.2))
        mode = str(self.cfg.get("mode", "breakdown")).lower()

        avg1h = (qv24/24.0) if qv24>0 else 0.0
        vol_ok = (avg1h>0 and qv1h >= vol_mult * avg1h)

        price = float(row.get("close", 0.0) or 0.0)
        if price <= 0.0:
            return None

        cond_atr = atrr >= min_atr
        go_short = False

        if mode == "fade_gainers":
            rsi_over = float(self.cfg.get("rsi_overbought", 70.0))
            obi_thresh= float(self.cfg.get("obi_overbought", 0.7))
            # If we don't know OBI scale, we make it optional: pass if either RSI or OBI confirms
            overbought_ok = (rsi >= rsi_over) or (obi >= obi_thresh)
            go_short = (mom_sum >= min_mom) and cond_atr and vol_ok and overbought_ok
        else:
            # breakdown (our highest-PF in sweeps): short strong down-momentum
            go_short = (mom_sum <= -min_mom) and cond_atr and vol_ok

        if not go_short:
            return None

        sl_mult = float(self.cfg.get("sl_atr_mult", 1.2))
        tp_mult = float(self.cfg.get("tp_atr_mult", 2.0))
        max_hold = int(self.cfg.get("max_hold_hours", 72))

        stop = price * (1.0 + sl_mult * atrr)
        take = price * (1.0 - tp_mult * atrr) if tp_mult>0 else None
        return Signal(side="SHORT", reason=f"short_{mode}", stop_price=stop, take_profit=take, max_hold_hours=max_hold,
                      tags={"mom_sum": mom_sum, "atr_ratio": atrr, "mode": mode})

    def manage_position(self, t, sym, pos, row, ctx):
        price = float(row.get("close", 0.0) or 0.0)
        if price <= 0.0:
            return Adjust(action="HOLD", reason="bad_price")

        atrr = float(row.get("atr_ratio", 0.0) or 0.0)
        dp6 = float(row.get("dp6h", 0.0) or 0.0)
        dp12= float(row.get("dp12h", 0.0) or 0.0)
        mom_sum = dp6 + dp12

        # time stop
        if pos.meta.get("max_hold_hours") is not None:
            elapsed = max(int((t - pos.entry_time).total_seconds() // 3600), 0)
            if elapsed >= int(pos.meta.get("max_hold_hours")):
                return Adjust(action="EXIT", reason="time_stop")

        # MAE stop in ATR units
        max_mae_mult = float(self.cfg.get("max_mae_atr_mult", 1.5))
        ret = (pos.entry_price - price) / max(pos.entry_price, 1e-12)  # short profit if positive
        if ret < - max_mae_mult * atrr:
            return Adjust(action="EXIT", reason="mae_break")

        # momentum flip exit (for SHORT, flip if momentum becomes less negative than -mom_flip)
        mom_flip = float(self.cfg.get("mom_flip_thresh", 0.0))
        if mom_sum > -mom_flip:
            return Adjust(action="EXIT", reason="mom_flip")

        # trailing stop after profit exceed threshold
        trail_start = float(self.cfg.get("trail_start_atr", 1.0))
        trail_dist  = float(self.cfg.get("trail_dist_atr", 1.0))
        if atrr > 0.0 and trail_start > 0.0:
            up = (pos.entry_price - price) / max(pos.entry_price, 1e-12)
            if up >= trail_start * atrr:
                new_stop = price * (1.0 + trail_dist * atrr)
                if pos.stop_price is None or new_stop < pos.stop_price:
                    return Adjust(action="MOVE_SL", reason="trail_down", new_stop=new_stop)

        return Adjust(action="HOLD", reason="hold_ok")
