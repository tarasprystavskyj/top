# top_1 Telegram/Copytrader Handoff

Local source workspace: `/srv/codex-projects/python_scripts/top_1_telegram_signals`

Local git commit prepared for upload: `993d470b4137198932ddca09d52914900234ed1c` (`Harden exchange-native copytrader rankings`).

Local archive generated from tracked files:

- `/tmp/top1-pr-upload/top_1_telegram_signals_snapshot_993d470.zip`
- manifest: `/tmp/top1-pr-upload/top_1_telegram_signals_manifest_993d470.txt`
- patch: `/tmp/top1-pr-upload/top_1_telegram_signals_changes_993d470.patch`

## Current agent status

- Copytrader OKX path is active and uses exchange-native sort/filter rank slices as primary ranking, not full-table local crawling.
- Latest OKX capped rank slice produced validator-clean JSONL under `.agent/copytrader_logs/runs/okx/20260524/`.
- Latest ranking output is research-only, with `not_order_execution=true` and `order_execution_allowed=false`.
- Bybit remains blocked for live collection until an official unauthenticated public leaderboard endpoint or explicit legal/ToS approval exists.
- Night consilium remains runnable, but the prior All52 weekly CS momentum survivor is `fragile_rejected_for_promotion` and should not be promoted to paper/live.

## Verification

From `/srv/codex-projects/python_scripts/top_1_telegram_signals`:

```bash
python3 -m py_compile copytrader_research/tools/collect_okx_rank_table.py copytrader_research/tools/rank_public_traders.py
python3 tests/run_copytrader_tests.py
# 30 copytrader tests passed on 2026-05-24
```

## Upload blocker

Direct `git push` to `https://github.com/tarasprystavskyj/top.git` failed in this environment with:

```text
fatal: could not read Username for 'https://github.com': No such device or address
```

SSH auth also failed with `Permission denied (publickey)`. I did not read `.env`, key files, tokens, or secrets. This PR therefore records the handoff online via the GitHub connector. To upload the full tracked source tree, run from the local workspace with a valid GitHub credential:

```bash
cd /srv/codex-projects/python_scripts/top_1_telegram_signals
git remote add top-online https://github.com/tarasprystavskyj/top.git 2>/dev/null || true
git push -u top-online HEAD:codex/telegram-copytrader-paper-live
```

Then open a PR from `codex/telegram-copytrader-paper-live` into `main`.
