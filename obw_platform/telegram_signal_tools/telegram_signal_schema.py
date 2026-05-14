#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared Telegram signal parser, normalizer, and validator."""
from __future__ import annotations

import csv
import datetime as dt
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


NUM_RE = r"[-+]?\d+(?:[\.,]\d+)?"
SIG_RE = re.compile(r"(?:заходжу\s+в|enter(?:ing)?|signal)\s+#?\$?([a-z0-9/_:-]{2,40})\s+(long|short)\s+(\d{1,3})x", re.I)
ENTRY_RE = re.compile(r"(?:точка\s+входу|entry(?:\s+zone)?)\s*[:：]?\s*(" + NUM_RE + r")\s*[-–—]\s*(" + NUM_RE + r")", re.I)
TP_RE = re.compile(r"(?:тейк[-\s]?профіт|take[-\s]?profit|tp)\s*[:：]?\s*([^\n]+)", re.I)
SL_RE = re.compile(r"(?:стоп[-\s]?лосс|stop[-\s]?loss|sl)\s*[:：]?\s*(" + NUM_RE + r")", re.I)

REPLAY_FIELDS = [
    "message_idx",
    "dt_utc",
    "symbol",
    "side",
    "leverage",
    "entry_a",
    "entry_b",
    "sl",
    "tp1",
    "tp2",
    "tp3",
    "raw_text",
    "entry_low",
    "entry_high",
    "source_channel",
    "telegram_message_id",
    "telegram_message_date",
]


def normalize_telegram_channel(raw: Any, default: str = "darkknighttrade") -> str:
    s = str(raw or default).strip()
    if not s:
        return default
    if "://" in s:
        parsed = urlparse(s)
        if parsed.netloc.lower() in {"t.me", "telegram.me"}:
            parts = [p for p in parsed.path.split("/") if p]
            if parts:
                return parts[0].lstrip("@")
    if s.startswith("t.me/"):
        return s.split("/", 1)[1].split("/", 1)[0].lstrip("@")
    return s.lstrip("@")


def parse_float(v: Any) -> Optional[float]:
    try:
        if v is None or v == "":
            return None
        return float(str(v).replace(",", "."))
    except Exception:
        return None


def normalize_side(v: Any) -> Optional[str]:
    s = str(v or "").strip().lower()
    if s in {"long", "buy"}:
        return "long"
    if s in {"short", "sell"}:
        return "short"
    return None


def normalize_symbol(raw: Any, quote: str = "USDT") -> str:
    s = str(raw or "").strip().upper().lstrip("#$")
    if not s:
        return ""
    if "/" in s:
        base, rest = s.split("/", 1)
        q = rest.split(":", 1)[0] or quote
        return f"{base}/{q}:{q}"
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return f"{s[:-len(q)]}/{q}:{q}"
    return f"{s}/{quote}:{quote}"


def base_symbol(symbol: Any) -> str:
    s = str(symbol or "").strip().upper()
    if "/" in s:
        return s.split("/", 1)[0]
    for q in ("USDT", "USDC", "USD"):
        if s.endswith(q) and len(s) > len(q):
            return s[:-len(q)]
    return s


def parse_signal_text(text: str, ts_utc: Optional[str] = None) -> Optional[Dict[str, Any]]:
    low = text.lower()
    sm = SIG_RE.search(low)
    em = ENTRY_RE.search(low)
    tm = TP_RE.search(low)
    slm = SL_RE.search(low)
    if not (sm and em and tm and slm):
        return None
    tps = [parse_float(x) for x in re.findall(NUM_RE, tm.group(1))[:3]]
    if len(tps) < 3 or any(x is None for x in tps):
        return None
    a = parse_float(em.group(1))
    b = parse_float(em.group(2))
    sl = parse_float(slm.group(1))
    side = normalize_side(sm.group(2))
    if a is None or b is None or sl is None or side is None:
        return None
    symbol = normalize_symbol(sm.group(1))
    return {
        "ts_utc": ts_utc or dt.datetime.now(dt.timezone.utc).isoformat(),
        "symbol": symbol,
        "base_symbol": base_symbol(symbol),
        "side": side,
        "leverage_claimed": int(sm.group(3)),
        "entry_low": min(a, b),
        "entry_high": max(a, b),
        "tp": tps,
        "sl": sl,
        "raw_text": text,
        "mode": "paper_signal_only",
    }


def _tp_values(row: Dict[str, Any]) -> List[Optional[float]]:
    tps = row.get("tp")
    if isinstance(tps, str):
        try:
            tps = json.loads(tps)
        except Exception:
            tps = []
    if not isinstance(tps, list):
        tps = [row.get("tp1"), row.get("tp2"), row.get("tp3")]
    return [parse_float(x) for x in tps[:3]]


