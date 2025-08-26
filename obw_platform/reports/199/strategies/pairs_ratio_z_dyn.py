
from .base import StrategyBase, Signal, Adjust
from collections import deque

class PairsRatioZDyn(StrategyBase):
    """
    Ratio z-score MR with dynamic notional sizing:
    notional = base_notional * clamp(atr_target/ATR, [size_min_scale, size_max_scale])
    """
    def __init__(self, cfg):
        super().__init__(cfg); self._hist = {}

    def _liq_ok(self, r):
        try:
            return float(r.get("qv_24h",0) or 0)>=float(self.cfg.get("min_qv_24h",2e5)) and                    float(r.get("quote_volume",0) or 0)>=float(self.cfg.get("min_qv_1h",1e4))
        except Exception:
            return False

    def universe(self, t, md):
        return [s for s,r in md.items() if self._liq_ok(r)]

    def _push(self, alt, ratio, win):
        dq = self._hist.get(alt)
        if dq is None:
            dq = deque(maxlen=win); self._hist[alt] = dq
        dq.append(float(ratio))

    def _z(self, alt, win):
        dq = self._hist.get(alt)
        if dq is None or len(dq) < win:
            return None, None, None
        xs = list(dq)
        mu = sum(xs)/len(xs)
        var = sum((x-mu)**2 for x in xs) / max(1, len(xs)-1)
        std = var ** 0.5
        z = (xs[-1]-mu)/std if std>0 else 0.0
        return mu, std, z

    def rank(self, t, md, symbols):
        btc = self.cfg.get("btc_symbol","BTC-USDT")
        if btc not in md: return []
        btc_px = float(md[btc].get("close",0.0) or 0.0)
        if btc_px <= 0: return []
        win = int(self.cfg.get("z_window", 30))
        items = []
        for s in symbols:
            if s == btc: continue
            r = md[s]
            if not self._liq_ok(r): continue
            alt_px = float(r.get("close",0.0) or 0.0)
            if alt_px <= 0: continue
            self._push(s, alt_px/btc_px, win)
            mu, std, z = self._z(s, win)
            if mu is None: continue
            items.append((s, abs(z), z))
        items.sort(key=lambda x: x[1], reverse=True)
        return [s for s,_,_ in items[:int(self.cfg.get("top_n", 3))]]

    def entry_signal(self, t, sym, row, ctx):
        md = ctx.get("md", {})
        btc = self.cfg.get("btc_symbol","BTC-USDT")
        if btc not in md: return None
        alt_px = float(row.get("close",0.0) or 0.0)
        btc_px = float(md[btc].get("close",0.0) or 0.0)
        if alt_px <= 0 or btc_px <= 0: return None

        win = int(self.cfg.get("z_window", 30))
        self._push(sym, alt_px/btc_px, win)
        _, _, z = self._z(sym, win)
        if z is None: return None

        z_entry = float(self.cfg.get("z_entry", 2.0))
        if z >= z_entry:
            side = "SHORT"
        elif z <= -z_entry:
            side = "LONG"
        else:
            return None

        atr = float(row.get("atr_ratio",0.0) or 0.0)
        min_atr = float(self.cfg.get("min_atr_ratio", 0.028))
        max_atr = float(self.cfg.get("max_atr_ratio", 0.10))
        if atr < min_atr or atr > max_atr: return None

        # dynamic notional
        base = float(self.cfg.get("base_notional", 20.0))
        atr_tgt = float(self.cfg.get("atr_target", 0.03))
        smin = float(self.cfg.get("size_min_scale", 0.8))
        smax = float(self.cfg.get("size_max_scale", 1.3))
        scale = atr_tgt / max(atr, 1e-6)
        if scale < smin: scale = smin
        if scale > smax: scale = smax
        notional = base * scale

        sl = float(self.cfg.get("sl_atr_mult", 1.0))
        tp = float(self.cfg.get("tp_atr_mult", 2.0))
        if side == "LONG":
            stop = alt_px * (1.0 - sl*atr)
            take = alt_px * (1.0 + tp*atr)
        else:
            stop = alt_px * (1.0 + sl*atr)
            take = alt_px * (1.0 - tp*atr)

        return Signal(side=side, reason="ratio_z_dyn_open",
                      stop_price=stop, take_profit=take,
                      max_hold_hours=int(self.cfg.get("max_hold_hours", 96)),
                      tags={"notional": notional, "z": z})

    def manage_position(self, t, sym, pos, row, ctx):
        md = ctx.get("md", {})
        btc = self.cfg.get("btc_symbol","BTC-USDT")
        if btc not in md: return self._time_mae(t, pos, row)

        alt_px = float(row.get("close",0.0) or 0.0)
        btc_px = float(md[btc].get("close",0.0) or 0.0)
        if alt_px <= 0 or btc_px <= 0:
            return Adjust(action="HOLD", reason="bad_px")

        win = int(self.cfg.get("z_window", 30))
        self._push(sym, alt_px/btc_px, win)
        _, _, z = self._z(sym, win)
        if z is None: return self._time_mae(t, pos, row)

        if abs(z) <= float(self.cfg.get("z_exit", 0.5)):
            return Adjust(action="EXIT", reason="z_mean_revert")

        adj = self._time_mae(t, pos, row)
        if adj.action == "EXIT":
            return adj

        return Adjust(action="HOLD", reason="hold")

    def _time_mae(self, t, pos, row):
        max_hold = int(pos.meta.get("max_hold_hours", self.cfg.get("max_hold_hours", 96)))
        if (t - pos.entry_time).total_seconds() >= max_hold*3600:
            return Adjust(action="EXIT", reason="time_exit")
        atr = float(row.get("atr_ratio",0.0) or 0.0)
        price = float(row.get("close",0.0) or 0.0)
        if price <= 0: return Adjust(action="HOLD", reason="bad_price")
        ret = (price - pos.entry_price) / max(pos.entry_price, 1e-12)
        pnl_like = ret if pos.side == "LONG" else -ret
        if pnl_like < -float(self.cfg.get("max_mae_atr_mult", 1.5)) * atr:
            return Adjust(action="EXIT", reason="mae_break")
        return Adjust(action="HOLD", reason="hold_time_mae")
