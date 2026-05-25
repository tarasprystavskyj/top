# Data Request For More Telegram History

Date: 2026-05-16
Mode: paper-only research
Live readiness: false

## Why More Data Is Needed

The current local static Telegram universe is capped at 312 extracted signals. The strongest candidate,
`rolling240d_symbol_side_min3_positive`, is positive on both split60 and split70 but opens only 30 and 23 trades.

That is below the minimum validation gate of 50 opened trades per validation split.

## Requested Data

Preferred input:

```text
More historical darkknighttrade Telegram messages or extracted signal rows from the same source/channel.
```

Acceptable formats:

```text
CSV
JSONL
raw Telegram export JSON/HTML/TXT, if a parser can be added later
```

Required fields for already-extracted rows:

```text
dt_utc
symbol
side
entry or entry range
stop loss / sl
take profit / tp levels
source/channel identifier
raw message id or raw message text when available
```

Important constraints:

1. Keep each source/channel separated.
2. Preserve original timestamps and timezone information.
3. Do not mix generated filters or report derivatives with raw/extracted source data.
4. Mark duplicates explicitly if the same signal appears in multiple exports.

## Validation To Run After Data Is Added

Primary candidate:

```text
rolling240d_symbol_side_min3_positive
```

Mandatory controls:

```text
all-after by split
all_49 / no-filter static replay
base no-DCA TP1
```

Required checks:

```text
opened_signals >= 50 on validation split
positive mtm_pnl_pct on split60 and split70 equivalents
drawdown not worse than all-after controls
no full-sample symbol blacklist
no live or paper-live changes
```

## Current Decision

No promotion.

Further broad DCA tuning on the same 312-signal dataset is not useful. The next meaningful research step is to increase historical sample size and rerun the candidate plus controls.
