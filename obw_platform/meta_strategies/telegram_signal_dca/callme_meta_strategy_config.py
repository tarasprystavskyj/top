#!/usr/bin/env python3
"""Helpers for resolving Callme meta-strategy symbol configs."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any, Dict


CONFIG_SCHEMA = "callme_meta_strategy_config_v1"


def compact_symbol_key(value: Any) -> str:
    text = str(value or "").upper().strip()
    if not text:
        return ""
    return text.replace("/", "").replace("-", "").replace(":", "")


def load_callme_meta_strategy_config(path: str) -> Dict[str, Any]:
    cfg_path = Path(path)
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError("failed to read Callme meta-strategy config %s: %s" % (cfg_path, exc)) from exc
    if not isinstance(cfg, dict):
        raise ValueError("Callme meta-strategy config must contain a JSON object: %s" % cfg_path)
    if cfg.get("schema") != CONFIG_SCHEMA:
        raise ValueError("unsupported Callme meta-strategy schema in %s: %s" % (cfg_path, cfg.get("schema")))
    cfg["_config_path"] = str(cfg_path)
    return cfg


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = copy.deepcopy(value)
    return out


def _symbol_entry(symbols: Dict[str, Any], symbol: Any) -> Dict[str, Any]:
    compact = compact_symbol_key(symbol)
    if not compact:
        return {}
    for key, value in symbols.items():
        if key == "*":
            continue
        if compact_symbol_key(key) == compact and isinstance(value, dict):
            return value
    return {}


def _strategy_override_fields(entry: Dict[str, Any]) -> Dict[str, Any]:
    strategy_override = entry.get("strategy_override")
    if isinstance(strategy_override, dict):
        fields = strategy_override.get("override_fields")
        if isinstance(fields, dict):
            return fields
    strategy_config = entry.get("strategy_config")
    if isinstance(strategy_config, dict):
        return strategy_config
    return {}


def resolve_symbol_strategy_config(meta_config: Dict[str, Any], symbol: Any) -> Dict[str, Any]:
    """Resolve DCA/v21 policy for one Callme symbol without exchange metadata.

    Resolution order:
    1. Start with ``default_symbol_config``.
    2. If ``symbols.<SYMBOL>.strategy_override.override_fields`` exists,
       deep-merge it over the default.
    3. ``symbols.<SYMBOL>.strategy_config`` is accepted as a compatibility
       alias for sibling branches that used that name first.
    4. For ``*`` or unknown symbols, return the default unchanged.
    """
    if not isinstance(meta_config, dict):
        raise ValueError("meta_config must be a dict")
    default_config = meta_config.get("default_symbol_config")
    if not isinstance(default_config, dict):
        raise ValueError("Callme meta-strategy config requires default_symbol_config")

    symbols = meta_config.get("symbols") if isinstance(meta_config.get("symbols"), dict) else {}
    entry = {} if str(symbol or "").strip() == "*" else _symbol_entry(symbols, symbol)
    override = _strategy_override_fields(entry)
    strategy_config = _deep_merge(default_config, override) if override else copy.deepcopy(default_config)
    source_symbol = compact_symbol_key(symbol)
    source = "symbols.%s.strategy_override.override_fields" % source_symbol if override else "default_symbol_config"
    return {
        "symbol": source_symbol or "*",
        "config_source": source,
        "has_symbol_override": bool(override),
        "strategy_config": strategy_config,
    }
