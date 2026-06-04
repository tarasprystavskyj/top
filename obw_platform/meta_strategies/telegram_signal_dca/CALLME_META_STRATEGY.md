# Callme Meta-Strategy

Callme is a multi-symbol Binance copy-trading lead. The strategy must be
represented as a Callme meta-strategy with symbol configuration inside the
meta-strategy config, not as a separate strategy per coin.

Canonical config:

```text
obw_platform/meta_strategies/telegram_signal_dca/configs/callme_meta_strategy_live.json
```

The current multi-symbol runner in `binance_online_copytrading` is shadow-only.
The guarded live runner remains a lower-level execution component until a
multi-symbol live adapter reads the Callme meta-strategy config directly.

For a newly observed Callme symbol:

1. Resolve the canonical `BASE/USDT:USDT` market on each exchange.
2. If the symbol has no explicit entry in `symbols`, apply
   `default_symbol_config`.
3. Treat that default as a placeholder derived from the HYPE/candidate-189
   guarded DCA baseline.
4. Replace the placeholder after tuning on all historical Callme signals.

TODO: optimize/tune across all historical Callme signals to obtain an averaged
Callme-specific default configuration for new symbols. The working assumption is
that this lead chooses new symbols in a similar style to prior symbols.

Legacy files with `legacy_amd_single_symbol` in the name are compatibility
canaries for the old AMD-only live process. They are not the Callme strategy.
