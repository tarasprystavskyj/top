# strategies/breakout_avaai_full_sized.py
# Обгортка над BreakoutAVAAIFull, яка додає size_mult через AdaptiveSizer.
from types import SimpleNamespace
from math import isfinite

# Імпортуємо твою базову стратегію
from strategies.breakout_avaai_full import BreakoutAVAAIFull as Base

# Наш простий сайзер (той, що ми додали раніше)
try:
    from strategies.adaptive_sizer import AdaptiveSizer
except Exception:
    # fallback: нейтральний
    class AdaptiveSizer:
        def __init__(self, cfg=None): pass
        def on_trade_close(self, pct_return): pass
        def size_mult(self, conf, current_dd): return 1.0

def _clip(x, a, b):
    return a if x < a else b if x > b else x

def _safe_div(a, b, default=0.0):
    try:
        if b == 0: return default
        v = a / b
        return v if isfinite(v) else default
    except Exception:
        return default

class BreakoutAVAAIFullSized(Base):
    """
    Обгортка: додає size_mult = f(confidence, DD, історична ефективність),
    не змінюючи логіку входу/фільтрів базової BreakoutAVAAIFull.
    """
    def __init__(self, cfg):
        super().__init__(cfg)
        self.cfg = cfg or {}
        self.sizer = AdaptiveSizer(self.cfg.get("sizer", {}) or {})

        # Пороги для нормування (дефолти дружні до 5m/15m сетапів)
        self._mom_ref = float(self.cfg.get("sizer", {}).get("mom_ref", 0.015))  # 1.5%
        self._atr_ref = float(self.cfg.get("sizer", {}).get("atr_ref", 0.003))  # 0.3%
        self._vol_ref_div = float(self.cfg.get("sizer", {}).get("vol_ref_div", 24.0))  # qv_24h/24

    def _confidence(self, row, ctx):
        # momentum: нормуємо |dp6h+dp12h| на ref
        mom_sum = float(row.get("dp6h", 0.0) or 0.0) + float(row.get("dp12h", 0.0) or 0.0)
        mom = _clip(abs(_safe_div(mom_sum, max(1e-9, self._mom_ref), 0.0)), 0.0, 1.0)

        # volume: qv_1h / (qv_24h/24)
        qv1h = float(row.get("quote_volume", 0.0) or 0.0)
        qv24 = float(row.get("qv_24h", 0.0) or 0.0)
        vol_ref = _safe_div(qv24, self._vol_ref_div, 0.0)
        vol = _clip(_safe_div(qv1h, max(1e-9, vol_ref), 0.0), 0.0, 1.0)

        # atr_trend: atr_ratio / atr_ref
        atr_ratio = float(row.get("atr_ratio", 0.0) or 0.0)
        atr = _clip(_safe_div(atr_ratio, max(1e-9, self._atr_ref), 0.0), 0.0, 1.0)

        # adx у цій вибірці немає — ставимо 0
        adx = 0.0

        return {"momentum": mom, "volume": vol, "atr_trend": atr, "adx": adx}

    def entry_signal(self, t, sym, row, ctx=None):
        # 1) Беремо базовий сигнал із твоєї стратегії
        base_sig = super().entry_signal(t, sym, row, ctx)
        if base_sig is None:
            return None
        take = getattr(base_sig, "take", True)
        if not take:
            return base_sig  # шануємо відмову

        # 2) Рахуємо конфіденс і size_mult
        conf = self._confidence(row, ctx or SimpleNamespace(current_dd=0.0))
        current_dd = getattr(ctx, "current_dd", 0.0) if ctx else 0.0
        m = 1.0
        try:
            m = float(self.sizer.size_mult(conf, current_dd))
            if not isfinite(m): m = 1.0
        except Exception:
            m = 1.0

        # 3) Повертаємо новий сигнальний об’єкт (щоб не ламати __slots__ бази)
        side = getattr(base_sig, "side", None)
        return SimpleNamespace(side=side, take=True, size_mult=m)
