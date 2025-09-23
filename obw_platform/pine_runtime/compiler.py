"""Compiler registry and artefact helpers for Pine Script integration."""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, MutableMapping, Optional, Type


# ``slots`` support for ``dataclass`` was only added in Python 3.10.
# The production runners for this project still target Python 3.8, so
# we must avoid passing the ``slots`` keyword to remain compatible.
@dataclass
class PineStrategyArtifact:
    """Describes the result of compiling a Pine Script into a Python strategy."""

    module: str
    class_name: str
    strategy_params: Dict[str, Any] = field(default_factory=dict)
    config_overrides: Dict[str, Any] = field(default_factory=dict)
    metadata: Dict[str, Any] = field(default_factory=dict)
    sys_path_entries: List[str] = field(default_factory=list)

    def ensure_sys_path(self) -> None:
        """Ensure that the Python module containing the compiled strategy is importable."""

        for entry in self.sys_path_entries:
            if entry and entry not in sys.path:
                sys.path.insert(0, entry)

    def load_class(self):
        """Import and return the compiled strategy class."""

        self.ensure_sys_path()
        module = importlib.import_module(self.module)
        try:
            return getattr(module, self.class_name)
        except AttributeError as exc:  # pragma: no cover - defensive branch
            raise ImportError(
                f"Compiled module '{self.module}' does not expose class '{self.class_name}'."
            ) from exc


class BasePineCompiler:
    """Base interface for Pine Script compilers."""

    def __init__(self, **options: Any) -> None:
        self.options = options

    def compile(
        self,
        pine_path: str | Path,
        *,
        force: bool = False,
    ) -> PineStrategyArtifact:
        raise NotImplementedError


_COMPILERS: Dict[str, Type[BasePineCompiler]] = {}


def register_compiler(name: str, compiler_cls: Type[BasePineCompiler]) -> None:
    key = name.strip().lower()
    if not key:
        raise ValueError("Compiler name must be a non-empty string")
    _COMPILERS[key] = compiler_cls


def get_registered_compilers() -> Iterable[str]:
    return sorted(_COMPILERS.keys())


def load_compiler(name: str, **options: Any) -> BasePineCompiler:
    key = name.strip().lower()
    if key not in _COMPILERS:
        raise ValueError(
            f"Unknown Pine compiler '{name}'. Known compilers: {', '.join(get_registered_compilers()) or 'none'}."
        )
    cls = _COMPILERS[key]
    return cls(**options)


def normalise_cache_dir(path: str | Path | None, *, default: Path) -> Path:
    if path is None:
        return default
    return Path(path).expanduser().resolve()


def merge_strategy_params(
    base: Optional[MutableMapping[str, Any]],
    overrides: Optional[MutableMapping[str, Any]],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = {}
    if base:
        merged.update(base)
    if overrides:
        merged.update(overrides)
    return merged
