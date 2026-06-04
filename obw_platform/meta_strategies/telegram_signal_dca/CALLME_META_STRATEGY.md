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
3. If `symbols.<SYMBOL>.strategy_override.override_fields` exists, deep-merge
   those fields over `default_symbol_config`.
4. Keep exchange metadata under `exchange_symbols`; do not mix it into DCA/v21
   policy.

The current default is a pooled Callme public-history research default selected
from visible Binance copy-trading history on 2026-06-04. It is not a production
live approval: the sample is one public-history window, per-symbol overrides are
skipped until each symbol clears the min-trade gate, and the multi-symbol live
adapter still must read this meta-strategy config directly before real order
eligibility.

Per-symbol override rule:

1. Start with the pooled default.
2. Tune a symbol-only candidate only when the symbol has enough closed trades.
3. Allow only small DCA/v21 override fields.
4. Reject an override if it worsens max MTM drawdown, liquidation touches, or
   max notional versus the pooled default.

Legacy files with `legacy_amd_single_symbol` in the name are compatibility
canaries for the old AMD-only live process. They are not the Callme strategy.
