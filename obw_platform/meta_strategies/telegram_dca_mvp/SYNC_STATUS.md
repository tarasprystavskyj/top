# Telegram DCA MVP Sync Status

Updated: 2026-05-19
Branch: `codex/telegram-dca-mvp-sync`
Scope: `obw_platform/meta_strategies/telegram_dca_mvp`

## Purpose

This lane is the canonical Telegram signal -> DCA research workspace for
`https://t.me/darkknighttrade`.

It compares:

- simple Telegram signal execution via `run_telegram_simple_baseline_npz.py`;
- DCA execution via `run_telegram_dca_mvp_npz.py`;
- selector/filter research under `telegram_dca_next_agent_bundle/research`.

## Current Evidence

The strongest known result is a filtered `edge_min5` subset:

- 83 signal rows;
- 68 opened trades;
- +9.2276% PnL;
- -1.1122% MDD.

The full 312-signal universe with the same execution profile was negative:

- 251 opened trades;
- -16.1257% PnL;
- -20.1267% MDD.

This means selector validation is the main problem. DCA is not promoted.

## Commit Policy

Commit code, compact docs, and compact state contracts from this folder.
Do not commit local NPZ parts, DBs, zip/tar bundles, charts, raw sweep outputs,
or full worker bundles.

Large local artifacts remain useful for research but are ignored by
`.gitignore`.

## Safety

Research-only. No live execution, no daemon changes, no broker/order paths, no
secrets, and no paper-live promotion without explicit human approval.
