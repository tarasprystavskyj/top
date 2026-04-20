import sys, importlib.util, sqlite3, yaml
import pandas as pd
import numpy as np

sys.path.insert(0, '/mnt/data')

def load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    assert spec.loader is not None
    spec.loader.exec_module(mod)
    return mod

m = load_module('slip_v2_test', '/mnt/data/slippage_directional_model_v2.py')
core = load_module('core_v6_test', '/mnt/data/backtester_dual_core_dynamic_v6.py')
strat = load_module('strat_v2_test', '/mnt/data/cryptomine_pack_dual_full_compensated_v2.py')

def test_build_training_frame_has_microstructure_columns():
    df = m.build_training_frame('/mnt/data/session(10).sqlite', '/mnt/data/combined_cache_session(3).db', symbol='ENA/USDT:USDT')
    assert len(df) > 0
    assert 'spread_bp' in df.columns
    assert 'est_sweep_slip_bp' in df.columns


def test_fit_and_update_model():
    df = m.build_training_frame('/mnt/data/session(10).sqlite', '/mnt/data/combined_cache_session(3).db', symbol='ENA/USDT:USDT')
    model = m.fit_directional_slippage_model(df)
    obs = {
        'strategy_side': 'LONG', 'order_action': 'OPEN', 'qty': 20.0,
        'open': 0.12, 'high': 0.121, 'low': 0.119, 'close': 0.1205, 'volume': 10000.0, 'quote_volume': 1205.0,
        'spread_bp': 2.0, 'est_sweep_slip_bp': 6.0, 'bid_depth_qty': 2000.0, 'ask_depth_qty': 1800.0, 'book_imbalance': 0.05,
        'actual_adverse_bp': 7.0
    }
    feat = m.make_feature_row(obs, 'LONG', 'OPEN', 20.0)
    obs.update(feat)
    model2 = m.update_directional_slippage_model(model, obs)
    assert model2['models']['BUY']['n'] >= model['models']['BUY']['n']


def test_core_v6_runs_small():
    cfg = yaml.safe_load(open('/mnt/data/final_best_ena_1y_pack_04-14.yaml', 'r', encoding='utf-8'))
    cfg['strategy_class_long'] = 'strat_v2_test.CryptomineLongPackAdaptiveEven'
    cfg['strategy_class_short'] = 'strat_v2_test.CryptomineShortPackAdaptiveEven'
    cfg['timeframe'] = '1m'
    con = sqlite3.connect('/mnt/data/combined_cache_session(3).db')
    bars = pd.read_sql_query("select datetime_utc, open, high, low, close, volume, quote_volume from price_indicators where symbol='ENA/USDT:USDT' order by datetime_utc limit 300", con)
    con.close()
    ts = (pd.to_datetime(bars['datetime_utc'], utc=True).astype('int64') // 10**9).to_numpy(np.int64)
    out = core.simulate(cfg, ts, bars['close'].to_numpy(float), open_=bars['open'].to_numpy(float), high=bars['high'].to_numpy(float), low=bars['low'].to_numpy(float), volume=bars['volume'].to_numpy(float), extras={'quote_volume': bars['quote_volume'].to_numpy(float)}, market_symbol='ENA/USDT:USDT', export_curves=True)
    assert 'curves' in out and len(out['curves']) > 0
