#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd

BASE_COLS = {"symbol", "datetime_utc", "open", "high", "low", "close", "volume"}
EXCLUDE_EXTRAS = {"rsi", "stochastic", "mfi", "overbought_index"}


def table_columns(con: sqlite3.Connection, table: str = "price_indicators") -> list[str]:
    return [r[1] for r in con.execute(f"PRAGMA table_info({table})").fetchall()]


def main() -> int:
    ap = argparse.ArgumentParser(description="Stream SQLite price_indicators to fast OHLCV NPZ without loading the full DB into RAM.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--meta-out", default="")
    ap.add_argument("--chunksize", type=int, default=100_000)
    ap.add_argument("--include-extras", action="store_true")
    args = ap.parse_args()

    db_path = Path(args.db)
    out_path = Path(args.out)
    meta_path = Path(args.meta_out) if args.meta_out else out_path.with_suffix(".meta.json")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(str(db_path))
    cols = table_columns(con)
    if not cols:
        raise SystemExit("price_indicators table not found")
    required = ["symbol", "datetime_utc", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in cols]
    if missing:
        raise SystemExit(f"missing required columns: {missing}")

    extras = []
    if args.include_extras:
        extras = [c for c in cols if c not in BASE_COLS and c not in EXCLUDE_EXTRAS]

    counts = con.execute(
        "SELECT symbol, COUNT(*) FROM price_indicators GROUP BY symbol ORDER BY symbol ASC"
    ).fetchall()
    if not counts:
        raise SystemExit("No rows found in price_indicators")
    symbols = [str(s) for s, _ in counts]
    row_counts = [int(n) for _, n in counts]
    offsets = np.zeros(len(symbols) + 1, dtype=np.int64)
    offsets[1:] = np.cumsum(np.asarray(row_counts, dtype=np.int64))
    total = int(offsets[-1])

    meta = {
        "schema": "sqlite_price_indicators_to_npz_stream_v1",
        "db": str(db_path),
        "rows": total,
        "symbols": {},
        "extras": extras,
        "chunksize": int(args.chunksize),
    }

    with tempfile.TemporaryDirectory(prefix="sqlite_npz_stream_") as td:
        tmp = Path(td)
        arrays = {
            "timestamp_s": np.memmap(tmp / "timestamp_s.i64", mode="w+", dtype=np.int64, shape=(total,)),
            "open": np.memmap(tmp / "open.f64", mode="w+", dtype=np.float64, shape=(total,)),
            "high": np.memmap(tmp / "high.f64", mode="w+", dtype=np.float64, shape=(total,)),
            "low": np.memmap(tmp / "low.f64", mode="w+", dtype=np.float64, shape=(total,)),
            "close": np.memmap(tmp / "close.f64", mode="w+", dtype=np.float64, shape=(total,)),
            "volume": np.memmap(tmp / "volume.f64", mode="w+", dtype=np.float64, shape=(total,)),
        }
        for col in extras:
            arrays[col] = np.memmap(tmp / f"{col}.f64", mode="w+", dtype=np.float64, shape=(total,))

        select_cols = ["datetime_utc", "open", "high", "low", "close", "volume", *extras]
        for idx, sym in enumerate(symbols):
            start = int(offsets[idx])
            pos = start
            first_ts = None
            last_ts = None
            sql = (
                f"SELECT {','.join(select_cols)} FROM price_indicators "
                "WHERE symbol=? ORDER BY datetime_utc ASC"
            )
            for chunk in pd.read_sql_query(sql, con, params=(sym,), chunksize=args.chunksize):
                n = len(chunk)
                if n == 0:
                    continue
                end = pos + n
                ts = pd.to_datetime(chunk["datetime_utc"], utc=True)
                arrays["timestamp_s"][pos:end] = (ts.astype("int64") // 10**9).to_numpy(dtype=np.int64)
                for col in ["open", "high", "low", "close", "volume", *extras]:
                    arrays[col][pos:end] = chunk[col].to_numpy(dtype=np.float64, copy=False)
                first_ts = str(chunk["datetime_utc"].iloc[0]) if first_ts is None else first_ts
                last_ts = str(chunk["datetime_utc"].iloc[-1])
                pos = end
            expected_end = int(offsets[idx + 1])
            if pos != expected_end:
                raise SystemExit(f"row count mismatch for {sym}: wrote {pos - start}, expected {expected_end - start}")
            meta["symbols"][sym] = {"rows": int(pos - start), "time_from": first_ts, "time_to": last_ts}
            print(f"[symbol] {sym} rows={pos - start} range={first_ts}..{last_ts}", flush=True)

        for arr in arrays.values():
            arr.flush()
        payload = {
            "symbols": np.asarray(symbols, dtype=f"<U{max(len(s) for s in symbols)}"),
            "offsets": offsets,
            **arrays,
        }
        np.savez_compressed(out_path, **payload)

    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    con.close()
    print(f"[ok] wrote {out_path} rows={total} symbols={len(symbols)}")
    print(f"[ok] wrote {meta_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
