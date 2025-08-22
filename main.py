
from AlgorithmImports import *
from datetime import timedelta, datetime
import math
from collections import defaultdict

class CrossSectionalC2Crypto(QCAlgorithm):
    """
    Cross-Sectional Relative Strength (C2 profile) for crypto on 1h bars.
    Ranks by short-term momentum (dp6h + dp12h) with ATR filter, volume surge and breadth checks.
    Entries: LONG-only by default (Binance cash model; SHORT requires a margin model not used here).
    Exits: time-based / momentum flip / ATR MAE / optional trailing.
    Also records equity curve and computes daily / monthly returns in OnEndOfAlgorithm.
    """

    # -------- Default parameters (mirrors cs_C2_base_1h.yaml keys where applicable) --------
    DEFAULTS = {
        "top_n":                  5,
        "side":                   "LONG",
        "min_momentum_sum":       0.08,
        "min_atr_ratio":          0.016,
        "min_vol_surge_mult":     1.20,
        "min_qv_24h":             200_000,
        "min_qv_1h":              10_000,
        "min_breadth":            0.00,
        "sl_atr_mult":            1.3,
        "tp_atr_mult":            2.2,
        "max_hold_hours":         96,
        "max_mae_atr_mult":       1.6,
        "mom_flip_thresh":        0.02,
        "trail_start_atr":        1.2,
        "trail_dist_atr":         1.0,
        "leverage":               2,
        "entry_every_hours":      1,
        "universe":               "",
        "warmup_hours":           240,
        "use_limit_tp":           1,
        "start_cash":             100000,
        "start_date":             "2024-01-01",
        "end_date":               "2025-01-01",
        "plot_equity":            1,
        "export_equity_csv":      1,
        "export_trades_csv":      1,
        "quote_currency":        "USDT",
    }

    RAW_UNIVERSE = [
        "BTCUSDT","ETHUSDT","SOLUSDT","BNBUSDT","XRPUSDT","DOGEUSDT","ADAUSDT","LINKUSDT","TRXUSDT",
        "AVAXUSDT","NEARUSDT","ATOMUSDT","FILUSDT","DOTUSDT","INJUSDT","SUIUSDT","TIAUSDT","OPUSDT",
        "ARBUSDT","APTUSDT","PEPEUSDT","WIFUSDT","BONKUSDT","FETUSUSDT","UNIUSDT","ETCUSDT","HBARUSDT",
        "ALGOUSDT","XLMUSDT","ICPUSDT","AAVEUSDT","LTCUSDT","GMXUSDT","MKRUSDT","SEIUSDT","JUPUSDT",
        "PYTHUSDT","ENAUSDT","BEAMXUSDT","ORDIUSDT","RUNEUSDT"
    ]

    def Initialize(self):
        sd = self._p("start_date"); ed = self._p("end_date")
        try:
            sdt = datetime.strptime(sd, "%Y-%m-%d"); edt = datetime.strptime(ed, "%Y-%m-%d")
        except Exception:
            sdt = datetime(2024, 1, 1); edt = datetime(2025, 1, 1)
        self.SetStartDate(sdt.year, sdt.month, sdt.day)
        self.SetEndDate(edt.year, edt.month, edt.day)
        start_cash = float(self._pf("start_cash"))
        # Use USDT cash for Binance USDT-quoted pairs
        self.SetCash("USDT", start_cash)
        # Optional: zero out USD to make intent explicit
        try:
            self.SetCash("USD", 0)
        except Exception:
            pass

        self.SetBrokerageModel(BrokerageName.Binance, AccountType.Cash)
        self.UniverseSettings.Resolution = Resolution.Hour
        self.SetWarmup(int(self._pf("warmup_hours")), Resolution.Hour)

        self.top_n             = int(self._pf("top_n"))
        self.side_pref         = str(self._p("side")).upper()
        self.min_mom_sum       = float(self._pf("min_momentum_sum"))
        self.min_atr_ratio     = float(self._pf("min_atr_ratio"))
        self.min_vol_mult      = float(self._pf("min_vol_surge_mult"))
        self.min_qv_24h        = float(self._pf("min_qv_24h"))
        self.min_qv_1h         = float(self._pf("min_qv_1h"))
        self.min_breadth       = float(self._pf("min_breadth"))
        self.sl_atr_mult       = float(self._pf("sl_atr_mult"))
        self.tp_atr_mult       = float(self._pf("tp_atr_mult"))
        self.max_hold_hours    = int(self._pf("max_hold_hours"))
        self.max_mae_atr_mult  = float(self._pf("max_mae_atr_mult"))
        self.mom_flip_thresh   = float(self._pf("mom_flip_thresh"))
        self.trail_start_atr   = float(self._pf("trail_start_atr"))
        self.trail_dist_atr    = float(self._pf("trail_dist_atr"))
        self.leverage          = float(self._pf("leverage"))
        self.entry_every_hours = int(self._pf("entry_every_hours"))
        self.use_limit_tp      = int(self._pf("use_limit_tp"))
        self.quote_ccy         = str(self._p("quote_currency")).upper()
        self.plot_equity       = int(self._pf("plot_equity"))
        self.export_equity_csv = int(self._pf("export_equity_csv"))
        self.export_trades_csv = int(self._pf("export_trades_csv"))

        universe_raw = (self.GetParameter("universe") or "").strip()
        if universe_raw:
            self.universe_symbols = [s.strip().upper().replace("-", "") for s in universe_raw.split(",") if s.strip()]
        else:
            self.universe_symbols = list(self.RAW_UNIVERSE)

        self.symbols = []
        for tkr in self.universe_symbols:
            try:
                sym = self.AddCrypto(tkr, Resolution.Hour, Market.Binance).Symbol
                try:
                    self.Securities[sym].SetLeverage(self.leverage)
                except Exception:
                    pass
                self.symbols.append(sym)
            except Exception as e:
                self.Debug(f"AddCrypto failed for {tkr}: {e}")

        self.last_selection_time = None
        self.pos = {}

        self.daily_equity = {}
        perf = Chart("Performance"); perf.AddSeries(Series("Equity", SeriesType.Line, 0)); self.AddChart(perf)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.Every(timedelta(hours=1)), self._hourly_tick)

        self.Debug(f"C2 params: top_n={self.top_n} side={self.side_pref} min_mom_sum={self.min_mom_sum} "
                   f"min_atr_ratio={self.min_atr_ratio} vol_mult={self.min_vol_mult} min_qv24={self.min_qv_24h} "
                   f"min_qv1h={self.min_qv_1h} min_breadth={self.min_breadth} sl_atr={self.sl_atr_mult} "
                   f"tp_atr={self.tp_atr_mult} max_hold_h={self.max_hold_hours} flip={self.mom_flip_thresh} "
                   f"trail_start={self.trail_start_atr} trail_dist={self.trail_dist_atr} symbols={len(self.symbols)}")

    # ----- Helpers -----
    def _p(self, name: str):
        v = self.GetParameter(name)
        return str(v) if v is not None and v != "" else str(self.DEFAULTS.get(name))

    def _pf(self, name: str):
        v = self.GetParameter(name)
        if v is None or v == "":
            return self.DEFAULTS.get(name)
        try:
            base = self.DEFAULTS.get(name)
            if isinstance(base, (int, float)):
                return float(v)
            return v
        except Exception:
            return self.DEFAULTS.get(name)

    def _hourly_tick(self):
        self.Evaluate()
        if self.plot_equity:
            self.Plot("Performance", "Equity", float(self.Portfolio.TotalPortfolioValue))

    def OnEndOfDay(self):
        self.daily_equity[self.Time.date()] = float(self.Portfolio.TotalPortfolioValue)

    def OnEndOfAlgorithm(self):
        days = sorted(self.daily_equity.keys())
        if not days:
            return
        first_equity = self.daily_equity[days[0]]
        prev_e = None
        daily_rows = ["date,equity,ret_d"]
        from collections import defaultdict
        monthly = defaultdict(list)
        for d in days:
            e = self.daily_equity[d]
            r = 0.0 if prev_e is None else (e/prev_e - 1.0)
            daily_rows.append(f"{d.isoformat()},{e:.2f},{r:.6f}")
            monthly[(d.year, d.month)].append((d, e))
            prev_e = e
        if self.export_equity_csv:
            try:
                self.ObjectStore.Save("equity.csv", "\n".join(daily_rows))
            except Exception as e:
                self.Debug(f"ObjectStore equity.csv save failed: {e}")
        if self.export_trades_csv:
            try:
                trades = self.TradeBuilder.ClosedTrades
                header = "symbol,entry_time,entry_price,exit_time,exit_price,profit,profit_pct,direction,quantity"
                lines = [header]
                for t in trades:
                    direction = "LONG" if t.Direction == TradeDirection.Long else "SHORT"
                    profit_pct = (t.ExitPrice / t.EntryPrice - 1.0) * (1 if direction == "LONG" else -1)
                    lines.append(",".join([
                        str(t.Symbol), t.EntryTime.isoformat(), f"{t.EntryPrice:.8f}",
                        t.ExitTime.isoformat(), f"{t.ExitPrice:.8f}", f"{t.ProfitLoss:.2f}",
                        f"{profit_pct:.6f}", direction, f"{t.Quantity:.8f}"
                    ]))
                self.ObjectStore.Save("trades.csv", "\n".join(lines))
            except Exception as e:
                self.Debug(f"ObjectStore trades.csv save failed: {e}")
        total_return = self.Portfolio.TotalPortfolioValue / first_equity - 1.0 if first_equity else 0.0
        self.Debug(f"[STATS] Total return: {total_return:.4f}")

    def OnOrderEvent(self, orderEvent: OrderEvent):
        if orderEvent.Status in (OrderStatus.Filled, OrderStatus.PartiallyFilled):
            sym = orderEvent.Symbol
            if not self.Portfolio[sym].Invested:
                info = self.pos.get(sym, {})
                for k in ("sl_ticket", "tp_ticket"):
                    ticket = info.get(k)
                    if ticket and ticket.Status not in (OrderStatus.Canceled, OrderStatus.Filled):
                        try:
                            ticket.Cancel("OCO pair: counterpart filled/flat")
                        except Exception:
                            pass
                self.pos.pop(sym, None)

    # ---- Core ----
    def Evaluate(self):
        if self.IsWarmingUp:
            return
        now = self.Time
        if self.last_selection_time is not None:
            delta_h = (now - self.last_selection_time).total_seconds() / 3600.0
            if delta_h < self.entry_every_hours - 1e-9:
                self.ManageOpenPositions()
                return

        try:
            hist = self.History(self.symbols, 60, Resolution.Hour)
        except Exception as e:
            self.Debug(f"History error: {e}")
            return
        if hist.empty:
            self.ManageOpenPositions()
            return

        # Build set of available symbol keys in history (string compare is robust across QC wrappers)
        try:
            idx_syms = set(str(s) for s in hist.index.get_level_values(0).unique())
        except Exception:
            # If for some reason it's not a MultiIndex, skip processing safely
            self.ManageOpenPositions()
            return

        feats = {}
        for sym in self.symbols:
            if str(sym) not in idx_syms:
                continue
            try:
                df = hist.xs(sym, level=0).sort_index()
            except Exception:
                try:
                    df = hist.loc[sym].sort_index()
                except Exception:
                    continue
            if df is None or len(df) < 30:
                continue

            closes = df["close"].values.tolist()
            highs  = df["high"].values.tolist()
            lows   = df["low"].values.tolist()
            vols   = df["volume"].values.tolist()
            if len(closes) < 25:
                continue

            price = closes[-1]
            if price <= 0:
                continue

            dp6  = (closes[-1] - closes[-7]) / max(closes[-7], 1e-12) if len(closes) >= 8 else 0.0
            dp12 = (closes[-1] - closes[-13]) / max(closes[-13], 1e-12) if len(closes) >= 14 else 0.0

            atr = self._atr_from_series(highs, lows, closes, period=14)
            atr_ratio = atr / price if price > 0 else 0.0

            qv_last = price * vols[-1]
            qv_24h  = 0.0
            upto = min(24, len(vols))
            for i in range(1, upto + 1):
                qv_24h += closes[-i] * vols[-i]
            avg1h = qv_24h / 24.0 if qv_24h > 0 else 0.0

            feats[sym] = {
                "price": price,
                "dp6h": dp6,
                "dp12h": dp12,
                "atr": atr,
                "atr_ratio": atr_ratio,
                "qv_1h": qv_last,
                "qv_24h": qv_24h,
                "avg1h": avg1h,
            }

        # Liquidity filter
        liquid = [sym for sym, f in feats.items() if f["qv_24h"] >= self.min_qv_24h and f["qv_1h"] >= self.min_qv_1h]
        if not liquid:
            self.ManageOpenPositions(feats=feats, breadth=0.0)
            return

        # Rank with ATR filter
        items = []
        for sym in liquid:
            f = feats[sym]
            score = (f["dp6h"] + f["dp12h"]) if f["atr_ratio"] >= self.min_atr_ratio else -1e9
            items.append((sym, score))
        items.sort(key=lambda x: x[1], reverse=True)

        considered = [1 for _, s in items if s > -1e9/2]
        positive = [1 for _, s in items if s > 0]
        breadth = (sum(positive) / max(len(considered), 1)) if considered else 0.0

        selected_syms = [sym for sym, _ in items[: self.top_n]]

        # Entries (LONG only)
        # Cash-aware sizing per quote currency to avoid insufficient buying power
        try:
            avail_cash = float(self.Portfolio.CashBook[self.quote_ccy].Amount)
        except Exception:
            avail_cash = float(self.Portfolio.Cash)
        remaining_slots = len([s for s in selected_syms if not self.Portfolio[s].Invested])
        for sym in selected_syms:
            if self.Portfolio[sym].Invested:
                continue
            f = feats.get(sym)
            if not f:
                continue
            mom_sum = f["dp6h"] + f["dp12h"]
            if self.side_pref not in ("BOTH", "LONG"):
                continue
            if mom_sum < self.min_mom_sum:
                continue
            if f["atr_ratio"] < self.min_atr_ratio:
                continue
            if f["avg1h"] <= 0 or f["qv_1h"] < self.min_vol_mult * f["avg1h"]:
                continue
            if breadth < self.min_breadth:
                continue

            # Allocate at most target_value or proportional share of available quote cash
            target_value = self.Portfolio.TotalPortfolioValue / max(self.top_n, 1)
            per_trade_budget = min(target_value, avail_cash / max(1, remaining_slots))
            qty = math.floor(per_trade_budget / max(f["price"], 1e-8))
            if qty <= 0:
                continue

            # Deduct from available cash budget for subsequent entries
            avail_cash -= qty * f["price"]
            remaining_slots = max(0, remaining_slots - 1)

            market_ticket = self.MarketOrder(sym, qty)
            self.Debug(f"BUY {sym} qty={qty} price~{f['price']:.4f} mom_sum={mom_sum:.4f} atrr={f['atr_ratio']:.4f} breadth={breadth:.3f}")

            sl_price = f["price"] * (1.0 - self.sl_atr_mult * f["atr_ratio"])
            tp_price = f["price"] * (1.0 + self.tp_atr_mult * f["atr_ratio"]) if self.tp_atr_mult > 0 else None
            sl_ticket = self.StopMarketOrder(sym, -qty, sl_price)
            tp_ticket = None
            if self.use_limit_tp and tp_price is not None:
                tp_ticket = self.LimitOrder(sym, -qty, tp_price)

            self.pos[sym] = {
                "entry_time": self.Time,
                "entry_price": f["price"],
                "sl_ticket": sl_ticket,
                "tp_ticket": tp_ticket,
            }

        self.last_selection_time = now
        self.ManageOpenPositions(feats=feats, breadth=breadth)

    def ManageOpenPositions(self, feats=None, breadth=None):
        if feats is None:
            inv_syms = [sym for sym in self.symbols if self.Portfolio[sym].Invested]
            if not inv_syms:
                return
            try:
                hist = self.History(inv_syms, 30, Resolution.Hour)
            except Exception:
                return
            if hist.empty:
                return
            try:
                idx_syms = set(str(s) for s in hist.index.get_level_values(0).unique())
            except Exception:
                return
            feats = {}
            for sym in inv_syms:
                if str(sym) not in idx_syms:
                    continue
                try:
                    df = hist.xs(sym, level=0).sort_index()
                except Exception:
                    try:
                        df = hist.loc[sym].sort_index()
                    except Exception:
                        continue
                if len(df) < 15:
                    continue
                closes = df["close"].values.tolist()
                highs  = df["high"].values.tolist()
                lows   = df["low"].values.tolist()
                price = closes[-1]
                dp6  = (closes[-1] - closes[-7]) / max(closes[-7], 1e-12) if len(closes) >= 8 else 0.0
                dp12 = (closes[-1] - closes[-13]) / max(closes[-13], 1e-12) if len(closes) >= 14 else 0.0
                atr = self._atr_from_series(highs, lows, closes, period=14)
                atr_ratio = atr / price if price > 0 else 0.0
                feats[sym] = {"price": price, "dp6h": dp6, "dp12h": dp12, "atr_ratio": atr_ratio}
        if breadth is None:
            breadth = 1.0

        for sym, info in list(self.pos.items()):
            if not self.Portfolio[sym].Invested:
                continue
            f = feats.get(sym)
            if not f:
                continue
            price = f["price"]
            atrr  = f["atr_ratio"]
            entry_price = info.get("entry_price", price)

            # Time stop
            ent_t = info.get("entry_time")
            if ent_t is not None:
                elapsed_h = (self.Time - ent_t).total_seconds() / 3600.0
                if elapsed_h >= self.max_hold_hours:
                    self.Liquidate(sym, tag="time_stop")
                    self.Debug(f"EXIT {sym} reason=time_stop")
                    continue

            # MAE
            ret = (price - entry_price) / max(entry_price, 1e-12)
            if ret < -self.max_mae_atr_mult * atrr:
                self.Liquidate(sym, tag="mae_break")
                self.Debug(f"EXIT {sym} reason=mae_break")
                continue

            # Momentum flip
            mom_sum = f["dp6h"] + f["dp12h"]
            if mom_sum < self.mom_flip_thresh:
                self.Liquidate(sym, tag="mom_flip")
                self.Debug(f"EXIT {sym} reason=mom_flip")
                continue

            # Trailing stop
            if atrr > 0 and self.trail_start_atr > 0:
                up = (price - entry_price) / max(entry_price, 1e-12)
                if up >= self.trail_start_atr * atrr:
                    new_sl = price * (1.0 - self.trail_dist_atr * atrr)
                    sl_ticket = info.get("sl_ticket")
                    if sl_ticket and sl_ticket.Status not in (OrderStatus.Filled, OrderStatus.Canceled):
                        try:
                            current_stop = sl_ticket.Get(OrderField.StopPrice)
                        except Exception:
                            current_stop = None
                        if (current_stop is None) or (new_sl > float(current_stop)):
                            try:
                                fields = UpdateOrderFields()
                                fields.StopPrice = new_sl
                                fields.Tag = "trail_up"
                                try:
                                    res = sl_ticket.Update(fields)
                                    if not res.IsSuccess:
                                        self.Debug(f"SL update failed for {sym}: {res.ErrorMessage}")
                                except Exception as e:
                                    self.Debug(f"SL update exception for {sym}: {e}")
                                self.Debug(f"TRAIL {sym} move SL -> {new_sl:.6f}")
                            except Exception as e:
                                self.Debug(f"SL update failed for {sym}: {e}")

    @staticmethod
    def _atr_from_series(highs, lows, closes, period=14):
        if len(closes) < period + 1:
            return 0.0
        trs = []
        for i in range(1, len(closes)):
            hl  = highs[i] - lows[i]
            hc  = abs(highs[i] - closes[i-1])
            lc  = abs(lows[i]  - closes[i-1])
            tr = max(hl, hc, lc)
            trs.append(tr)
        if len(trs) < period:
            return 0.0
        return sum(trs[-period:]) / float(period)
