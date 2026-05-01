from __future__ import annotations

import datetime as dt
from typing import Optional


UTC = dt.timezone.utc


def utc_now() -> dt.datetime:
    return dt.datetime.now(tz=UTC)


def utc_now_iso() -> str:
    return utc_now().isoformat()


def parse_iso_to_datetime(value: str) -> dt.datetime:
    """Parse ISO string into UTC-aware datetime."""
    s = str(value).strip()
    if not s:
        raise ValueError("empty datetime string")
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    x = dt.datetime.fromisoformat(s)
    if x.tzinfo is None:
        x = x.replace(tzinfo=UTC)
    else:
        x = x.astimezone(UTC)
    return x


def parse_iso_to_epoch_s(value: str) -> int:
    return int(parse_iso_to_datetime(value).timestamp())


def epoch_s_to_datetime(ts_s: int | float) -> dt.datetime:
    return dt.datetime.fromtimestamp(float(ts_s), tz=UTC)


def epoch_s_to_utc_iso(ts_s: int | float) -> str:
    return epoch_s_to_datetime(ts_s).isoformat()


def floor_epoch_s(ts_s: int | float, timeframe_s: int) -> int:
    tf = int(timeframe_s)
    if tf <= 0:
        raise ValueError("timeframe_s must be positive")
    t = int(ts_s)
    return t - (t % tf)


def ceil_epoch_s(ts_s: int | float, timeframe_s: int) -> int:
    tf = int(timeframe_s)
    if tf <= 0:
        raise ValueError("timeframe_s must be positive")
    t = int(ts_s)
    r = t % tf
    return t if r == 0 else t + (tf - r)


def seconds_between_iso(start: str, end: str) -> int:
    return parse_iso_to_epoch_s(end) - parse_iso_to_epoch_s(start)


def infer_bar_seconds(timestamps_s, default: float = 60.0) -> float:
    """Infer median bar spacing from iterable of epoch seconds."""
    try:
        import numpy as np
        arr = np.asarray(list(timestamps_s), dtype=float)
        if len(arr) < 2:
            return float(default)
        diffs = np.diff(arr)
        diffs = diffs[diffs > 0]
        if len(diffs) == 0:
            return float(default)
        return float(np.median(diffs))
    except Exception:
        return float(default)
