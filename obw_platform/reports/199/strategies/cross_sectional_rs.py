from .base import StrategyBase, Signal, Adjust

class CrossSectionalRS(StrategyBase):
    """
    Cross-Sectional Relative Strength (C2-профіль)
    Ідея: на кожному кроці беремо топ-N альтів за короткостроковим моментумом
    (dp6h + dp12h), але пропускаємо тільки ті, де:
      • достатня волатильність (ATR-фільтр),
      • є сплеск обсягу (qv_1h вище середнього годинного за 24h),
      • «ширина ринку» (breadth) не надто низька.
    Входи — LONG-only (для C2). Виходи — time-based, momentum-flip, MAE по ATR,
    опційний трейлінг.
    """

    # -----------------------------
    #  Universe: ліквідні інструменти
    # -----------------------------
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

    # -----------------------------
    #  Rank: dp6h + dp12h (з ATR-фільтром)
    #  + підрахунок breadth (частка «позитивних» після ATR-фільтра)
    # -----------------------------
    def rank(self, t, md_slice, symbols):
        use_atr = float(self.cfg.get("min_atr_ratio", 0.012))
        items = []
        for sym in symbols:
            row  = md_slice[sym]
            dp6  = float(row.get("dp6h", 0.0) or 0.0)
            dp12 = float(row.get("dp12h", 0.0) or 0.0)
            atrr = float(row.get("atr_ratio", 0.0) or 0.0)
            score = (dp6 + dp12) if atrr >= use_atr else -1e9
            items.append((sym, score, atrr))

        items.sort(key=lambda x: x[1], reverse=True)
        valid = [1 for _, s, _ in items if s > 0]
        total = len([1 for _, s, _ in items if s > -1e9/2])
        self._last_breadth = (sum(valid)/max(total, 1)) if total > 0 else 0.0

        top_n = int(self.cfg.get("top_n", 5))
        return [sym for sym, score, _ in items[:top_n]]

    # -----------------------------
    #  Допоміжний: перевірка сплеску обсягу
    # -----------------------------
    def _vol_ok(self, row, mult):
        qv24 = float(row.get("qv_24h", 0.0) or 0.0)
        qv1h = float(row.get("quote_volume", 0.0) or 0.0)
        avg1h = (qv24 / 24.0) if qv24 > 0 else 0.0
        return (avg1h > 0) and (qv1h >= mult * avg1h)

    # -----------------------------
    #  Entry: LONG/SHORT (для C2 — LONG-only),
    #  умови: momentum + ATR + vol-surge + breadth
    # -----------------------------
    def entry_signal(self, t, sym, row, ctx):
        side_pref = str(self.cfg.get("side", "BOTH")).upper()

        dp6  = float(row.get("dp6h", 0.0) or 0.0)
        dp12 = float(row.get("dp12h", 0.0) or 0.0)
        atrr = float(row.get("atr_ratio", 0.0) or 0.0)
        price = float(row.get("close", 0.0) or 0.0)
        if price <= 0.0:
            return None

        mom_sum     = dp6 + dp12
        min_mom     = float(self.cfg.get("min_momentum_sum", 0.08))
        min_atr     = float(self.cfg.get("min_atr_ratio", 0.016))
        vol_mult    = float(self.cfg.get("min_vol_surge_mult", 1.20))
        min_breadth = float(self.cfg.get("min_breadth", 0.0))

        if not self._vol_ok(row, vol_mult):
            return None
        if atrr < min_atr:
            return None
        breadth = getattr(self, "_last_breadth", 1.0)
        if breadth < min_breadth:
            return None

        go_long  = (mom_sum >= min_mom)  and side_pref in ("BOTH", "LONG")
        go_short = (mom_sum <= -min_mom) and side_pref in ("BOTH", "SHORT")

        sl_mult  = float(self.cfg.get("sl_atr_mult", 1.3))
        tp_mult  = float(self.cfg.get("tp_atr_mult", 2.2))
        max_hold = int(self.cfg.get("max_hold_hours", 96))

        if go_long:
            stop = price * (1.0 - sl_mult * atrr)
            take = price * (1.0 + tp_mult * atrr) if tp_mult > 0 else None
            return Signal(
                side="LONG", reason="cs_long",
                stop_price=stop, take_profit=take, max_hold_hours=max_hold,
                tags={"mom_sum": mom_sum, "atr_ratio": atrr, "breadth": breadth}
            )
        if go_short:
            stop = price * (1.0 + sl_mult * atrr)
            take = price * (1.0 - tp_mult * atrr) if tp_mult > 0 else None
            return Signal(
                side="SHORT", reason="cs_short",
                stop_price=stop, take_profit=take, max_hold_hours=max_hold,
                tags={"mom_sum": mom_sum, "atr_ratio": atrr, "breadth": breadth}
            )
        return None

    # -----------------------------
    #  Manage: швидкі виходи при деградації edge
    # -----------------------------
    def manage_position(self, t, sym, pos, row, ctx):
        price = float(row.get("close", 0.0) or 0.0)
        if price <= 0.0:
            return Adjust(action="HOLD", reason="bad_price")

        atrr = float(row.get("atr_ratio", 0.0) or 0.0)
        dp6  = float(row.get("dp6h", 0.0) or 0.0)
        dp12 = float(row.get("dp12h", 0.0) or 0.0)
        mom_sum = dp6 + dp12

        # time-based
        if pos.meta.get("max_hold_hours") is not None:
            elapsed = max(int((t - pos.entry_time).total_seconds() // 3600), 0)
            if elapsed >= int(pos.meta.get("max_hold_hours")):
                return Adjust(action="EXIT", reason="time_stop")

        # MAE (жорсткий стоп за ATR)
        max_mae_mult = float(self.cfg.get("max_mae_atr_mult", 1.6))
        if pos.side == "LONG":
            ret = (price - pos.entry_price) / max(pos.entry_price, 1e-12)
            if ret < -max_mae_mult * atrr:
                return Adjust(action="EXIT", reason="mae_break")
        else:
            ret = (pos.entry_price - price) / max(pos.entry_price, 1e-12)
            if ret < -max_mae_mult * atrr:
                return Adjust(action="EXIT", reason="mae_break")

        # momentum flip
        mom_flip = float(self.cfg.get("mom_flip_thresh", 0.02))
        if pos.side == "LONG" and mom_sum < mom_flip:
            return Adjust(action="EXIT", reason="mom_flip")
        if pos.side == "SHORT" and mom_sum > -mom_flip:
            return Adjust(action="EXIT", reason="mom_flip")

        # trailing stop
        trail_start = float(self.cfg.get("trail_start_atr", 1.2))
        trail_dist  = float(self.cfg.get("trail_dist_atr", 1.0))
        if atrr > 0 and trail_start > 0:
            if pos.side == "LONG":
                up = (price - pos.entry_price) / max(pos.entry_price, 1e-12)
                if up >= trail_start * atrr:
                    new_stop = price * (1.0 - trail_dist * atrr)
                    if pos.stop_price is None or new_stop > pos.stop_price:
                        return Adjust(action="MOVE_SL", reason="trail_up", new_stop=new_stop)
            else:
                up = (pos.entry_price - price) / max(pos.entry_price, 1e-12)
                if up >= trail_start * atrr:
                    new_stop = price * (1.0 + trail_dist * atrr)
                    if pos.stop_price is None or new_stop < pos.stop_price:
                        return Adjust(action="MOVE_SL", reason="trail_down", new_stop=new_stop)

        return Adjust(action="HOLD", reason="hold_ok")
