#!/usr/bin/env python3
"""External-signal gate for the real V21 dual-pack strategy.

The classes in this module intentionally keep the strategy API used by the
existing dual backtest/live runners: `entry_signal`, `manage_position`, and
`sync_after_external_fill`. They do not reimplement DCA. They instantiate the
real V21 leg strategy and only gate new entries by an external signal list.
"""

from __future__ import annotations

import copy
import importlib
import json
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


DEFAULT_LONG_CLASS = "strategies.cryptomine_pack_dual_full.CryptomineLongPackAdaptiveEven"
DEFAULT_SHORT_CLASS = "strategies.cryptomine_pack_dual_full.CryptomineShortPackAdaptiveEven"


def _parse_dt(raw: Any) -> Optional[datetime]:
    if raw in (None, ""):
        return None
    if isinstance(raw, datetime):
        dt = raw
    else:
        dt = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _norm_symbol(raw: Any) -> str:
    text = str(raw or "").upper().strip()
    if "/" in text:
        return text
    if text.endswith("USDT"):
        return f"{text[:-4]}/USDT:USDT"
    return text


def _base_symbol(raw: Any) -> str:
    text = str(raw or "").upper().strip()
    if "/" in text:
        return text.split("/", 1)[0]
    if text.endswith("USDT"):
        return text[:-4]
    return text


def _import_class(path: str):
    mod_path, cls_name = str(path).rsplit(".", 1)
    return getattr(importlib.import_module(mod_path), cls_name)


@dataclass(frozen=True)
class ExternalSignal:
    symbol: str
    side: str
    start_utc: Optional[datetime] = None
    expires_utc: Optional[datetime] = None
    source: str = ""
    signal_id: str = ""

    @classmethod
    def from_dict(cls, row: Dict[str, Any]) -> "ExternalSignal":
        return cls(
            symbol=_norm_symbol(row.get("symbol") or row.get("market") or row.get("base")),
            side=str(row.get("side") or "").upper(),
            start_utc=_parse_dt(row.get("start_utc") or row.get("detected_utc") or row.get("entry_utc")),
            expires_utc=_parse_dt(row.get("expires_utc") or row.get("ttl_until_utc")),
            source=str(row.get("source") or row.get("strategy_name") or ""),
            signal_id=str(row.get("signal_id") or row.get("id") or ""),
        )

    def is_active(self, side: str, symbol: str, t: Optional[datetime]) -> bool:
        if self.side != side.upper():
            return False
        want = _norm_symbol(symbol)
        if self.symbol != want and _base_symbol(self.symbol) != _base_symbol(want):
            return False
        if t is None:
            return True
        if self.start_utc and t < self.start_utc:
            return False
        if self.expires_utc and t > self.expires_utc:
            return False
        return True


class ExternalSignalRegistry:
    def __init__(self, cfg: Dict[str, Any]):
        section = cfg.get("external_signal_v21") or {}
        self.allow_all = bool(section.get("allow_all", False))
        self.signals_file = str(section.get("signals_file") or "")
        self.inline_signals = [ExternalSignal.from_dict(x) for x in (section.get("signals") or [])]
        self._file_mtime: Optional[float] = None
        self._file_signals: List[ExternalSignal] = []

    def _load_file_signals(self) -> List[ExternalSignal]:
        if not self.signals_file:
            return []
        path = Path(self.signals_file)
        if not path.is_absolute():
            path = ROOT / self.signals_file
        if not path.exists():
            return []
        mtime = path.stat().st_mtime
        if self._file_mtime == mtime:
            return self._file_signals
        raw = json.loads(path.read_text(encoding="utf-8"))
        rows = raw.get("signals", raw) if isinstance(raw, dict) else raw
        self._file_signals = [ExternalSignal.from_dict(x) for x in rows if isinstance(x, dict)]
        self._file_mtime = mtime
        return self._file_signals

    def active_for(self, side: str, symbol: str, t: Optional[datetime]) -> Optional[ExternalSignal]:
        if self.allow_all:
            return ExternalSignal(symbol=_norm_symbol(symbol), side=side.upper(), start_utc=t, source="allow_all")
        for sig in [*self.inline_signals, *self._load_file_signals()]:
            if sig.is_active(side, symbol, t):
                return sig
        return None


