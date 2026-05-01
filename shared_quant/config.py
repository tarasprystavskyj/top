from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, MutableMapping

try:
    import yaml
except Exception:  # pragma: no cover
    yaml = None


def load_yaml(path: str | Path) -> dict:
    """Load YAML file as dict."""
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data or {}


def save_yaml(data: Mapping[str, Any], path: str | Path) -> None:
    """Save mapping to YAML."""
    if yaml is None:
        raise RuntimeError("PyYAML is required: pip install pyyaml")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        yaml.safe_dump(dict(data), f, sort_keys=False, allow_unicode=True)


def load_json(path: str | Path) -> Any:
    p = Path(path)
    with p.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_json(data: Any, path: str | Path, *, indent: int = 2) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=indent)


def deep_merge(base: Mapping[str, Any], override: Mapping[str, Any]) -> dict:
    """Recursively merge override into base and return a new dict."""
    out = dict(base)
    for key, val in dict(override).items():
        if isinstance(val, Mapping) and isinstance(out.get(key), Mapping):
            out[key] = deep_merge(out[key], val)
        else:
            out[key] = val
    return out


def canonical_json(data: Any) -> str:
    """Stable JSON string for hashing configs/results."""
    return json.dumps(
        data,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


def stable_hash(data: Any, *, length: int = 16) -> str:
    """Stable SHA256 hash for configs, params, and run metadata."""
    h = hashlib.sha256(canonical_json(data).encode("utf-8")).hexdigest()
    return h[:length] if length and length > 0 else h


def get_nested(data: Mapping[str, Any], path: str, default: Any = None, sep: str = ".") -> Any:
    cur: Any = data
    for part in path.split(sep):
        if not isinstance(cur, Mapping) or part not in cur:
            return default
        cur = cur[part]
    return cur


def set_nested(data: MutableMapping[str, Any], path: str, value: Any, sep: str = ".") -> None:
    cur: MutableMapping[str, Any] = data
    parts = path.split(sep)
    for part in parts[:-1]:
        nxt = cur.get(part)
        if not isinstance(nxt, MutableMapping):
            nxt = {}
            cur[part] = nxt
        cur = nxt
    cur[parts[-1]] = value
