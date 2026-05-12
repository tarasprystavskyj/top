#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Convert standard SQLite OHLCV DB to multi-symbol NPZ, with a hard truncation check.

Expected standard table:
  price_indicators(symbol, datetime_utc, open, high, low, close, volume, ...)

This script refuses truncated SQLite files. It is intentional: a half-uploaded DB can
silently produce fake/incomplete backtests if you try to ignore corruption.
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path
from typing import Dict, List

import numpy as np
import pandas as pd


def sqlite_expected_size(path: str):
    with open(path, "rb") as f:
        header = f.read(100)
    if not header.startswith(b"SQLite format 3"):
        return None
    page_size = int.from_bytes(header[16:18], "big") or 65536
    page_count = int.from_bytes(header[28:32], "big")
    return page_size, page_count, page_size * page_count


def check_db(path: str) -> None:
    size = os.path.getsize(path)
    exp = sqlite_expected_size(path)
    if exp:
        page_size, page_count, expected_size = exp
        if expected_size > size:
            raise SystemExit(
                f"DB looks truncated: actual={size} bytes, expected_from_header={expected_size} bytes, "
                f"missing={expected_size-size} bytes, page_size={page_size}, page_count={page_count}. "
                "Re-upload the DB as .zip/.zst/.gz or copy it directly on the server."
            )
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        con.execute("PRAGMA quick_check").fetchall()
        con.close()
    except Exception as e:
        raise SystemExit(f"SQLite quick_check failed: {type(e).__name__}: {e}")


def load_symbols_file(path: str) -> List[str]:
    if not path:
        return []
    out = []
    seen = set()
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if s not in seen:
            seen.add(s)
            out.append(s)
    return out


def existing_columns(con: sqlite3.Connection, table: str) -> List[str]:
    return [str(row[1]) for row in con.execute(f"PRAGMA table_info({table})")]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--table", default="price_indicators")
    ap.add_argument("--symbol-col", default="symbol")
    ap.add_argument("--datetime-col", default="datetime_utc")
    ap.add_argument("--symbols-file", default="")
    ap.add_argument("--skip-integrity-check", action="store_true")
    ap.add_argument("--include-extra-cols", action="store_true", help="Export all numeric columns except symbol/datetime")
    args = ap.parse_args()

    if not args.skip_integrity_check:
        check_db(args.db)

    con = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    cols = existing_columns(con, args.table)
    required = [args.symbol_col, args.datetime_col, "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in cols]
    if missing:
        raise SystemExit(f"Missing columns in {args.table}: {missing}; existing={cols}")

    select_cols = required[:]
    if args.include_extra_cols:
        for c in cols:
            if c not in select_cols and c not in {args.symbol_col, args.datetime_col}:
                select_cols.append(c)

    q = "SELECT " + ", ".join(select_cols) + f" FROM {args.table}"
    params: List[str] = []
    syms = load_symbols_file(args.symbols_file)
    if syms:
        q += " WHERE " + args.symbol_col + " IN (" + ",".join(["?"] * len(syms)) + ")"
        params.extend(syms)
    q += f" ORDER BY {args.symbol_col} ASC, {args.datetime_col} ASC"
    print(f"[read] {args.db}", flush=True)
    df = pd.read_sql_query(q, con, params=params)
    con.close()
    if df.empty:
        raise SystemExit("No rows selected")

    symbols: List[str] = []
    offsets = [0]
    parts: Dict[str, List[np.ndarray]] = {"timestamp_s": [], "open": [], "high": [], "low": [], "close": [], "volume": []}
    numeric_cols = [c for c in df.columns if c not in {args.symbol_col, args.datetime_col}]
    for sym, part in df.groupby(args.symbol_col, sort=True):
        part = part.drop_duplicates(subset=[args.datetime_col]).sort_values(args.datetime_col)
        if part.empty:
            continue
        symbols.append(str(sym))
        ts = pd.to_datetime(part[args.datetime_col], utc=True).astype("int64").to_numpy() // 1_000_000_000
        parts.setdefault("timestamp_s", []).append(ts.astype(np.int64))
        for c in numeric_cols:
            vals = pd.to_numeric(part[c], errors="coerce").astype("float64").to_numpy()
            parts.setdefault(c, []).append(vals)
        offsets.append(offsets[-1] + len(part))
        print(f"[sym] {sym} rows={len(part)}", flush=True)

    max_len = max([len(s) for s in symbols] + [1])
    out = {
        "symbols": np.asarray(symbols, dtype=f"<U{max_len}"),
        "offsets": np.asarray(offsets, dtype=np.int64),
    }
    for c, arrs in parts.items():
        out[c] = np.concatenate(arrs) if arrs else np.asarray([], dtype=np.float64)
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.out, **out)
    print(f"[done] wrote {args.out} symbols={len(symbols)} rows={offsets[-1]}", flush=True)


if __name__ == "__main__":
    main()
