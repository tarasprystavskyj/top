#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import subprocess
import tempfile
from pathlib import Path

import ccxt
import numpy as np
import pandas as pd
import yaml

ROOT = Path(__file__).resolve().parents[3]
DEFAULT_CFG = ROOT / "obw_platform" / "meta_strategies" / "akela_meta_short" / "live_freedommoney" / "V21_freedommoney_bingx_live_min2p2.yaml"
DEFAULT_OUT = ROOT / "_reports" / "akela_meta_short" / "v21_majors_rank"
DEFAULT_PASSIVE_DB = ROOT / "_reports" / "akela_meta_short" / "s0_passive_orderbook_majors" / "s0_passive_orderbook.sqlite"
DEFAULT_SYMBOLS = "SOL/USDT:USDT,XRP/USDT:USDT,BNB/USDT:USDT,SUI/USDT:USDT,DOGE/USDT:USDT,ADA/USDT:USDT,LINK/USDT:USDT,AVAX/USDT:USDT"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def git_hash() -> str:
    try:
        return subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=str(ROOT), text=True).strip()
    except Exception:
        return ""


def parse_symbols(text: str):
    return [s.strip() for s in text.split(",") if s.strip()]


def tf_ms(timeframe: str) -> int:
    if timeframe.endswith("m"):
        return int(float(timeframe[:-1]) * 60_000)
    if timeframe.endswith("h"):
        return int(float(timeframe[:-1]) * 3_600_000)
    raise ValueError(f"unsupported timeframe: {timeframe}")


def fetch_ohlcv(ex, symbol: str, timeframe: str, bars: int) -> pd.DataFrame:
    limit = min(1000, max(100, int(bars)))
    step = tf_ms(timeframe)
    since = int((dt.datetime.now(dt.timezone.utc).timestamp() * 1000) - bars * step * 1.2)
    rows = []
    seen = set()
    while len(rows) < bars:
        chunk = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since, limit=limit)
        if not chunk:
            break
        advanced = False
        for r in chunk:
            ts = int(r[0])
            if ts in seen:
                continue
            seen.add(ts)
            rows.append(r)
            advanced = True
        since = int(chunk[-1][0]) + step
        if not advanced or len(chunk) < 2:
            break
        if since > int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000):
            break
    rows = rows[-bars:]
    if not rows:
        raise RuntimeError(f"no ohlcv for {symbol}")
    df = pd.DataFrame(rows, columns=["timestamp_ms", "open", "high", "low", "close", "volume"])
    df = df.drop_duplicates("timestamp_ms").sort_values("timestamp_ms").reset_index(drop=True)
    df["timestamp_s"] = (df["timestamp_ms"].astype("int64") // 1000).astype("int64")
    return df


def write_npz(path: Path, data: dict[str, pd.DataFrame]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    symbols = []
    offsets = [0]
    arrays = {k: [] for k in ["timestamp_s", "open", "high", "low", "close", "volume"]}
    for sym, df in data.items():
        symbols.append(sym)
        for key in arrays:
            arrays[key].append(df[key].to_numpy(dtype=np.float64 if key != "timestamp_s" else np.int64))
        offsets.append(offsets[-1] + len(df))
    payload = {
        "symbols": np.asarray(symbols, dtype=str),
        "offsets": np.asarray(offsets, dtype=np.int64),
    }
    for key, chunks in arrays.items():
        dtype = np.int64 if key == "timestamp_s" else np.float64
        payload[key] = np.concatenate(chunks).astype(dtype) if chunks else np.asarray([], dtype=dtype)
    np.savez_compressed(path, **payload)


def percentile(values, p: float):
    xs = [float(x) for x in values if x is not None and math.isfinite(float(x))]
    if not xs:
        return None
    return float(np.percentile(np.asarray(xs), p))


def passive_stats(db_path: Path) -> dict:
    if not db_path.exists():
        return {}
    import sqlite3

    con = sqlite3.connect(str(db_path))
    con.row_factory = sqlite3.Row
    out = {}
    for sym_row in con.execute("SELECT DISTINCT symbol FROM passive_orderbook_snapshots"):
        sym = sym_row[0]
        rows = [dict(r) for r in con.execute("SELECT spread_bp, top10_bid_notional, top10_ask_notional, expected_roundtrip_cost_floor_bp FROM passive_orderbook_snapshots WHERE symbol=?", (sym,))]
        spreads = [r.get("spread_bp") for r in rows]
        rt = [r.get("expected_roundtrip_cost_floor_bp") for r in rows]
        out[sym] = {
            "passive_n": len(rows),
            "spread_p50_bp": percentile(spreads, 50),
            "spread_p95_bp": percentile(spreads, 95),
            "roundtrip_floor_p50_bp": percentile(rt, 50),
            "top10_bid_p50_usdt": percentile([r.get("top10_bid_notional") for r in rows], 50),
            "top10_ask_p50_usdt": percentile([r.get("top10_ask_notional") for r in rows], 50),
        }
    con.close()
    return out


def patch_cfg(cfg: dict, symbol: str, static_slip_bp: float) -> dict:
    out = json.loads(json.dumps(cfg))
    out["symbol"] = symbol
    out["cache_db"] = ""
    out.setdefault("backtest", {}).setdefault("slippage", {})
    out["backtest"]["slippage"].update({"enabled": True, "mode": "static", "static_bp": float(static_slip_bp)})
    return out


def run_backtest(cfg: dict, cfg_path: Path, npz_path: Path, symbol: str, out_dir: Path, scenario: str, limit_bars: int) -> dict:
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False), encoding="utf-8")
    curves = out_dir / f"{symbol.replace('/', '_').replace(':', '_')}_{scenario}_curves.csv"
    cmd = [
        "python3",
        "backtester_dual_long_short_fast_pack_v2.py",
        "--cfg",
        str(cfg_path),
        "--npz",
        str(npz_path),
        "--symbol",
        symbol,
        "--limit-bars",
        str(limit_bars),
        "--export-curves",
        str(curves),
    ]
    proc = subprocess.run(cmd, cwd=str(ROOT / "obw_platform"), text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=180)
    result = {"scenario": scenario, "symbol": symbol, "cmd": " ".join(cmd), "returncode": proc.returncode, "raw_output": proc.stdout[-5000:]}
    if proc.returncode == 0:
        try:
            parsed = json.loads(proc.stdout[proc.stdout.index("{"):])
            result.update(parsed)
        except Exception as exc:
            result["parse_error"] = str(exc)
    return result


