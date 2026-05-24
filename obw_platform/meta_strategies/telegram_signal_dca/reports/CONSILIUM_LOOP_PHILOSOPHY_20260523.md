# Consilium Loop Philosophy for Binance 475183 and DarkKnight V21 Tuning

Source archive inspected:
`/var/www/vps2.happyuser.info/top/temp/doc_2026-05-21_15-12-55.claude.zip`

Relevant source sections:
- `.claude/loop.md`: one tick is one search wave; orchestrator dispatches planning, testing, evaluation, constraints, journal write, and summary.
- `.claude/agents/orchestrator.md`: read fresh state, build compact journal, run cheap tests first, full tests only on finalists, evaluate realism, enforce hard constraints outside Brain, write journal before returning.
- `.claude/agents/brain-planning.md`: Brain only plans mutations; use accumulated learnings, top candidates, promotion history, recent waves, and stall count; keep mutations small and interpretable.
- `.claude/agents/brain-evaluation.md`: Brain evaluates candidates and validates realism; MDD/risk penalties dominate small return gains.
- `.claude/agents/critic.md`: reject or flag unrealistic winners, overfit artifacts, impossible configs, excessive order frequency, high live risk, and weak execution assumptions.
- `.claude/agents/tester.md`: Tester runs backtests and applies automatic rejects for margin calls, low trades, unrealized PnL breach, and backtest errors.

## Practical Loop Rules

1. Run the search as waves, not one-off guesses.
   Each loop should produce a compact journal, a candidate manifest, test results, Brain/evaluator notes, and a promoted-or-rejected outcome.

2. Use two-stage testing.
   First run cheap scans across all candidates/windows. Only the best surviving candidates get deeper/full-window tests.

3. Keep mutations explainable.
   Prefer 2-4 parameter changes per candidate. Include at least one conservative variant and one aggressive variant. If several waves stall, add from-scratch challengers with a clearly stated philosophy.

4. Treat Brain as advisory and constraints as mandatory.
   The loop may let an evaluator rank candidates, but the orchestrator must independently reject candidates that violate risk constraints.

5. Promote only with saved state.
   A candidate becomes a champion only after journal update, metrics capture, and realism notes are written. Never rely on memory from a prior wave.

6. Penalize risk heavily.
   Small return improvements are not enough if MDD, min unrealized PnL, margin risk, or order realism worsens materially.

7. Preserve source-specific assumptions.
   Binance copy and Telegram DarkKnight should be tuned separately because their signal cadence, symbol universe, entry direction source, and available history differ.

## Adaptation for Requested V21 Loops

These are the task-specific rules to apply on top of the consilium docs.

### Binance champion loop: lead `4751838302089254401`

- Build the symbol universe from the instruments traded by this lead.
- Collect NPZ/price windows for that universe with explicit window metadata.
- Treat Binance signal direction as authoritative for the entry leg.
- Enter only one leg per signal: long if the Binance-derived signal says long, short if it says short.
- Do not flip, suppress, resize, or exit because the trend detector changed its opinion.
- Consilium may compute warmup and trend state as context and as a quality/risk diagnostic, but trend must not decide which leg to place.
- Optimize V21 parameters around this single-leg, signal-directed behavior.
- Rank candidates on return, drawdown, no margin calls, min unrealized PnL, trade count, and robustness across windows.

### DarkKnight Telegram loop

- Build the symbol universe from parsed `darkknighttrade` signals.
- Collect NPZ/price windows for that universe with explicit window metadata.
- Treat parsed Telegram signal direction as authoritative for the entry leg.
- Enter only one leg per signal.
- Do not react to trend after entry direction is known.
- Use consilium warmup/trend handling only to decide whether the data window is sufficiently initialized and to annotate market context.
- Tune V21 separately from Binance; do not mix performance journals or promote one source's champion into the other source without retesting.

## Hard Risk Gates

- No live exchange orders in these loops; paper/backtest only.
- Reject any candidate with margin calls.
- Reject candidates with min unrealized PnL below `-50%`; flag high risk if below `-40%`.
- Reject candidates with implausible order frequency or impossible logic.
- Prefer robust multi-window performance over a single lucky segment.
- Record limitations when Telegram/Binance metrics are not directly comparable.

## Notes

The archive does not contain a dedicated warmup/trend policy for this exact Binance/DarkKnight signal-following task. The direct consilium instruction is that trend filter parameters were locked in the original ENA search. For these requested loops, the practical adaptation is: consilium owns warmup and trend diagnosis, while the trading leg comes only from the source signal and does not react to trend.
