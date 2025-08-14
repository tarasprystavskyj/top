# strategies/vol_breakout.py
# Volatility Breakout (LONG) — Donchian/Keltner style via momentum proxy,
# with volume-surge and breadth filters. Works with engine/data.py from this repo.

from __future__ import annotations

import math
from typing import Dict, List, Optional, Any

import numpy as np
import pandas as pd

try:
    # normal relative import inside the project
    from .base import StrategyBase
except Exception:  # pragma: no cover
    # fallback if loaded standalone
    from strategies.base import StrategyBase  # type: ignore


def _get_df(md_slice: Any, symbol: str) -> pd.DataFrame:
    """
    Be tolerant to different md_slice implementations in this repo.
    """
    # common patterns in this codebase
    if hasattr(md_slice, "dfs") and symbol in md_slice.dfs:
        return md_slice.dfs[symbol]
    if hasattr(md_slice, "get_df"):
        return md_slice.get_df(symbol)
    if isinstance(md_slice, dict) and symbol in md_slice:
        return md_slice[symbol]
    raise KeyError(f"Cannot resolve dataframe for symbol={symbol}")


def _last_row(md_slice: Any, symbol: str) -> pd.Series:
    df = _get_df(md_slice, symbol)
    if len(df) == 0:
        raise ValueError(f"Empty dataframe for {symbol}")
    return df.iloc[-1]


def _symbols(md_slice: Any) -> List[str]:
    if hasattr(md_slice, "symbols"):
        return list(md_slice.symbols)
    if hasattr(md_slice, "dfs"):
        return list(md_slice.dfs.keys())
    if hasattr(md_slice, "get_symbols"):
        return list(md_slice.get_symbols())
    if isinstance(md_slice, dict):
        return list(md_slice.keys())
    raise RuntimeError("Cannot enumerate symbols in md_slice")