def score_row(row: dict) -> float:
    ret = float(row.get("return_mtm_pct_on_start") or -999.0)
    mdd = abs(float(row.get("mdd_mtm_%") or 0.0))
    mc = float(row.get("margin_call_events_total") or 0.0)
    trades = float(row.get("trades_total") or 0.0)
    spread = float(row.get("spread_p50_bp") or 999.0)
    rt = float(row.get("roundtrip_floor_p50_bp") or 999.0)
    vol = float(row.get("bar_volatility_p50_bp") or 0.0)
    liquidity = math.log1p(float(row.get("top10_bid_p50_usdt") or 0.0) + float(row.get("top10_ask_p50_usdt") or 0.0))
    return ret - 0.75 * mdd - 25.0 * mc + 0.02 * min(trades, 2000.0) - 0.15 * spread - 0.08 * rt + 0.02 * vol + 0.8 * liquidity


def realized_vol_bp(df: pd.DataFrame) -> dict:
    ret = df["close"].pct_change().dropna().abs() * 10000.0
    return {
        "bar_volatility_p50_bp": percentile(ret, 50),
        "bar_volatility_p95_bp": percentile(ret, 95),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--symbols", default=DEFAULT_SYMBOLS)
    ap.add_argument("--exchange", default="bingx")
    ap.add_argument("--timeframe", default="5m")
    ap.add_argument("--bars", type=int, default=5000)
    ap.add_argument("--limit-bars", type=int, default=5000)
    ap.add_argument("--cfg", default=str(DEFAULT_CFG))
    ap.add_argument("--out-dir", default=str(DEFAULT_OUT))
    ap.add_argument("--passive-db", default=str(DEFAULT_PASSIVE_DB))
    args = ap.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    symbols = parse_symbols(args.symbols)
    ex = getattr(ccxt, args.exchange)({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    data = {}
    for sym in symbols:
        data[sym] = fetch_ohlcv(ex, sym, args.timeframe, args.bars)
    npz_path = out_dir / f"majors_{args.exchange}_{args.timeframe}_{args.bars}b.npz"
    write_npz(npz_path, data)

    cfg = yaml.safe_load(Path(args.cfg).read_text(encoding="utf-8")) or {}
    pstats = passive_stats(Path(args.passive_db))
    rows = []
    raw = []
    with tempfile.TemporaryDirectory(prefix="v21_majors_rank_") as tmp:
        tmp_path = Path(tmp)
        for sym, df in data.items():
            stats = pstats.get(sym, {})
            vol = realized_vol_bp(df)
            scenarios = {
                "cfg_static": float(((cfg.get("backtest") or {}).get("slippage") or {}).get("static_bp") or 9.38),
                "passive_spread_p50": float(stats.get("spread_p50_bp") or 10.0),
                "passive_spread_p95": float(stats.get("spread_p95_bp") or stats.get("spread_p50_bp") or 10.0),
            }
            for name, slip in scenarios.items():
                patched = patch_cfg(cfg, sym, slip)
                result = run_backtest(patched, tmp_path / f"{sym.replace('/', '_').replace(':', '_')}_{name}.yaml", npz_path, sym, out_dir, name, args.limit_bars)
                raw.append(result)
                row = {
                    "symbol": sym,
                    "scenario": name,
                    "static_slippage_bp": slip,
                    **stats,
                    **vol,
                    **{k: result.get(k) for k in [
                        "return_mtm_pct_on_start",
                        "total_pnl_mtm",
                        "realized_pnl_total",
                        "realized_pnl_long",
                        "realized_pnl_short",
                        "unrealized_pnl_total",
                        "terminal_unrealized_to_realized_ratio",
                        "mdd_mtm_%",
                        "trades_total",
                        "trades_long",
                        "trades_short",
                        "margin_call_events_total",
                        "bars_in_margin_call",
                    ]},
                    "returncode": result.get("returncode"),
                }
                row["score"] = score_row(row)
                rows.append(row)
    df_out = pd.DataFrame(rows).sort_values(["scenario", "score"], ascending=[True, False])
    csv_path = out_dir / "v21_majors_rank.csv"
    json_path = out_dir / "v21_majors_rank.json"
    raw_path = out_dir / "v21_majors_rank_raw.json"
    df_out.to_csv(csv_path, index=False)
    payload = {
        "schema": "v21_majors_liquidity_volatility_rank_v1",
        "ts_utc": utc_now(),
        "git_hash": git_hash(),
        "npz": str(npz_path),
        "cfg": str(Path(args.cfg)),
        "symbols": symbols,
        "rows": df_out.to_dict(orient="records"),
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    raw_path.write_text(json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
    md = ["# V21 majors liquidity/volatility rank", "", f"Updated: {payload['ts_utc']}", "", "## Top by scenario"]
    for scenario, grp in df_out.groupby("scenario", sort=False):
        md.append(f"### {scenario}")
        for _, r in grp.head(8).iterrows():
            md.append(
                f"- {r['symbol']}: score={r['score']:.2f} ret={r.get('return_mtm_pct_on_start')} "
                f"mdd={r.get('mdd_mtm_%')} mc={r.get('margin_call_events_total')} "
                f"spread50={r.get('spread_p50_bp')} rt50={r.get('roundtrip_floor_p50_bp')} trades={r.get('trades_total')}"
            )
        md.append("")
    (out_dir / "v21_majors_rank.md").write_text("\n".join(md), encoding="utf-8")
    print(json.dumps({"csv": str(csv_path), "json": str(json_path), "top": df_out.head(10).to_dict(orient="records")}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
