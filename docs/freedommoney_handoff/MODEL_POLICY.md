# FREEDOMMONEY Claude Model Policy

Default model: `haiku`.

Use `haiku` for:

- running existing commands
- collecting OHLCV data
- reading CSV reports
- appending `AGENT_STATE.md`
- appending `EXPERIMENT_LEDGER.csv`
- packing snapshots
- small guided sweeps around existing configs

Use `sonnet` only for:

- backtester/tuner code edits
- broken data pipeline debugging
- failed loop recovery
- every Nth review pass if enabled

Current loop policy:

```bash
CLAUDE_MODEL=haiku
CLAUDE_FALLBACK_MODEL=sonnet
SONNET_REVIEW_EVERY_N=5
```

If token cost matters more than review quality, set:

```bash
SONNET_REVIEW_EVERY_N=0
```

The loop uses `--model` when available. If the installed Claude CLI does not expose `--model`, it exports:

```bash
ANTHROPIC_MODEL=$CLAUDE_MODEL
```

That may or may not be honored by the installed CLI version. Verify with:

```bash
commands/freedom_12_model_status.sh
```
