# Worker Task: HYPE rnd5337 Pine Static Signals

Research-only. Do not place live orders, do not read secrets, do not use private account pages.

Context:

- We now treat `first_bar_close` as the offline review model for open/close placement because paper-live/live should execute immediately on BingX mark price with minimum latency.
- Candidate added as a Pine preset: `rnd5337_t500_b12_s0p953-1p3-1p442-1p767_w0p597-0p82-1p151-1p868`.
- First-bar-close replay result: equity `500 -> 758.870428`, net `+51.774086%`, PF `3.699657`, max MTM DD `-12.268260%`.
- Binance `avgCost` / `avgClosePrice` are metadata only, not executable fills.

Files in this archive:

- `C - LONG - MA driven HYPE ie500 fixed champion.pine`
- `C - LONG - MA driven 1.pine`
- `hype_rnd5337_firstbarclose_fixed_signals.pine`
- `hype_long_open_close_chart.html`
- `signal_events.csv`
- `README_tradingview_static_overlay.md`
- `README_signal_chart_artifact.md`

What changed in the two strategy files:

- Added input `DCA preset` with option `rnd5337 HYPE FBC`.
- Added optional static overlay input `Show fixed OPEN/CLOSE signals`.
- Embedded 122 fixed OPEN timestamps and 122 fixed CLOSE timestamps.
- The static labels are visual only; they do not drive strategy orders.

Please review:

1. Whether the Pine syntax is acceptable in TradingView v5.
2. Whether the static signal labels should remain inside the strategy files or be split into an indicator-only helper.
3. Whether the `rnd5337 HYPE FBC` preset correctly expresses the replay params:
   - base `12%`
   - steps `[0.953, 1.300, 1.442, 1.767]`
   - add multipliers `[0.9869251578, 1.3555755936, 1.9027652540, 3.0880673279]`
   - max total buys `5`
   - close-confirm DCA behavior
4. Any issues that would make this unsuitable for visual review on a HYPE 1m TradingView chart.

Return a concise review with pass/fail, exact line-level concerns if any, and recommended next edit.