def build_v21_external_signal_cfg(
    base_cfg: Dict[str, Any],
    *,
    delegated_capital_usdt: float,
    base_order_pct_eq: float = 5.0,
    signals_file: str = "",
    signals: Optional[Iterable[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    """Return a cfg copy that uses wrapper strategy classes around real V21."""
    cfg = copy.deepcopy(base_cfg)
    cfg["strategy_class_long"] = "meta_strategies.v21_external_signal_wrapper.V21ExternalSignalLong"
    cfg["strategy_class_short"] = "meta_strategies.v21_external_signal_wrapper.V21ExternalSignalShort"
    for key in ("strategy_params_long", "strategy_params_short"):
        params = cfg.setdefault(key, {})
        params["equityForSizingUSDT"] = float(delegated_capital_usdt)
        params["baseOrderPctEq"] = float(base_order_pct_eq)
    section = cfg.setdefault("external_signal_v21", {})
    section.setdefault("delegate_strategy_class_long", DEFAULT_LONG_CLASS)
    section.setdefault("delegate_strategy_class_short", DEFAULT_SHORT_CLASS)
    section["delegated_capital_usdt"] = float(delegated_capital_usdt)
    section["base_order_pct_eq"] = float(base_order_pct_eq)
    if signals_file:
        section["signals_file"] = str(signals_file)
    if signals is not None:
        section["signals"] = list(signals)
    return cfg


class _V21ExternalSignalBase:
    SIDE = ""
    PARAMS_KEY = ""
    DEFAULT_DELEGATE = ""

    def __init__(self, cfg: Dict[str, Any]):
        self.cfg = copy.deepcopy(cfg)
        section = self.cfg.get("external_signal_v21") or {}
        cap = section.get("delegated_capital_usdt")
        bop = section.get("base_order_pct_eq")
        params = self.cfg.setdefault(self.PARAMS_KEY, {})
        if cap is not None:
            params["equityForSizingUSDT"] = float(cap)
        if bop is not None:
            params["baseOrderPctEq"] = float(bop)
        class_key = "delegate_strategy_class_long" if self.SIDE == "LONG" else "delegate_strategy_class_short"
        delegate_path = str(section.get(class_key) or self.DEFAULT_DELEGATE)
        self.delegate = _import_class(delegate_path)(self.cfg)
        self.registry = ExternalSignalRegistry(self.cfg)
        self.last_entry_signal: Optional[ExternalSignal] = None

    def _row_time(self, row: Dict[str, Any]) -> Optional[datetime]:
        return _parse_dt(row.get("datetime_utc") or row.get("timestamp") or row.get("time"))

    def universe(self, t, md_map):
        if hasattr(self.delegate, "universe"):
            return self.delegate.universe(t, md_map)
        return list(md_map.keys())

    def rank(self, t, md_map, universe_syms):
        if hasattr(self.delegate, "rank"):
            return self.delegate.rank(t, md_map, universe_syms)
        return list(universe_syms)

    def entry_signal(self, is_opening, sym, row, ctx=None):
        sig = self.registry.active_for(self.SIDE, sym, self._row_time(row))
        if sig is None:
            return None
        out = self.delegate.entry_signal(is_opening, sym, row, ctx=ctx)
        if out is not None:
            self.last_entry_signal = sig
            try:
                reason = getattr(out, "reason", "") or ""
                source = sig.source or "external"
                signal_id = f":{sig.signal_id}" if sig.signal_id else ""
                out.reason = f"{reason}|external_signal:{source}{signal_id}"
            except Exception:
                pass
        return out

    def manage_position(self, sym, row, pos, ctx=None):
        return self.delegate.manage_position(sym, row, pos, ctx=ctx)

    def sync_after_external_fill(self, *args, **kwargs):
        if hasattr(self.delegate, "sync_after_external_fill"):
            return self.delegate.sync_after_external_fill(*args, **kwargs)
        return None


class V21ExternalSignalLong(_V21ExternalSignalBase):
    SIDE = "LONG"
    PARAMS_KEY = "strategy_params_long"
    DEFAULT_DELEGATE = DEFAULT_LONG_CLASS


class V21ExternalSignalShort(_V21ExternalSignalBase):
    SIDE = "SHORT"
    PARAMS_KEY = "strategy_params_short"
    DEFAULT_DELEGATE = DEFAULT_SHORT_CLASS
