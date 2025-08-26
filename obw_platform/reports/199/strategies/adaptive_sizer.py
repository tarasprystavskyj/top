# strategies/adaptive_sizer.py
from collections import deque
import math

def _clip(x, lo, hi): 
    return lo if x < lo else hi if x > hi else x

def _lin_map(x, x0, x1, y0, y1):
    # лінійне відображення з насиченням на краях
    if x1 == x0:
        return (y0 + y1) * 0.5
    t = (x - x0) / float(x1 - x0)
    t = _clip(t, 0.0, 1.0)
    return y0 + t * (y1 - y0)

class AdaptiveSizer:
    """
    Розмір позиції = base_notional * size_mult

    size_mult = conf_mult * perf_mult * dd_mult
      perf_mult — від середньої дохідності та winrate останніх N угод
      conf_mult — від «впевненості сигналу» (моментум/обсяг/ATR/ADX …)
      dd_mult   — штраф за поточний локальний просідання портфелю

    Параметри беруться з cfg['sizer'] (приклад YAML нижче).
    """

    def __init__(self, cfg: dict):
        s = (cfg or {}).get("sizer", {}) or {}
        self.enabled       = bool(s.get("enabled", True))
        self.min_mult      = float(s.get("min_mult", 0.4))
        self.max_mult      = float(s.get("max_mult", 1.8))
        self.lookback      = int(s.get("lookback_trades", 20))

        # Перетворення performance → множник
        self.ret_floor     = float(s.get("ret_mean_floor", -0.01))  # середній %/угоду → 0.6…1.4
        self.ret_ceiling   = float(s.get("ret_mean_ceiling",  0.03))
        self.perf_lo       = float(s.get("perf_mult_low",  0.6))
        self.perf_hi       = float(s.get("perf_mult_high", 1.4))

        self.win_floor     = float(s.get("winrate_floor", 0.45))
        self.win_ceiling   = float(s.get("winrate_ceiling", 0.65))
        self.win_lo        = float(s.get("win_mult_low",  0.8))
        self.win_hi        = float(s.get("win_mult_high", 1.2))

        # Впевненість сигналу (0..1) → множник
        self.conf_lo       = float(s.get("conf_mult_low",  0.7))
        self.conf_hi       = float(s.get("conf_mult_high", 1.3))

        # Анти-DD
        self.dd_softcap    = float(s.get("dd_softcap", 0.12))  # 12% просідання
        self.dd_gamma      = float(s.get("dd_gamma", 2.5))     # агресивність штрафу

        # Ваги для складових «впевненості»
        cw = (s.get("conf_weights", {}) or {})
        self.w_mom   = float(cw.get("momentum", 0.40))
        self.w_vol   = float(cw.get("volume",   0.20))
        self.w_atr   = float(cw.get("atr_trend",0.20))
        self.w_adx   = float(cw.get("adx",      0.20))

        self.last_returns = deque(maxlen=self.lookback)

    # ==== API для ядра ====

    def on_trade_close(self, pct_return: float):
        """Додати % результат угоди, наприклад +0.032 = +3.2%."""
        if pct_return is not None:
            self.last_returns.append(float(pct_return))

    def perf_stats(self):
        """Середня дохідність та winrate по останніх угодах."""
        if not self.last_returns:
            return 0.0, 0.0, 0
        arr = list(self.last_returns)
        avg = sum(arr) / len(arr)
        winrate = sum(1 for r in arr if r > 0) / float(len(arr))
        return avg, winrate, len(arr)

    # ==== Розрахунок множників ====

    def _perf_mult(self):
        avg, win, n = self.perf_stats()
        perf_m = _lin_map(avg, self.ret_floor, self.ret_ceiling, self.perf_lo, self.perf_hi)
        win_m  = _lin_map(win, self.win_floor, self.win_ceiling, self.win_lo,  self.win_hi)
        return perf_m * win_m

    def _conf_mult(self, conf: dict):
        """
        conf — словник ознак впевненості (0..1):
          momentum, volume, atr_trend, adx
        """
        mom = float(conf.get("momentum", 0.0))
        vol = float(conf.get("volume",   0.0))
        atr = float(conf.get("atr_trend",0.0))
        adx = float(conf.get("adx",      0.0))
        score = self.w_mom*mom + self.w_vol*vol + self.w_atr*atr + self.w_adx*adx
        score = _clip(score, 0.0, 1.0)
        return _lin_map(score, 0.0, 1.0, self.conf_lo, self.conf_hi)

    def _dd_mult(self, current_dd: float):
        """
        current_dd — поточне просідання (додатне), наприклад 0.08 для -8%.
        """
        over = max(0.0, current_dd - self.dd_softcap)
        return math.exp(-self.dd_gamma * over)

    def size_mult(self, conf: dict, current_dd: float):
        if not self.enabled:
            return 1.0
        m = self._perf_mult() * self._conf_mult(conf) * self._dd_mult(max(0.0, current_dd))
        return _clip(m, self.min_mult, self.max_mult)
