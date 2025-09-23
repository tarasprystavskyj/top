"""Adapter strategy that bridges TradingView Pine scripts to native strategies."""
from __future__ import annotations

from typing import Any, Mapping, Optional

if __package__ and __package__.startswith("obw_platform."):
    from obw_platform.pine_runtime import PineStrategyArtifact, load_compiler
    from obw_platform.pine_runtime.compiler import merge_strategy_params
else:  # When imported as a top-level ``strategies`` module by legacy runners.
    from pine_runtime import PineStrategyArtifact, load_compiler
    from pine_runtime.compiler import merge_strategy_params


class PineBridgeStrategy:
    """Strategy adapter that instantiates a compiled Pine strategy."""

    def __init__(self, cfg: Mapping[str, Any]) -> None:
        self.cfg = dict(cfg or {})
        sp_raw = dict(self.cfg.get("strategy_params") or {})

        self._artifact: Optional[PineStrategyArtifact] = None
        self._impl = self._initialise_impl(sp_raw)

    # ------------------------------------------------------------------
    # Backtester facing API — delegated to the compiled strategy.
    # ------------------------------------------------------------------
    def universe(self, t: Any, md_map):
        return self._impl.universe(t, md_map)

    def rank(self, t: Any, md_map, universe_syms):
        return self._impl.rank(t, md_map, universe_syms)

    def entry_signal(self, t: Any, symbol: str, row, ctx=None):
        return self._impl.entry_signal(t, symbol, row, ctx=ctx)

    def manage_position(self, symbol: str, row, position, ctx=None):
        return self._impl.manage_position(symbol, row, position, ctx=ctx)

    # ------------------------------------------------------------------
    @property
    def artifact(self) -> PineStrategyArtifact:
        if not self._artifact:
            raise RuntimeError("Pine strategy was not initialised correctly")
        return self._artifact

    def _initialise_impl(self, strategy_params: Mapping[str, Any]):
        compiler_info = strategy_params.get("compiler") or {}
        compiled_module_info = strategy_params.get("compiled_module")
        pine_path = strategy_params.get("pine_script_path")
        auto_compile = strategy_params.get("auto_compile", True)

        if compiled_module_info:
            artifact = PineStrategyArtifact(
                module=compiled_module_info["module"],
                class_name=compiled_module_info.get("class_name", "Strategy"),
                strategy_params=dict(compiled_module_info.get("strategy_params") or {}),
                config_overrides=dict(compiled_module_info.get("config_overrides") or {}),
                metadata=dict(compiled_module_info.get("metadata") or {}),
                sys_path_entries=list(compiled_module_info.get("sys_path_entries") or []),
            )
        elif auto_compile:
            if not pine_path:
                raise ValueError("pine_script_path must be provided when auto_compile is enabled")
            compiler_name = compiler_info.get("name", "dummy")
            compiler_opts = {k: v for k, v in compiler_info.items() if k != "name"}
            compiler = load_compiler(compiler_name, **compiler_opts)
            artifact = compiler.compile(pine_path, force=bool(compiler_info.get("force", False)))
        else:
            raise ValueError("Either compiled_module must be supplied or auto_compile must be enabled.")

        self._artifact = artifact
        self.metadata = dict(artifact.metadata)
        strategy_cls = artifact.load_class()

        passthrough_keys = {"pine_script_path", "compiler", "compiled_module", "auto_compile", "base_strategy_params"}
        base_params = dict(strategy_params.get("base_strategy_params") or {})
        for key, value in strategy_params.items():
            if key not in passthrough_keys:
                base_params.setdefault(key, value)
        final_params = merge_strategy_params(base_params, artifact.strategy_params)

        impl_cfg = dict(self.cfg)
        impl_cfg["strategy_params"] = final_params
        if artifact.config_overrides:
            impl_cfg.update(artifact.config_overrides)

        return strategy_cls(impl_cfg)

    def __getattr__(self, item: str):  # pragma: no cover - delegated attributes
        return getattr(self._impl, item)
