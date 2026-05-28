#!/usr/bin/env python3
"""Generate a full V21 config wrapped by the external-signal gate."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from obw_platform.meta_strategies.v21_external_signal_wrapper import build_v21_external_signal_cfg


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-cfg", default="obw_platform/configs/V21_strict_trend_stable_live_static9p38.yaml")
    ap.add_argument("--out", default="obw_platform/meta_strategies/telegram_signal_dca/generated_v21_external_signal.yaml")
    ap.add_argument("--signals-file", default="obw_platform/meta_strategies/telegram_signal_dca/reports/live_signals.json")
    ap.add_argument("--delegated-capital-usdt", type=float, default=100.0)
    ap.add_argument("--base-order-pct-eq", type=float, default=5.0)
    args = ap.parse_args()

    base = yaml.safe_load(Path(args.base_cfg).read_text(encoding="utf-8")) or {}
    cfg = build_v21_external_signal_cfg(
        base,
        delegated_capital_usdt=args.delegated_capital_usdt,
        base_order_pct_eq=args.base_order_pct_eq,
        signals_file=args.signals_file,
    )
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=False), encoding="utf-8")
    print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
