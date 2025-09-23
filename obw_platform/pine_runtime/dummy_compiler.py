"""A lightweight Pine Script compiler that relies on inline annotations.

This compiler is intentionally simplistic – it expects Pine scripts to provide
special annotations describing which Python strategy class should be used and
how its parameters should be initialised.  It is primarily intended for local
use and unit tests, while real deployments can plug in a proper compiler by
registering it via :func:`register_compiler`.
"""
from __future__ import annotations

import hashlib
import json
import re
import textwrap
from pathlib import Path
from typing import Any, Dict

from .compiler import (
    BasePineCompiler,
    PineStrategyArtifact,
    normalise_cache_dir,
    register_compiler,
)


class DummyCompiler(BasePineCompiler):
    """Translate annotated Pine scripts into thin wrapper strategies."""

    STRATEGY_PATTERN = re.compile(r"//@obw_strategy\s+(?P<class>[\w.]+)")
    PARAMS_PATTERN = re.compile(r"//@obw_strategy_params\s+(?P<payload>\{.*\})")
    CFG_PATTERN = re.compile(r"//@obw_cfg_override\s+(?P<payload>\{.*\})")

    def compile(self, pine_path: str | Path, *, force: bool = False) -> PineStrategyArtifact:
        pine_file = Path(pine_path).expanduser().resolve()
        source = pine_file.read_text(encoding="utf-8")

        class_match = self.STRATEGY_PATTERN.search(source)
        if not class_match:
            raise ValueError(
                "Annotated Pine script must include a line starting with //@obw_strategy <python.path.ClassName>."
            )
        full_class_path = class_match.group("class").strip()
        if "." not in full_class_path:
            raise ValueError(
                f"Invalid //@obw_strategy annotation '{full_class_path}'. Expected a dotted module path."
            )
        base_module, base_class = full_class_path.rsplit(".", 1)

        params: Dict[str, Any] = {}
        for match in self.PARAMS_PATTERN.finditer(source):
            payload = match.group("payload").strip()
            params.update(json.loads(payload))

        cfg_overrides: Dict[str, Any] = {}
        for match in self.CFG_PATTERN.finditer(source):
            payload = match.group("payload").strip()
            cfg_overrides.update(json.loads(payload))

        cache_root = normalise_cache_dir(
            self.options.get("cache_dir"),
            default=Path(__file__).resolve().parent.parent / "_pine_cache",
        )
        package_prefix = str(self.options.get("package_prefix", "pine_generated"))
        cache_root.mkdir(parents=True, exist_ok=True)

        module_parts = base_module.split(".")
        package_root = None
        try:
            package_root = pine_file.resolve().parents[len(module_parts) - 1]
        except IndexError:
            package_root = pine_file.parent

        digest = hashlib.sha1(source.encode("utf-8")).hexdigest()[:12]
        package_name = f"{package_prefix}_{digest}"
        package_dir = cache_root / package_name
        strategy_py = package_dir / "strategy.py"
        metadata_path = package_dir / "metadata.json"

        if force or not strategy_py.exists() or not metadata_path.exists():
            package_dir.mkdir(parents=True, exist_ok=True)
            for parent in [cache_root, package_dir]:
                init_file = parent / "__init__.py"
                if not init_file.exists():
                    init_file.write_text("", encoding="utf-8")

            strategy_code = textwrap.dedent(
                f'''
                """Auto-generated adapter around {full_class_path}."""
                from {base_module} import {base_class} as _BaseStrategy


                class Strategy(_BaseStrategy):
                    """Thin wrapper generated for Pine integration."""
                    pass
                '''
            ).strip()
            strategy_py.write_text(strategy_code + "\n", encoding="utf-8")

            metadata = {
                "source": str(pine_file),
                "base_class": full_class_path,
                "digest": digest,
                "params": params,
                "config_overrides": cfg_overrides,
            }
            metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False), encoding="utf-8")

        sys_path_entries = [str(cache_root)]
        if package_root and str(package_root) not in sys_path_entries:
            sys_path_entries.append(str(package_root))

        return PineStrategyArtifact(
            module=f"{package_name}.strategy",
            class_name="Strategy",
            strategy_params=params,
            config_overrides=cfg_overrides,
            metadata={
                "source": str(pine_file),
                "base_class": full_class_path,
                "cache_package": package_name,
            },
            sys_path_entries=sys_path_entries,
        )


def _register() -> None:
    register_compiler("dummy", DummyCompiler)


_register()
