from .base import StrategyBase, Signal, Adjust

class FadeSpikes(StrategyBase):
    """
    Контртренд на спайках: SHORT коли вола/обсяг/осцилятори екстремальні,
    швидкі виходи і короткий hold.
    """

    def _liq_ok(self, r):
        return float(r.get("qv_24h",0) or 0)>=float(self.cfg.get("min_qv_24h",2e5)) and \
               float(r.get("quote_volume",0) or 0)>=float(self.cfg.get("min_qv_1h",1e4))

    def universe(self, t, md):
        return [s for s,r in md.items() if self._liq_ok(r)]

    def rank(self, t, md, symbols):
        # шукаємо "перегріті" — високе (dp6h+dp12h) + vol_surge
        items=[]
        for s in symbols:
            r=md[s]
            mom=(float(r.get("dp6h",0) or 0.0)+float(r.get("dp12h",0) or 0.0))
            vs=float(r.get("vol_surge_mult",1.0) or 1.0)
            items.append((s, mom*vs))
        items.sort(key=lambda x:x[1], reverse=True)
        return [s for s,_ in items[:int(self.cfg.get("top_n",6))]]

    def _overbought_hits(self, r):
        hits=0
        if float(r.get("overbought_index",0) or 0.0) >= float(self.cfg.get("min_ob",75)): hits+=1
        if float(r.get("rsi",0) or 0.0) >= float(self.cfg.get("min_rsi",60)): hits+=1
        if float(r.get("stochastic",0) or 0.0) >= float(self.cfg.get("min_stoch",70)): hits+=1
        if float(r.get("mfi",0) or 0.0) >= float(self.cfg.get("min_mfi",65)): hits+=1
        return hits

    def entry_signal(self, t, s, r, ctx):
        price=float(r.get("close",0) or 0.0)
        if price<=0 or not self._liq_ok(r): return None
        atr=float(r.get("atr_ratio",0) or 0.0)
        vs=float(r.get("vol_surge_mult",1.0) or 1.0)
        mom=(float(r.get("dp6h",0) or 0.0)+float(r.get("dp12h",0) or 0.0))

        if vs < float(self.cfg.get("min_vol_surge_mult",1.25)): return None
        if mom < float(self.cfg.get("min_momentum_sum",0.10)): return None
        if self._overbought_hits(r) < int(self.cfg.get("min_overbought_hits",2)): return None

        sl=float(self.cfg.get("sl_atr_mult",1.4))*atr
        tp=float(self.cfg.get("tp_atr_mult",2.0))*atr
        stop=price*(1.0+sl); take=price*(1.0-tp)
        return Signal(side="SHORT", reason="fade_spike", stop_price=stop, take_profit=take,
                      max_hold_hours=int(self.cfg.get("max_hold_hours",24)))

    def manage_position(self, t, s, pos, r, ctx):
        price=float(r.get("close",0) or 0.0); atr=float(r.get("atr_ratio",0) or 0.0)
        if price<=0: return Adjust(action="HOLD", reason="bad_price")

        # короткий time exit
        if pos.meta.get("max_hold_hours") is not None:
            elapsed=max(int((t-pos.entry_time).total_seconds()//3600),0)
            if elapsed>=int(pos.meta["max_hold_hours"]): return Adjust(action="EXIT", reason="time_exit")

        # MAE
        max_mae=float(self.cfg.get("max_mae_atr_mult",1.2))
        ret=(pos.entry_price-price)/max(pos.entry_price,1e-12) if pos.side=="SHORT" else (price-pos.entry_price)/max(pos.entry_price,1e-12)
        if ret < -max_mae*atr: return Adjust(action="EXIT", reason="mae_break")

        # momentum flip: якщо імпульс згас
        mom=float(r.get("dp6h",0) or 0.0)+float(r.get("dp12h",0) or 0.0)
        if pos.side=="SHORT" and mom < float(self.cfg.get("mom_flip_thresh",0.02)):
            return Adjust(action="EXIT", reason="mom_faded")

        return Adjust(action="HOLD", reason="hold")
