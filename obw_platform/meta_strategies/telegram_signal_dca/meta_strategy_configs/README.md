# Telegram Signal DCA Meta-Strategy Configs

This folder holds optional per-symbol policy configs for the copy-signal
meta-strategy.

If no config is provided, the existing HYPE candidate 189 champion policy is
used unchanged. A runner can opt in with:

```bash
--meta-strategy-config-dir obw_platform/meta_strategies/telegram_signal_dca/meta_strategy_configs
```

The live runner still only executes explicit strategy intents. These files only
change how the meta-strategy sizes base/DCA intents and levels for a source
symbol.

`*_contract_size_base` settings are exchange-contract-aware and should be used
only for a specific live venue after checking market metadata.
