# -*- coding: utf-8 -*-
"""
Compound DCA v5 — adds DUAL-MA TIMEFRAME REGIME SWITCHING (Wave 8 H3).

Hypothesis: champion uses single trendMaTf=D for adaptive sizing. By maintaining
BOTH W and D moving averages in parallel and selecting at signal time based on
`vol_surge_mult` from NPZ extras, we let the strategy use:
  - SLOW (W) MA when vol is calm (less reaction to micro-noise)
  - FAST (D) MA when vol surge is high (responsive to regime shifts)
  OR the inverse, controlled by `dualMaInvert` flag.

Mechanism:
  - Maintain TWO independent (trend_bucket, htf_closes, ma_series) state tracks,
    one for W and one for D.
  - On each bar, update both tracks.
  - At sizing decision time, pick the active TF based on:
        if vol_surge_mult >= dualMaSurgeThreshold:  use trendMaTfHi
        else:                                         use trendMaTfLo
    Default: HiTf=D (fast under surge), LoTf=W (slow under calm).

New gated params (default OFF for bit-for-bit identity to v1):
  dualMaEnabled:        0|1   (default 0)
  dualMaTfLo:           str   (default 'W')
  dualMaTfHi:           str   (default 'D')
  dualMaSurgeThreshold: float (default 2.0; vol_surge_mult >= X uses Hi TF)

When dualMaEnabled=0, behavior is bit-for-bit identical to base v1 (no extra MA
state tracked, _update_trend takes the original path using self.trend_ma_tf).

Implementation: override _update_trend() to either delegate to base (when off)
or run dual-track logic (when on).
"""
from __future__ import annotations
import datetime as _dt
from collections import deque
from typing import Any, Dict

from strategies.cryptomine_pack_dual_compound import _PackCompoundBase


class _PackCompoundV5Base(_PackCompoundBase):
    def __init__(self, cfg: Dict[str, Any], params_key: str):
        super().__init__(cfg, params_key)
        sp = cfg.get(params_key, {}) or {}
        self.dual_ma_enabled = bool(int(sp.get('dualMaEnabled', 0)))
        self.dual_ma_tf_lo = str(sp.get('dualMaTfLo', 'W'))
        self.dual_ma_tf_hi = str(sp.get('dualMaTfHi', 'D'))
        self.dual_ma_surge_threshold = float(sp.get('dualMaSurgeThreshold', 2.0))
        # Per-symbol dual-track state: {sym: {'lo': (bucket, htf_closes_deque, ma_series_deque),
        #                                      'hi': (bucket, htf_closes_deque, ma_series_deque)}}
        self._dual_states: Dict[str, Dict[str, Any]] = {}
        # Cache last vol_surge for use in _update_trend (set at top of manage/entry)
        self._last_vol_surge: float = 0.0

    def _get_dual_state(self, sym):
        if sym not in self._dual_states:
            self._dual_states[sym] = {
                'lo': {'bucket': None, 'htf_closes': deque(), 'ma_series': deque()},
                'hi': {'bucket': None, 'htf_closes': deque(), 'ma_series': deque()},
            }
        return self._dual_states[sym]

    def _bucket_for_tf(self, dt, tf):
        dt = dt.astimezone(_dt.timezone.utc) if dt.tzinfo else dt.replace(tzinfo=_dt.timezone.utc)
        tf_u = tf.upper()
        if tf_u == 'W':
            iso = dt.isocalendar()
            return f'{iso[0]}-W{iso[1]:02d}'
        if tf_u == 'D':
            return dt.strftime('%Y-%m-%d')
        secs = self._tf_seconds(tf.lower())
        return str(int(dt.timestamp()) // secs)

    def _update_one_track(self, track, bucket, close):
        if track['bucket'] is None:
            track['bucket'] = bucket
            track['htf_closes'].append(close)
        elif bucket != track['bucket']:
            track['bucket'] = bucket
            track['htf_closes'].append(close)
        else:
            if track['htf_closes']:
                track['htf_closes'][-1] = close
            else:
                track['htf_closes'].append(close)
        vals = list(track['htf_closes'])
        ma = sum(vals[-self.trend_ma_len:]) / self.trend_ma_len if len(vals) >= self.trend_ma_len else None
        track['ma_series'].append(ma)
        cap = max(self.trend_slope_bars + 5, 256)
        while len(track['ma_series']) > cap:
            track['ma_series'].popleft()
        prev = list(track['ma_series'])[-1 - self.trend_slope_bars] if len(track['ma_series']) > self.trend_slope_bars else None
        slope = ((ma - prev) / prev) * 100.0 if (ma is not None and prev not in (None, 0)) else 0.0
        return slope

    def _slope_to_target(self, slope):
        rng = max(abs(self.trend_slope_long_bound - self.trend_slope_short_bound), 1e-6)
        if self.SIDE == 'SHORT':
            strength = max(0.0, min(100.0, 100.0 * (self.trend_slope_long_bound - slope) / rng))
        else:
            strength = max(0.0, min(100.0, 100.0 * (slope - self.trend_slope_short_bound) / rng))
        score_rng = max(abs(self.trend_score_max - self.trend_score_min), 1e-6)
        factor = max(0.0, min(1.0, (strength - self.trend_score_min) / score_rng))
        return self.min_invest_pct + (self.max_invest_pct - self.min_invest_pct) * factor

    def _update_trend(self, st, t, close):
        if not self.dual_ma_enabled:
            return super()._update_trend(st, t, close)
        # Need symbol — derive from state object via reverse lookup. State carried per sym
        # Identify which sym from self._states matches st (tiny hash): fallback to scalar
        sym = None
        for k, v in self._states.items():
            if v is st:
                sym = k
                break
        if sym is None:
            return super()._update_trend(st, t, close)
        dual = self._get_dual_state(sym)
        dt = self._parse_time(t)
        bucket_lo = self._bucket_for_tf(dt, self.dual_ma_tf_lo)
        bucket_hi = self._bucket_for_tf(dt, self.dual_ma_tf_hi)
        slope_lo = self._update_one_track(dual['lo'], bucket_lo, close)
        slope_hi = self._update_one_track(dual['hi'], bucket_hi, close)
        # Select active slope based on vol_surge
        if self._last_vol_surge >= self.dual_ma_surge_threshold:
            slope = slope_hi
        else:
            slope = slope_lo
        return self._slope_to_target(slope)

    def entry_signal(self, is_opening, sym, row, ctx=None):
        if self.dual_ma_enabled:
            self._last_vol_surge = float(row.get('vol_surge_mult', 0.0) or 0.0)
        return super().entry_signal(is_opening, sym, row, ctx)

    def manage_position(self, sym, row, pos, ctx=None):
        if self.dual_ma_enabled:
            self._last_vol_surge = float(row.get('vol_surge_mult', 0.0) or 0.0)
        return super().manage_position(sym, row, pos, ctx)


class CompoundShortPackV5(_PackCompoundV5Base):
    SIDE = 'SHORT'
    def __init__(self, cfg):
        super().__init__(cfg, 'strategy_params_short')


class CompoundLongPackV5(_PackCompoundV5Base):
    SIDE = 'LONG'
    def __init__(self, cfg):
        super().__init__(cfg, 'strategy_params_long')
