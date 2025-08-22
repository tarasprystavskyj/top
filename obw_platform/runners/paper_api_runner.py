from .common import *


def run_paper_api(cfg: dict, args):
    assert EnginePortfolio is not None, "engine.portfolio.Portfolio unavailable"

    strat_path = cfg.get("strategy_class", "strategies.cross_sectional_rs.CrossSectionalRS")
    strat = load_strategy(strat_path, cfg)

    port_cfg = {
        "initial_equity": float(cfg.get("initial_equity", cfg.get("start_cash", 200.0))),
        "fee_rate": float(cfg.get("fee_rate", 0.0006)),
        "slippage_per_side": float(cfg.get("slippage_per_side", cfg.get("slip_bps", 1.5) / 10000.0
                                           if isinstance(cfg.get("slip_bps", 0), (int, float)) else 0.0003)),
        "tick_pct": float(cfg.get("tick_pct", 0.0001)),
        "position_notional": float(cfg.get("notional", 2.2)),
        "max_notional_frac": float(cfg.get("max_notional_frac", 0.5)),
        "funding_rate_hour": float(cfg.get("funding_rate_hour", 0.0)),
    }
    pf = EnginePortfolio(port_cfg)

    use_mock = bool(getattr(args, "dry_run", False)) or (ccxt is None)
    fetcher = MockFetcher() if use_mock else CCXTFetcher(exchange=args.exchange, symbol_format=args.symbol_format, debug=args.debug)

    os.makedirs(args.results_dir, exist_ok=True)
    orders_db = args.orders_db or os.path.join(args.results_dir, "orders.sqlite")
    ensure_orders_db(orders_db)
    session_db_path, cache_out_path = ensure_session_dbs(args.results_dir, args.session_db, args.cache_out)

    run_id = datetime.utcnow().strftime("PA_%Y%m%d_%H%M%S")
    write_config_snapshot(session_db_path, run_id, cfg)

    top_n = int(cfg.get("top_n", 4))
    print(f"[paper-api] polling every {args.poll_sec}s; entries at bar close +{args.bar_delay_sec}s; orders -> {orders_db}")
    last_bar_ts = None
    iters_left = getattr(args, "iterations", None) if getattr(args, "dry_run", False) else None

    while True:
        now = datetime.utcnow().replace(tzinfo=timezone.utc)
        bar_close = _align_bar_close(now, _tf_to_seconds(cfg.get('timeframe', '1h'))) if cfg.get("timeframe", "1h") == "1h" else now.replace(second=0, microsecond=0)
        if (last_bar_ts is None or bar_close > last_bar_ts) and (now - bar_close).total_seconds() >= args.bar_delay_sec:
            last_bar_ts = bar_close

            # Build real-time md
            universe = sorted(set(fetcher.by_base.values()))
            md = {}
            for ccxt_sym in universe:
                df = fetcher.fetch_ohlcv_df(ccxt_sym, timeframe=cfg.get("timeframe", "1h"), limit=max(60, args.limit_klines))
                if df is None or len(df) < 30:
                    continue
                feats_df = compute_feats(df)
                cache_out_upsert(cache_out_path, ccxt_sym, feats_df)
                md[ccxt_sym] = feats_df.iloc[-1].to_dict()

            # exits
            for pos in list(pf.positions):
                row = md.get(pos.symbol)
                if row is None:
                    continue
                adj = strat.manage_position(bar_close, pos.symbol, pos, row, ctx={"portfolio": pf})
                if adj.action == "EXIT":
                    px = float(row.get("close") or 0.0) * (1 - port_cfg["slippage_per_side"])  # assume sell
                    pf.close(pos, bar_close, px, reason=adj.reason)
                    insert_order_row(orders_db, {
                        "order_id": str(uuid.uuid4()),
                        "ts_utc": datetime.utcnow().isoformat(),
                        "bar_time_utc": bar_close.isoformat(),
                        "mode": "paper_api",
                        "symbol": pos.symbol,
                        "side": "sell",
                        "type": "market",
                        "price": float(px),
                        "qty": float(pos.qty),
                        "status": "filled",
                        "reason": adj.reason or "exit",
                        "run_id": run_id,
                        "extra": json.dumps({"sim": True})
                    })

            # entries + decisions logging
            uni = strat.universe(bar_close, md)
            ranked = strat.rank(bar_close, md, uni)[:top_n]
            selected_syms = list(ranked)
            write_decisions(session_db_path, run_id, bar_close, ranked, selected_syms)

            for sym in ranked:
                row = md.get(sym)
                if row is None:
                    continue
                sig = strat.entry_signal(bar_close, sym, row, ctx={"portfolio": pf})
                if sig is None:
                    continue
                if not pf.can_open(port_cfg):
                    continue
                entry_px = float(row.get("close") or 0.0) * (1 + port_cfg["slippage_per_side"])  # assume buy
                pos = pf.open(symbol=sym, signal=sig, t=bar_close, last_price=entry_px)
                insert_order_row(orders_db, {
                    "order_id": str(uuid.uuid4()),
                    "ts_utc": datetime.utcnow().isoformat(),
                    "bar_time_utc": bar_close.isoformat(),
                    "mode": "paper_api",
                    "symbol": sym,
                    "side": "buy",
                    "type": "market",
                    "price": float(entry_px),
                    "qty": float(getattr(pos, "qty", 0.0)),
                    "status": "filled",
                    "reason": "entry",
                    "run_id": run_id,
                    "extra": json.dumps({"sim": True})
                })

            # equity snapshot + save trades & summary each bar
            eq = {
                "equity": getattr(pf, "equity", 0.0),
                "cash": getattr(pf, "cash", 0.0),
                "position_value": getattr(pf, "position_value", 0.0),
                "realized_pnl_cum": getattr(pf, "realized_pnl_cum", 0.0),
                "unrealized_pnl": getattr(pf, "unrealized_pnl", 0.0)
            }
            write_equity(session_db_path, run_id, bar_close, eq)

            trades_csv = os.path.join(args.results_dir, "trades.csv")
            summary_csv = os.path.join(args.results_dir, "summary.csv")
            pf.save_trades(trades_csv)
            pf.save_summary(summary_csv)

            print(f"[paper-api] bar {bar_close.isoformat()} processed: positions={len(pf.positions)}")

            if iters_left is not None:
                iters_left -= 1
                if iters_left <= 0:
                    break

        time.sleep(args.poll_sec)

# ---------- LIVE runner (BingX via CCXT; simplified bracket flow) ----------