def normalize_signal_row(row: Dict[str, Any], idx: int = 0) -> Optional[Dict[str, Any]]:
    side = normalize_side(row.get("side"))
    entry_low = parse_float(row.get("entry_low"))
    entry_high = parse_float(row.get("entry_high"))
    if entry_low is None or entry_high is None:
        a = parse_float(row.get("entry_a"))
        b = parse_float(row.get("entry_b"))
        if a is not None and b is not None:
            entry_low, entry_high = min(a, b), max(a, b)
    sl = parse_float(row.get("sl") or row.get("stop"))
    tp = _tp_values(row)
    if side is None or entry_low is None or entry_high is None or sl is None or len(tp) < 3 or any(x is None for x in tp):
        return None
    symbol = normalize_symbol(row.get("symbol"))
    return {
        "message_idx": row.get("message_idx", idx),
        "dt_utc": row.get("dt_utc") or row.get("ts_utc"),
        "symbol": symbol,
        "side": side,
        "leverage": row.get("leverage") or row.get("leverage_claimed") or "",
        "entry_a": entry_low,
        "entry_b": entry_high,
        "sl": sl,
        "tp1": tp[0],
        "tp2": tp[1],
        "tp3": tp[2],
        "raw_text": row.get("raw_text", ""),
        "entry_low": min(entry_low, entry_high),
        "entry_high": max(entry_low, entry_high),
        "source_channel": row.get("source_channel", ""),
        "telegram_message_id": row.get("telegram_message_id", ""),
        "telegram_message_date": row.get("telegram_message_date", ""),
    }


def read_signal_rows(path: str) -> List[Dict[str, Any]]:
    p = Path(path)
    if p.suffix.lower() == ".jsonl":
        rows = []
        for line_no, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            row.setdefault("_line_no", line_no)
            rows.append(row)
        return rows
    with p.open("r", newline="", encoding="utf-8") as f:
        return [dict(r) for r in csv.DictReader(f)]


def normalize_rows(rows: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for i, row in enumerate(rows):
        norm = normalize_signal_row(row, i)
        if norm:
            out.append(norm)
    return out


def validate_normalized(row: Dict[str, Any]) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    side = normalize_side(row.get("side"))
    entry_low = parse_float(row.get("entry_low"))
    entry_high = parse_float(row.get("entry_high"))
    sl = parse_float(row.get("sl"))
    tp = [parse_float(row.get("tp1")), parse_float(row.get("tp2")), parse_float(row.get("tp3"))]
    if not row.get("dt_utc"):
        reasons.append("missing_dt")
    if not row.get("symbol"):
        reasons.append("missing_symbol")
    if side is None:
        reasons.append("bad_side")
    if None in (entry_low, entry_high, sl) or any(x is None for x in tp):
        reasons.append("missing_price")
    if reasons:
        return False, reasons
    assert side is not None and entry_low is not None and entry_high is not None and sl is not None
    tpf = [float(x) for x in tp if x is not None]
    if min(entry_low, entry_high, sl, *tpf) <= 0:
        reasons.append("non_positive_price")
    if entry_low > entry_high:
        reasons.append("entry_bounds_reversed")
    mid = (entry_low + entry_high) / 2.0
    if mid > 0 and (entry_high - entry_low) / mid > 0.25:
        reasons.append("wide_entry_zone")
    if side == "long":
        if sl >= entry_low:
            reasons.append("long_sl_not_below_entry")
        if not (tpf[0] <= tpf[1] <= tpf[2]):
            reasons.append("long_tp_not_ascending")
        if tpf[0] <= entry_high:
            reasons.append("long_tp1_not_above_entry")
    if side == "short":
        if sl <= entry_high:
            reasons.append("short_sl_not_above_entry")
        if not (tpf[0] >= tpf[1] >= tpf[2]):
            reasons.append("short_tp_not_descending")
        if tpf[0] >= entry_low:
            reasons.append("short_tp1_not_below_entry")
    return not reasons, reasons


def quality_report(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    total = 0
    valid = 0
    invalid = 0
    reason_counts: Dict[str, int] = {}
    side_counts: Dict[str, int] = {}
    symbols = set()
    seen = set()
    duplicates = 0
    invalid_examples = []
    for i, row in enumerate(rows):
        total += 1
        norm = normalize_signal_row(row, i)
        if not norm:
            invalid += 1
            reason_counts["not_normalizable"] = reason_counts.get("not_normalizable", 0) + 1
            if len(invalid_examples) < 10:
                invalid_examples.append({"row": i, "reasons": ["not_normalizable"], "symbol": row.get("symbol"), "side": row.get("side")})
            continue
        ok, reasons = validate_normalized(norm)
        key = (norm.get("dt_utc"), norm.get("symbol"), norm.get("side"), norm.get("entry_low"), norm.get("entry_high"))
        if key in seen:
            duplicates += 1
        seen.add(key)
        symbols.add(base_symbol(norm.get("symbol")))
        side = str(norm.get("side"))
        side_counts[side] = side_counts.get(side, 0) + 1
        if ok:
            valid += 1
        else:
            invalid += 1
            for reason in reasons:
                reason_counts[reason] = reason_counts.get(reason, 0) + 1
            if len(invalid_examples) < 10:
                invalid_examples.append({"row": i, "reasons": reasons, "symbol": norm.get("symbol"), "side": norm.get("side")})
    return {
        "total_rows": total,
        "valid_rows": valid,
        "invalid_rows": invalid,
        "valid_ratio": (valid / total) if total else 0.0,
        "unique_symbols": len(symbols),
        "side_counts": side_counts,
        "duplicates": duplicates,
        "reason_counts": dict(sorted(reason_counts.items())),
        "invalid_examples": invalid_examples,
    }
