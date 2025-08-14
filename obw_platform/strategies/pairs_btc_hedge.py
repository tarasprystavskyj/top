from .base import StrategyBase, Signal, Adjust

class PairsBTCHedge(StrategyBase):
    """
    Дві ноги: LONG на сильнішому і SHORT на BTC (або навпаки) за порогом різниці імпульсів.
    Спред-сигнал: (dp6h+dp12h)_ALT - (dp6h+dp12h)_BTC.
    """

    def _liq_ok(self, r):
        return float(r.get("qv_24h",0) or 0)>=float(self.cfg.get("min_qv_24h",2e5)) and \
               float(r.get("quote_volume",0) or 0)>=float(self.cfg.get("min_qv_1h",1e4))

    def universe(self, t, md):
        return [s for s,r in md.items() if self._liq_ok(r)]

    def rank(self, t, md, symbols):
        # беремо ALT-и, BTC тикер з конфіга
        btc = self.cfg.get("btc_symbol","BTC-USDT")
        if btc not in md: return []
        mom_btc=(float(md[btc].get("dp6h",0) or 0.0)+float(md[btc].get("dp12h",0) or 0.0))
        items=[]
        for s in symbols:
            if s==btc: continue
            r=md[s]
            mom=(float(r.get("dp6h",0) or 0.0)+float(r.get("dp12h",0) or 0.0)) - mom_btc
            items.append((s, mom))
        items.sort(key=lambda x:x[1], reverse=True)
        return [s for s,_ in items[:int(self.cfg.get("top_n",3))]]

    def entry_signal(self, t, s, r, ctx):
        # Відкриваємо спред: LONG на ALT та SHORT на BTC, якщо mom_diff > thresh
        btc = self.cfg.get("btc_symbol","BTC-USDT")
        row_btc = ctx.md.get(btc)
        if not row_btc: return None

        mom_alt=(float(r.get("dp6h",0) or 0.0)+float(r.get("dp12h",0) or 0.0))
        mom_btc=(float(row_btc.get("dp6h",0) or 0.0)+float(row_btc.get("dp12h",0) or 0.0))
        diff = mom_alt - mom_btc

        if diff < float(self.cfg.get("min_spread_mom",0.08)): return None
        if float(r.get("atr_ratio",0) or 0.0) < float(self.cfg.get("min_atr_ratio",0.02)): return None

        # Ця стратегія вимагає дві ноги — сигналимо лише для ALT; BTC відкриває ядро як окремий trade?
        # Простий підхід: повернути LONG сигнал по ALT, а в manage_position покласти хедж наказом.
        price=float(r.get("close",0) or 0.0)
        atr=float(r.get("atr_ratio",0) or 0.0)
        sl=float(self.cfg.get("sl_atr_mult",1.2))*atr
        tp=float(self.cfg.get("tp_atr_mult",2.0))*atr
        stop=price*(1.0-sl); take=price*(1.0+tp)
        return Signal(side="LONG", reason="pairs_open", stop_price=stop, take_profit=take,
                      max_hold_hours=int(self.cfg.get("max_hold_hours",72)),
                      tags={"hedge": btc, "hedge_side": "SHORT"})

    def manage_position(self, t, s, pos, r, ctx):
        # Зверніть увагу: повний мікро-хедж (створення другої ноги) краще робити на рівні ядра/портфоліо.
        price=float(r.get("close",0) or 0.0); atr=float(r.get("atr_ratio",0) or 0.0)
        if price<=0: return Adjust(action="HOLD", reason="bad_price")

        # time exit
        if pos.meta.get("max_hold_hours") is not None:
            elapsed=max(int((t-pos.entry_time).total_seconds()//3600),0)
            if elapsed>=int(pos.meta["max_hold_hours"]): return Adjust(action="EXIT", reason="time_exit")

        # MAE
        max_mae=float(self.cfg.get("max_mae_atr_mult",1.4))
        ret=(price-pos.entry_price)/max(pos.entry_price,1e-12)
        if ret < -max_mae*atr: return Adjust(action="EXIT", reason="mae_break")

        # Змикання спреду: якщо mom_diff < thresh_close — закрити
        btc = self.cfg.get("btc_symbol","BTC-USDT"); row_btc = ctx.md.get(btc)
        if row_btc:
            mom_alt=(float(r.get("dp6h",0) or 0.0)+float(r.get("dp12h",0) or 0.0))
            mom_btc=(float(row_btc.get("dp6h",0) or 0.0)+float(row_btc.get("dp12h",0) or 0.0))
            diff = mom_alt - mom_btc
            if diff < float(self.cfg.get("close_spread_mom",0.02)):
                return Adjust(action="EXIT", reason="spread_closed")

        return Adjust(action="HOLD", reason="hold")
