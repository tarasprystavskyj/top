#!/usr/bin/env python3
"""Rolling-high regime-off controller for external-signal DCA research.

The controller is intentionally small and side-effect free. It does not place
orders and does not know about exchanges. Callers feed it one bar/mark price at
a time and use the returned decision to block new long cycles or force-close an
existing research position.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any, Deque, Dict, Optional


@dataclass(frozen=True)
class RegimeOffConfig:
    enabled: bool = False
    mode: str = "hard_close"
    lookback_bars: int = 168 * 60
    required_new_high_bars: int = 72 * 60
    new_high_tolerance: float = 0.001
    min_history_bars: int = 10

    @classmethod
    def from_hours(
        cls,
        *,
        enabled: bool = True,
        mode: str = "hard_close",
        lookback_hours: float = 168.0,
        required_new_high_within_hours: float = 72.0,
        bar_minutes: float = 1.0,
        new_high_tolerance: float = 0.001,
    ) -> "RegimeOffConfig":
        bars_per_hour = 60.0 / float(bar_minutes)
        lookback_bars = max(1, int(round(float(lookback_hours) * bars_per_hour)))
        required_bars = max(1, int(round(float(required_new_high_within_hours) * bars_per_hour)))
        min_history = min(lookback_bars, max(10, lookback_bars // 20))
        return cls(
            enabled=bool(enabled),
            mode=str(mode or "hard_close"),
            lookback_bars=lookback_bars,
            required_new_high_bars=required_bars,
            new_high_tolerance=float(new_high_tolerance),
            min_history_bars=min_history,
        )

    @classmethod
    def from_dict(cls, raw: Optional[Dict[str, Any]]) -> "RegimeOffConfig":
        section = dict(raw or {})
        if not bool(section.get("enabled", False)):
            return cls(enabled=False)
        return cls.from_hours(
            enabled=True,
            mode=str(section.get("mode") or "hard_close"),
            lookback_hours=float(section.get("lookback_hours", 168.0)),
            required_new_high_within_hours=float(section.get("required_new_high_within_hours", 72.0)),
            bar_minutes=float(section.get("bar_minutes", 1.0)),
            new_high_tolerance=float(section.get("new_high_tolerance", 0.001)),
        )


@dataclass(frozen=True)
class RegimeOffDecision:
    regime_on: bool
    allow_new_long: bool
    should_close_long: bool
    bars_since_new_high: Optional[int]
    rolling_high: Optional[float]
    reason: str


class RegimeOffController:
    def __init__(self, config: RegimeOffConfig):
        self.config = config
        self._history: Deque[float] = deque(maxlen=max(1, int(config.lookback_bars)))
        self._bars_since_new_high: Optional[int] = None

    def update(
        self,
        close: float,
        *,
        signal_fresh: bool = False,
        has_long_position: bool = False,
    ) -> RegimeOffDecision:
        cfg = self.config
        price = float(close)
        if not cfg.enabled:
            self._history.append(price)
            return RegimeOffDecision(True, True, False, None, None, "regime_off_disabled")

        if len(self._history) < cfg.min_history_bars:
            self._history.append(price)
            return RegimeOffDecision(True, True, False, None, None, "insufficient_regime_history")

        rolling_high = max(self._history)
        is_new_high = price >= rolling_high * (1.0 - cfg.new_high_tolerance)
        if is_new_high:
            self._bars_since_new_high = 0
        elif self._bars_since_new_high is None:
            self._bars_since_new_high = 1
        else:
            self._bars_since_new_high += 1

        regime_on = int(self._bars_since_new_high or 0) <= cfg.required_new_high_bars
        mode = str(cfg.mode or "hard_close")
        if mode == "fresh_override":
            allow = regime_on or bool(signal_fresh)
            reason = "regime_on" if regime_on else ("signal_fresh_override" if signal_fresh else "regime_off")
        elif mode == "hard_close":
            allow = regime_on
            reason = "regime_on" if regime_on else "regime_off"
        else:
            raise ValueError(f"unsupported regime-off mode: {mode!r}")

        self._history.append(price)
        return RegimeOffDecision(
            regime_on=regime_on,
            allow_new_long=allow,
            should_close_long=bool(has_long_position and not allow),
            bars_since_new_high=self._bars_since_new_high,
            rolling_high=float(rolling_high),
            reason=reason,
        )
