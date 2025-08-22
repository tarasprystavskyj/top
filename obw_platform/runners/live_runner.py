from .common import *


def run_live(cfg: dict, args):
    assert ccxt is not None, "ccxt required for LIVE mode"
    strat_path = cfg.get("strategy_class", "strategies.cross_sectional_rs.CrossSectionalRS")
    strat = load_strategy(strat_path, cfg)

    # Auth
    api_k = os.environ.get("BINGX_KEY", "")
    api_s = os.environ.get("BINGX_SECRET", "")
    print(f'[LIVE API] key="{mask(api_k)}", secret="{mask(api_s)}"')
    fetcher = CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=args.debug)

    top_n = int(cfg.get("top_n", 4))
    notional = float(cfg.get("notional", 2.2))
    position_mode = cfg.get("position_mode", "hedge")

    os.makedirs(args.results_dir, exist_ok=True)
    state_path = os.path.join(args.results_dir, "live_state.json")
    state = {}
    if os.path.exists(state_path):
        try:
            state = json.load(open(state_path, "r", encoding="utf-8"))
        except Exception:
            state = {}
    state.setdefault("positions", {})

    last_bar_ts = None
    print(f"[live] polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s")
    while True:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        bar_close = _align_bar_close(now, _tf_to_seconds(cfg.get('timeframe', '1h')))
        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close
            # md
            universe = sorted(set(fetcher.by_base.values()))
            md = {}
            for ccxt_sym in universe:
                df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=cfg.get("timeframe", "1h"), limit=max(60, args.limit_klines))
                if df is None or len(df) < 30:
                    continue
                feats = compute_feats(df).iloc[-1].to_dict()
                md[ccxt_sym] = feats

            # pipeline
            uni = strat.universe(bar_close, md)
            ranked = strat.rank(bar_close, md, uni)[:top_n]
            opened = 0
            for sym in ranked:
                row = md.get(sym)
                if row is None:
                    continue
                sig = strat.entry_signal(bar_close, sym, row, ctx={})
                if sig is None or sig.side != "LONG":
                    continue
                entry_px = fetcher.fetch_ticker_price(sym) or float(row.get("close") or 0.0)
                if not entry_px:
                    continue
                res = place_open_long(fetcher, sym, notional, entry_px, position_mode)
                if not res.get("ok"):
                    print(f"[open FAIL] {sym}: {res}", file=sys.stderr)
                    continue
                qty = float(res["qty"])
                # Brackets as reduce-only closes (simplified here)
                place_reduce_only(fetcher, sym, "sell", qty, position_mode)
                state["positions"][sym] = {"entry": entry_px, "qty": qty, "ts": bar_close.isoformat()}
                json.dump(state, open(state_path, "w", encoding="utf-8"), indent=2)
                opened += 1
            print(f"[live] opened={opened} at {bar_close.isoformat()}")
        time.sleep(args.poll_sec)

# ---------- BACKTEST delegator ----------