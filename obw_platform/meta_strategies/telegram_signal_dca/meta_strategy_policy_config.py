#!/usr/bin/env python3
"""Configurable copy-signal DCA policy loader.

The default HYPE champion policy remains the fallback. Config files let one
copy-signal meta-strategy choose symbol-specific DCA/V21-like sizing without
changing the live execution boundary.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

try:
    import yaml  # type: ignore
except Exception:  # pragma: no cover - yaml is optional
    yaml = None

try:
    from . import hype_cap100_champion_dca_strategy as champion_dca
except ImportError:  # pragma: no cover - script import path
    import hype_cap100_champion_dca_strategy as champion_dca


DEFAULT_CONFIG_DIR = Path(__file__).resolve().parent / "meta_strategy_configs"


def _as_float(raw: Any, default: float = 0.0) -> float:
    try:
        return float(raw)
    except Exception:
        return default


def _as_list(raw: Any) -> List[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return list(raw)
    if isinstance(raw, tuple):
        return list(raw)
    return [raw]


def _norm_symbol(raw: Any) -> str:
    return str(raw or "").upper().replace("/", "").replace("-", "").replace(":USDT", "").strip()


def _read_config(path: Path) -> Dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() in {".yaml", ".yml"}:
        if yaml is None:
            raise RuntimeError(f"yaml config requires PyYAML: {path}")
        data = yaml.safe_load(text) or {}
    else:
        data = json.loads(text)
    if not isinstance(data, dict):
        raise RuntimeError(f"meta-strategy config must be a mapping: {path}")
    data["_config_path"] = str(path)
    return data


def _candidate_paths(config_dir: Path, symbol: str) -> List[Path]:
    base = _norm_symbol(symbol)
    names = [
        f"{base}.json",
        f"{base}.yaml",
        f"{base}.yml",
        "default.json",
        "default.yaml",
        "default.yml",
    ]
    return [config_dir / name for name in names]


def load_config_for_symbol(config_dir: str, symbol: str) -> Optional[Dict[str, Any]]:
    if not config_dir:
        return None
    root = Path(config_dir)
    if not root.exists():
        raise RuntimeError(f"meta-strategy config dir does not exist: {root}")
    wanted = _norm_symbol(symbol)
    for path in _candidate_paths(root, wanted):
        if not path.exists():
            continue
        data = _read_config(path)
        symbols = [_norm_symbol(x) for x in _as_list(data.get("symbols") or data.get("symbol"))]
        if symbols and wanted not in symbols and "*" not in symbols:
            continue
        return data
    return None


class ConfiguredDcaPolicy:
    def __init__(self, config: Dict[str, Any]):
        self.config = dict(config)
        self.name = str(self.config.get("name") or Path(str(self.config.get("_config_path") or "configured")).stem)
        self.candidate_index = self.config.get("candidate_index", self.name)
        self.params = dict(self.config.get("params") or {})
        self.dca = dict(self.config.get("dca") or {})

    def dca_levels(self, entry_price: float) -> List[float]:
        absolute = self.dca.get("levels")
        if absolute:
            return [_as_float(x) for x in _as_list(absolute)]
        drops = [_as_float(x) for x in _as_list(self.dca.get("drops_pct"))]
        if not drops:
            drops = list(champion_dca.DCA_DROPS_PCT)
        levels: List[float] = []
        last = float(entry_price)
        for drop in drops:
            last *= 1.0 - drop / 100.0
            levels.append(last)
        return levels

    def _contract_notional(self, price: float, contracts: float) -> float:
        contract_size = _as_float(self.dca.get("contract_size_base"), 0.0)
        if contract_size <= 0:
            contract_size = _as_float(self.config.get("contract_size_base"), 0.0)
        if contract_size <= 0:
            raise RuntimeError(f"contract_size_base is required for contracts sizing policy {self.name}")
        return float(price) * contract_size * float(contracts)

    def build_plan(self, equity: float, args: Any, entry_price: float) -> Dict[str, Any]:
        target = min(float(args.initial_target_notional), float(args.max_gross_notional_usdt), max(float(equity), 0.0))
        levels = self.dca_levels(entry_price)
        if self.dca.get("base_contracts") is not None:
            base = self._contract_notional(entry_price, _as_float(self.dca.get("base_contracts"), 1.0))
        elif self.dca.get("base_notional_usdt") is not None:
            base = _as_float(self.dca.get("base_notional_usdt"), 0.0)
        else:
            base_pct = _as_float(self.dca.get("base_pct", self.params.get("fresh_base_pct", 28.0)), 28.0)
            base = target * base_pct / 100.0
        base = min(max(base, 0.0), target)

        add_notionals: List[float]
        add_contracts = [_as_float(x) for x in _as_list(self.dca.get("add_contracts"))]
        if add_contracts:
            add_notionals = [
                self._contract_notional(levels[min(idx, len(levels) - 1)] if levels else entry_price, contracts)
                for idx, contracts in enumerate(add_contracts)
            ]
        elif self.dca.get("add_notionals_usdt") is not None:
            add_notionals = [_as_float(x) for x in _as_list(self.dca.get("add_notionals_usdt"))]
        else:
            mode = str(self.dca.get("add_mode") or self.params.get("dca_add_mode") or "multiplier").lower()
            multipliers = [_as_float(x) for x in _as_list(self.dca.get("multipliers"))] or list(champion_dca.DCA_MULTIPLIERS)
            if mode == "fixed":
                fixed = _as_float(self.dca.get("add_notional_usdt", self.params.get("dca_add_notional_usdt", 0.0)), 0.0)
                add_notionals = [fixed for _ in multipliers]
            else:
                raw = [base * mult for mult in multipliers]
                remaining = max(target - base, 0.0)
                scale = min(1.0, remaining / max(sum(raw), 1e-12))
                add_notionals = [x * scale for x in raw]

        max_adds = len(levels)
        if max_adds:
            add_notionals = (add_notionals + [0.0] * max_adds)[:max_adds]
        return {
            "target_notional": target,
            "base_notional": base,
            "add_notionals": add_notionals,
            "dca_add_mode": str(self.dca.get("add_mode") or "configured"),
            "min_order_notional": _as_float(self.dca.get("min_order_notional_usdt"), 0.0),
            "levels": levels,
            "candidate_index": self.candidate_index,
            "strategy_config_name": self.name,
            "strategy_config_path": str(self.config.get("_config_path") or ""),
            "strategy_policy": str(self.config.get("strategy_policy") or f"configured_dca:{self.name}"),
        }

    def build_trade_from_source(self, pos: Dict[str, Any], plan: Dict[str, Any], *, now, mark: Optional[float], iso_fn) -> Dict[str, Any]:
        trade = champion_dca.build_trade_from_source(pos, plan, now=now, mark=mark, iso_fn=iso_fn)
        trade["strategy_candidate_index"] = plan.get("candidate_index")
        trade["strategy_config_name"] = plan.get("strategy_config_name")
        trade["strategy_config_path"] = plan.get("strategy_config_path")
        trade["strategy_policy"] = plan.get("strategy_policy")
        return trade

    def base_entry_intent(self, trade: Dict[str, Any], *, expected_price: float) -> Dict[str, Any]:
        intent = champion_dca.base_entry_intent(trade, expected_price=expected_price)
        intent["candidate_index"] = trade.get("strategy_candidate_index", self.candidate_index)
        intent["strategy_policy"] = str(trade.get("strategy_policy") or f"configured_dca:{self.name}:base")
        return intent

    def dca_entry_intents(self, trade: Dict[str, Any], *, mark: Optional[float], allow_dca: bool) -> List[Dict[str, Any]]:
        intents = champion_dca.dca_entry_intents(trade, mark=mark, allow_dca=allow_dca)
        for intent in intents:
            intent["candidate_index"] = trade.get("strategy_candidate_index", self.candidate_index)
            intent["strategy_policy"] = str(trade.get("strategy_policy") or f"configured_dca:{self.name}:dca")
        return intents


def resolve_policy(args: Any, symbol: str):
    config_dir = str(getattr(args, "meta_strategy_config_dir", "") or "")
    config_path = str(getattr(args, "strategy_config", "") or "")
    if config_path:
        return ConfiguredDcaPolicy(_read_config(Path(config_path)))
    config = load_config_for_symbol(config_dir, symbol)
    if config:
        return ConfiguredDcaPolicy(config)
    return champion_dca


def describe_policy(args: Any, symbol: str) -> Dict[str, Any]:
    policy = resolve_policy(args, symbol)
    if isinstance(policy, ConfiguredDcaPolicy):
        return {
            "source": "config",
            "name": policy.name,
            "candidate_index": policy.candidate_index,
            "strategy_policy": str(policy.config.get("strategy_policy") or f"configured_dca:{policy.name}"),
            "config_path": str(policy.config.get("_config_path") or ""),
        }
    return {
        "source": "hype_champion_default",
        "name": "hype_cap100_champion_dca_strategy",
        "candidate_index": champion_dca.CHAMPION_CANDIDATE_INDEX,
        "strategy_policy": "copy_source_open_base_entry",
        "config_path": "",
    }