class VolatilityBreakout(StrategyBase):
    """
    LONG-only breakout with:
      - momentum proxy: dp6h + dp12h (fallback to gain_24h_before if dp* absent)
      - min ATR/price filter (atr_ratio)
      - volume-surge filter: quote_volume vs (qv_24h/24)
      - breadth filter: share of universe with positive momentum
      - risk: SL/TP in ATR-multiples; trailing; max hold hours; MAE limit.

    Strategy parameters (YAML -> strategy_params):
      top_n: int
      min_qv_24h: float
      min_qv_1h: float
      min_atr_ratio: float
      min_momentum_sum: float
      min_vol_surge_mult: float
      min_breadth: float
      sl_atr_mult: float
      tp_atr_mult: float
      max_hold_hours: int
      max_mae_atr_mult: float
      mom_flip_thresh: float
      trail_start_atr: float
      trail_dist_atr: float
    """

    def __init__(self, cfg: Dict[str, Any]) -> None:
        super().__init__(cfg)
        self.cfg = cfg or {}
        self._last_breadth: float = 1.0  # updated in rank()

    # ---------- helpers ----------

    def _vol_ok(self, row: pd.Series, mult: float) -> bool:
        """
        Volume surge check: quote_volume >= mult * avg_1h, where
        avg_1h = qv_24h / 24. (This is TF-agnostic but robust on 1h caches.)
        """
        qv24 = float(row.get("qv_24h", 0.0) or 0.0)
        qv1h = float(row.get("quote_volume", 0.0) or 0.0)
        avg1h = (qv24 / 24.0) if qv24 > 0 else 0.0
        return (avg1h > 0.0) and (qv1h >= mult * avg1h)

    def _momentum(self, row: pd.Series) -> float:
        # prefer dp6h + dp12h (added by engine/data.py), fallback to a 24h gain proxy
        vals = []
        for k in ("dp6h", "dp12h"):
            if k in row and pd.notna(row[k]):
                vals.append(float(row[k]))
        if not vals:
            v = float(row.get("gain_24h_before", 0.0) or 0.0)
            vals.append(v)
        return float(np.nansum(vals))

    # ---------- required API ----------

    def universe(self, md_slice: Any) -> List[str]:
        """
        Liquidity screen at the last bar using qv_24h and quote_volume.
        """
        min_qv_24h = float(self.cfg.get("min_qv_24h", 200_000))
        min_qv_1h = float(self.cfg.get("min_qv_1h", 10_000))

        syms = _symbols(md_slice)
        out = []
        for s in syms:
            r = _last_row(md_slice, s)
            qv24 = float(r.get("qv_24h", 0.0) or 0.0)
            qv1h = float(r.get("quote_volume", 0.0) or 0.0)
            if qv24 >= min_qv_24h and qv1h >= min_qv_1h:
                out.append(s)
        return out

    def rank(self, md_slice: Any, symbols: List[str]) -> List[str]:
        """
        Rank by momentum proxy; also compute market breadth (= share with positive momentum).
        """
        items = []
        positives = 0
        total = 0
        for s in symbols:
            r = _last_row(md_slice, s)
            mom = self._momentum(r)
            # keep only those with atr available; NaN -> skip from breadth denom
            atr = float(r.get("atr_ratio", np.nan))
            if not math.isnan(atr):
                total += 1
                if mom > 0:
                    positives += 1
            items.append((s, float(mom)))

        self._last_breadth = (positives / max(total, 1)) if total > 0 else 0.0
        items.sort(key=lambda x: x[1], reverse=True)

        top_n = int(self.cfg.get("top_n", 4))
        return [s for s, _ in items[:top_n]]

    def entry_signal(self, md_slice: Any, symbol: str, ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """
        LONG entries only (per design). Filters: ATR, momentum, volume-surge, breadth.
        """
        r = _last_row(md_slice, symbol)

        side = str(self.cfg.get("side", "LONG")).upper()
        if side != "LONG":
            return None  # this implementation is long-only

        price = float(r.get("close", np.nan))
        atr = float(r.get("atr_ratio", np.nan))
        if math.isnan(price) or math.isnan(atr):
            return None

        mom = self._momentum(r)

        # filters
        if atr < float(self.cfg.get("min_atr_ratio", 0.022)):
            return None
        if mom < float(self.cfg.get("min_momentum_sum", 0.12)):
            return None
        if not self._vol_ok(r, float(self.cfg.get("min_vol_surge_mult", 1.25))):
            return None
        if float(getattr(self, "_last_breadth", 1.0)) < float(self.cfg.get("min_breadth", 0.58)):
            return None

        sl_mult = float(self.cfg.get("sl_atr_mult", 1.4))
        tp_mult = float(self.cfg.get("tp_atr_mult", 2.6))

        sl = price * (1.0 - sl_mult * atr)
        tp = price * (1.0 + tp_mult * atr)

        # simple capital split across top_n
        size = 1.0 / max(1, int(self.cfg.get("top_n", 4)))

        return {
            "symbol": symbol,
            "side": "LONG",
            "price": price,
            "sl": sl,
            "tp": tp,
            "size": size,
            "comment": f"VB mom={mom:.4f} atr={atr:.4f} br={getattr(self,'_last_breadth',1.0):.2f}",
        }

    def manage_position(self, md_slice: Any, pos: Dict[str, Any], ctx: Dict[str, Any]) -> Dict[str, Any]:
        """
        Trailing stop after favorable move; time-based exit; MAE guard; momentum flip exit.
        """
        symbol = pos["symbol"]
        r = _last_row(md_slice, symbol)
        price = float(r.get("close", np.nan))
        atr = float(r.get("atr_ratio", np.nan))
        if math.isnan(price) or math.isnan(atr):
            return {"action": "hold"}

        entry = float(pos.get("price", price))
        sl = float(pos.get("sl", entry * (1.0 - 1.4 * atr)))
        tp = float(pos.get("tp", entry * (1.0 + 2.6 * atr)))

        # bars held / hours held approximator (1 bar ~ 1h for 1h caches)
        bars_held = int(pos.get("bars_held", 0))
        bars_held += 1

        # trailing
        trail_start = float(self.cfg.get("trail_start_atr", 1.2))
        trail_dist = float(self.cfg.get("trail_dist_atr", 1.0))

        # maximum favorable excursion since entry
        mfe_price = float(pos.get("mfe_price", entry))
        mfe_price = max(mfe_price, price)

        # start trailing if moved >= trail_start*ATR from entry
        if (mfe_price - entry) >= (trail_start * atr * entry):
            trail_sl = mfe_price * (1.0 - trail_dist * atr)
            sl = max(sl, trail_sl)

        # TP refresh as proportional to ATR (optional no-op here)
        # tp = max(tp, entry * (1.0 + float(self.cfg.get("tp_atr_mult", 2.6)) * atr))

        # time stop
        max_hold_hours = int(self.cfg.get("max_hold_hours", 96))
        if bars_held >= max_hold_hours:
            return {"action": "close", "reason": "time", "sl": sl, "tp": tp, "bars_held": bars_held, "mfe_price": mfe_price}

        # MAE guard
        max_mae_mult = float(self.cfg.get("max_mae_atr_mult", 1.6))
        if price <= entry * (1.0 - max_mae_mult * atr):
            return {"action": "close", "reason": "mae", "sl": sl, "tp": tp, "bars_held": bars_held, "mfe_price": mfe_price}

        # momentum flip exit
        mom_flip = float(self.cfg.get("mom_flip_thresh", 0.02))
        mom = self._momentum(r)
        if mom < mom_flip:
            # exit on loss of momentum
            return {"action": "close", "reason": "mom_flip", "sl": sl, "tp": tp, "bars_held": bars_held, "mfe_price": mfe_price}

        # keep holding, update tracking fields
        return {"action": "hold", "sl": sl, "tp": tp, "bars_held": bars_held, "mfe_price": mfe_price}
