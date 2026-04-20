
import sqlite3, json, tempfile, os, importlib.util, sys
from pathlib import Path

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

probe = load_module("probev1_test", "/mnt/data/orderbook_slippage_probe_v1.py")
vxm = load_module("vxv6_test", "/mnt/data/virtual_exchange_v6.py")

def test_virtual_exchange_orderbook_exists():
    vx = vxm.VirtualExchange(
        db_path="/mnt/data/combined_cache_session(3).db",
        symbols=["ENA/USDT:USDT"],
        default_timeframe="1m",
        mode="hedge",
        dynamic_slippage_model={},
        broker_id="bingx",
        error_config={"simulate_bingx_reduceonly_reject": False},
        debug=False,
    )
    book = vx.fetch_order_book("ENA/USDT:USDT", 5)
    assert book["bids"] and book["asks"]
    assert book["asks"][0][0] > book["bids"][0][0]

def test_probe_records_snapshots_and_observations():
    vx = vxm.VirtualExchange(
        db_path="/mnt/data/combined_cache_session(3).db",
        symbols=["ENA/USDT:USDT"],
        default_timeframe="1m",
        mode="hedge",
        dynamic_slippage_model={},
        broker_id="bingx",
        error_config={"simulate_bingx_reduceonly_reject": False},
        debug=False,
    )
    fd, dbp = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        probe.ensure_microstructure_tables(dbp)
        book = vx.fetch_order_book("ENA/USDT:USDT", 10)
        snap = probe.record_pretrade_snapshot(
            dbp,
            run_id="T1",
            bar_time_utc="2026-04-20T04:30:00+00:00",
            symbol="ENA/USDT:USDT",
            strategy_side="LONG",
            order_action="OPEN",
            qty=20.0,
            requested_price=0.1158,
            book=book,
        )
        od = vx.create_order("ENA/USDT:USDT", "market", "buy", 20.0, None, {"positionSide": "LONG"})
        fill = float(od.get("average") or od.get("price"))
        obs = probe.record_fill_observation(
            dbp,
            run_id="T1",
            bar_time_utc="2026-04-20T04:30:00+00:00",
            symbol="ENA/USDT:USDT",
            strategy_side="LONG",
            order_action="OPEN",
            qty=20.0,
            requested_price=0.1158,
            fill_price=fill,
            pre_snapshot=snap,
        )
        con = sqlite3.connect(dbp)
        n1 = con.execute("select count(*) from market_microstructure").fetchone()[0]
        n2 = con.execute("select count(*) from slippage_observations").fetchone()[0]
        con.close()
        assert n1 == 1 and n2 == 1
        assert obs["actual_adverse_bp"] >= 0.0
    finally:
        try:
            os.remove(dbp)
        except OSError:
            pass

def test_last_scenario_smoke_replay_orderbook_side():
    vx = vxm.VirtualExchange(
        db_path="/mnt/data/combined_cache_session(3).db",
        symbols=["ENA/USDT:USDT"],
        default_timeframe="1m",
        mode="hedge",
        dynamic_slippage_model={},
        broker_id="bingx",
        error_config={"simulate_bingx_reduceonly_reject": False},
        debug=False,
    )
    vx.set_cursor("ENA/USDT:USDT", 10)
    buy = vx.create_order("ENA/USDT:USDT", "market", "buy", 20.0, None, {"positionSide": "LONG"})
    sell = vx.create_order("ENA/USDT:USDT", "market", "sell", 20.0, None, {"positionSide": "SHORT"})
    assert "spreadBps" in buy["info"] and "spreadBps" in sell["info"]
