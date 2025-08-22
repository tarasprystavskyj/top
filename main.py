from AlgorithmImports import *
from datetime import timedelta

# === STATIC CONFIGURATION ===
# Set numeric thresholds to 0 to disable that filter
ENTRY_HOUR_UTC = 1  # UTC hour for selection
TOP_N = 4
HOLD_HOURS = 24 # 36  # hours to hold before forced close
COOLDOWN_DAYS = 2 # 5  # set to 0 to disable cooldown
MIN_OVERBUGHT_INDEX = 70  # set to 0 to disable this filter
MIN_RSI = 60  # set to 0 to ignore RSI in high-indicator count
REQUIRE_AT_LEAST_N_HIGH = 0 # 2  # set to 0 to disable requirement of multiple high indicators
MAX_ATR_RATIO = 0 # 0.05  # set to 0 to disable ATR ratio filtering
RISK_PCT = 0.02  # used for stop loss calculation
# Expanded universe (top gainers from last 19 days), original names with hyphens; will sanitize by removing '-'
RAW_UNIVERSE = [
    "ZORA-USDT","SPK-USDT","AIOT-USDT","ELX-USDT","DIA-USDT","PORT3-USDT","VINE-USDT","CROSS-USDT","USELESS-USDT",
    "HOUSE-USDT","EPIC-USDT","C-USDT","DOLO-USDT","SQD-USDT","BABY-USDT","KERNEL-USDT","ASR-USDT","LAUNCHCOIN-USDT",
    "PENGU-USDT","HIPPO-USDT","BROCCOLIF3B-USDT","BSW-USDT","DOOD-USDT","PUMP-USDT","1000CAT-USDT","VELVET-USDT",
    "ORDER-USDT","GOR-USDT","FRAG-USDT","HEI-USDT","CKB-USDT","SXT-USDT","M-USDT","REX-USDT","AGT-USDT","ZRC-USDT",
    "DBR-USDT","HYPERLANE-USDT","OMNINETWORK-USDT","VIC-USDT","FHE-USDT","USUAL-USDT","XLM-USDC","XLM-USDT",
    "ALGO-USDC","ALGO-USDT","HBAR-USDT","HBAR-USDC","PROMPT-USDT","TURBO-USDT","AVAAI-USDT","FUNTOKEN-USDT",
    "HAEDAL-USDT","BANANA-USDT","THE-USDT","SERAPH-USDT","REZ-USDT","AEVO-USDT","AEVO-USDC","HFT-USDT","CUDIS-USDT",
    "FLOKI-USDT","1000BONK-USDT","1000BONK-USDC","BID-USDT","BOME-USDC","BOME-USDT","TOSHI-USDT","FXS-USDT",
    "CRV-USDC","CRV-USDT","DMC-USDT","XRP-USDT","TANSSI-USDT","SUSHI-USDT","USTC-USDT","NEIROETH-USDT","ETC-USDC",
    "RDO-USDT","BB-USDT","KDA-USDT","MEW-USDT","CFX-USDT","OM-USDT","PHB-USDT","UMA-USDT","DRIFT-USDT","API3-USDT",
    "FIDA-USDT","SYN-USDT","SLP-USDT","SWELL-USDT","ZKJ-USDT","SAHARA-USDT","H-USDT","NEWT-USDT","PARTI-USDT",
    "OL-USDT","FIS-USDT","HIFI-USDT","ENA-USDT","TAG-USDT","OBOL-USDT","TUT-USDT","TRU-USDT","VVV-USDT","GLM-USDT",
    "SANTOS-USDT","BANANAS31-USDT","KNC-USDT","GORK-USDT","MUBARAK-USDT","B3-USDT","ICNT-USDT","ZBCN-USDT","ALCH-USDT",
    "B-USDT","BIO-USDT","RFC-USDT","B2-USDT","HOME-USDT","MEME-USDT","TAC-USDT","BDXN-USDT","A2Z-USDT","ESPORTS-USDT",
    "SOPH-USDT","MAGIC-USDT","SWARMS-USDT","SOON-USDT","KOMA-USDT"
]
LOCAL_PEAK_ENABLED = 0  # set to 1 to enable local peak check

class ShortTopGainersCryptoStaticUniverse(QCAlgorithm):
    def Initialize(self):
        # warmup so history has enough bars
        self.SetWarmup(48, Resolution.Hour)

        self.SetStartDate(2024, 1, 1)
        self.SetEndDate(2025, 1, 1)
        self.SetCash(100000)

        # Static config with optional overrides
        self.entry_hour_utc = int(self.GetParameter("entry_hour_utc") or ENTRY_HOUR_UTC)
        self.top_n = int(self.GetParameter("top_n") or TOP_N)
        self.hold_hours = float(self.GetParameter("hold_hours") or HOLD_HOURS)
        self.cooldown_days = int(self.GetParameter("cooldown_days") or COOLDOWN_DAYS)
        self.min_overbought_index = float(self.GetParameter("min_overbought_index") if self.GetParameter("min_overbought_index") is not None else MIN_OVERBUGHT_INDEX)
        self.min_rsi = float(self.GetParameter("min_rsi") if self.GetParameter("min_rsi") is not None else MIN_RSI)
        self.require_at_least_n_high = int(self.GetParameter("require_at_least_n_high") or REQUIRE_AT_LEAST_N_HIGH)
        self.max_atr_ratio = float(self.GetParameter("max_atr_ratio") if self.GetParameter("max_atr_ratio") is not None else MAX_ATR_RATIO)
        self.risk_pct = float(self.GetParameter("risk_pct") or RISK_PCT)
        universe_param = self.GetParameter("universe") or ",".join(RAW_UNIVERSE)
        # sanitize: remove hyphens so Binance style (e.g., ZORAUSDT)
        self.universe_symbols = [s.replace('-', '').strip().upper() for s in universe_param.split(',') if s.strip()]
        self.local_peak_enabled = int(self.GetParameter("local_peak_enabled") or LOCAL_PEAK_ENABLED)

        # State
        self.last_used = {}
        self.open_positions = {}

        # Scheduling
        self.Schedule.On(self.DateRules.EveryDay(),
                         self.TimeRules.At(self.entry_hour_utc, 0),
                         self.RunSelection)
        self.Schedule.On(self.DateRules.EveryDay(),
                         self.TimeRules.Every(timedelta(hours=1)),
                         self.CloseExpiredPositions)

        # Add crypto symbols from Binance
        self.symbol_objects = {}
        self.add_failures = []
        for sym in self.universe_symbols:
            try:
                symbol = self.AddCrypto(sym, Resolution.Hour, Market.BINANCE).Symbol
                try:
                    self.Securities[symbol].SetLeverage(2)
                except Exception:
                    pass
                self.symbol_objects[sym] = symbol
            except Exception as e:
                self.add_failures.append((sym, str(e)))
                self.Log(f"Failed to add crypto {sym}: {e}")

        self.Log(f"Initialized universe: {list(self.symbol_objects.keys())} (requested {len(self.universe_symbols)})")
        if self.add_failures:
            self.Log(f"AddCrypto failures: {self.add_failures}")

        self.Log(f"Config: entry_hour_utc={self.entry_hour_utc}, top_n={self.top_n}, hold_hours={self.hold_hours}, "
                 f"cooldown_days={self.cooldown_days}, min_overbought_index={self.min_overbought_index}, min_rsi={self.min_rsi}, "
                 f"require_at_least_n_high={self.require_at_least_n_high}, max_atr_ratio={self.max_atr_ratio}, "
                 f"risk_pct={self.risk_pct}, local_peak_enabled={self.local_peak_enabled}")

    def RunSelection(self):
        now = self.Time
        self.Log(f"RunSelection at {now}. Universe size: {len(self.symbol_objects)}")
        candidates = []
        lookback = 24

        # diagnostic counters
        no_history = 0
        not_in_index = 0
        too_few_bars = 0
        prev_close_zero = 0

        for sym_str, symbol in self.symbol_objects.items():
            if self.cooldown_days > 0:
                last = self.last_used.get(symbol, None)
                if last and (now.date() - last.date()).days < self.cooldown_days:
                    continue

            history = self.History([symbol], timedelta(hours=lookback + 15), Resolution.Hour)
            if history.empty:
                no_history += 1
                self.Log(f"No history for {symbol}")
                continue
            if symbol not in history.index.get_level_values(0):
                not_in_index += 1
                self.Log(f"{symbol} missing from history index levels")
                continue
            df = history.loc[symbol].sort_index()
            if len(df) < lookback + 15:
                too_few_bars += 1
                self.Log(f"Too few bars for {symbol}: {len(df)} < {lookback+15}")
                continue

            prev_close = df["close"].iloc[-(lookback + 1)]
            current_price = df["close"].iloc[-1]
            if prev_close == 0:
                prev_close_zero += 1
                continue
            gain_24h_before = (current_price - prev_close) / prev_close * 100

            indicator_window = df.iloc[-15:-1]
            if len(indicator_window) < 14:
                self.Log(f"Indicator window too small for {symbol}")
                continue
            closes = indicator_window["close"].tolist()
            highs = indicator_window["high"].tolist()
            lows = indicator_window["low"].tolist()
            volumes = indicator_window["volume"].tolist()

            rsi = self.compute_rsi(closes, period=14) if self.min_rsi > 0 else None
            stoch_k = self.compute_stochastic_k(highs, lows, closes, k_period=14) if self.min_rsi > 0 else None
            mfi = self.compute_mfi(highs, lows, closes, volumes, period=14) if self.min_rsi > 0 else None
            overbought_index = self.compute_overbought_index(rsi, stoch_k, mfi) if self.min_overbought_index > 0 else None

            atr_ratio = None
            if self.max_atr_ratio > 0:
                atr_window_start = -(lookback + 1) - 24
                atr_window_end = -(lookback + 1)
                if len(df) >= abs(atr_window_start) + 1:
                    atr_window = df.iloc[atr_window_start:atr_window_end]
                    if len(atr_window) >= 1:
                        tr_values = [(row["high"] - row["low"]) for _, row in atr_window.iterrows()]
                        atr = sum(tr_values) / len(tr_values)
                        last_close_for_atr = atr_window["close"].iloc[-1]
                        if last_close_for_atr and last_close_for_atr != 0:
                            atr_ratio = atr / last_close_for_atr

            self.Log(f"Candidate raw: {symbol} gain24h={gain_24h_before:.2f} rsi={rsi} stoch_k={stoch_k} mfi={mfi} overbought_index={overbought_index} atr_ratio={atr_ratio}")

            if self.min_overbought_index > 0:
                if overbought_index is None or overbought_index < self.min_overbought_index:
                    continue

            if self.require_at_least_n_high > 0:
                cnt_high = 0
                if self.min_rsi > 0 and rsi is not None and rsi >= self.min_rsi:
                    cnt_high += 1
                if self.min_rsi > 0 and stoch_k is not None and stoch_k >= self.min_rsi:
                    cnt_high += 1
                if self.min_rsi > 0 and mfi is not None and mfi >= self.min_rsi:
                    cnt_high += 1
                if cnt_high < self.require_at_least_n_high:
                    continue

            if self.max_atr_ratio > 0:
                if atr_ratio is None or atr_ratio > self.max_atr_ratio:
                    continue

            if self.local_peak_enabled:
                prior_end = df.index[-2]
                prior_start = prior_end - timedelta(hours=3)
                prior_window = df.loc[(df.index >= prior_start) & (df.index < prior_end)]
                if len(prior_window) >= 1:
                    recent_high = prior_window["close"].max()
                    prev_close_val = df["close"].iloc[-2]
                    if prev_close_val < 0.98 * recent_high:
                        continue

            candidates.append({
                "symbol": symbol,
                "gain_24h_before": gain_24h_before,
                "overbought_index": overbought_index,
                "rsi": rsi,
                "stoch_k": stoch_k,
                "mfi": mfi,
                "atr_ratio": atr_ratio,
                "price": current_price
            })

        if not candidates:
            self.Log(f"No candidates passed filters. diagnostics: no_history={no_history} not_in_index={not_in_index} too_few_bars={too_few_bars} prev_close_zero={prev_close_zero}")
            return

        top_by_gain = sorted(candidates, key=lambda x: x["gain_24h_before"], reverse=True)[: self.top_n * 10]
        selected = sorted(top_by_gain, key=lambda x: x.get("overbought_index") if x.get("overbought_index") is not None else -1, reverse=True)[: self.top_n]

        self.Log(f"Selected: {[c['symbol'].Value for c in selected]}")
        weight = 1 / self.top_n
        for c in selected:
            symbol = c["symbol"]
            if self.Portfolio[symbol].Invested:
                continue
            price = self.Securities[symbol].Price
            if price is None or price == 0:
                continue

            target_value = self.Portfolio.TotalPortfolioValue * weight
            quantity = - target_value / price  # short

            self.MarketOrder(symbol, quantity)
            self.Log(f"OPEN_SHORT {symbol} price={price:.4f} OB={c.get('overbought_index')} gain24h={c['gain_24h_before']:.2f}")

            stop_price = price * (1 + self.risk_pct)
            self.StopMarketOrder(symbol, -quantity, stop_price)

            self.open_positions[symbol] = {
                "entry_time": self.Time,
                "entry_price": price
            }
            self.last_used[symbol] = self.Time

    def CloseExpiredPositions(self):
        now = self.Time
        to_close = []
        for symbol, info in list(self.open_positions.items()):
            if now - info["entry_time"] >= timedelta(hours=self.hold_hours):
                to_close.append(symbol)
        for symbol in to_close:
            if self.Portfolio[symbol].Invested:
                self.Liquidate(symbol)
                self.Log(f"CLOSE_BY_HOLD {symbol} at {self.Time}")
            self.open_positions.pop(symbol, None)

    # Indicator helpers
    def compute_rsi(self, closes, period=14):
        if len(closes) < period + 1:
            return None
        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i-1]
            if delta >= 0:
                gains.append(delta); losses.append(0)
            else:
                gains.append(0); losses.append(-delta)
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def compute_stochastic_k(self, highs, lows, closes, k_period=14):
        if len(closes) < k_period:
            return None
        highest = max(highs[-k_period:])
        lowest = min(lows[-k_period:])
        if highest - lowest == 0:
            return 50.0
        current_close = closes[-1]
        return (current_close - lowest) / (highest - lowest) * 100

    def compute_mfi(self, highs, lows, closes, volumes, period=14):
        if len(closes) < period + 1:
            return None
        typical_prices = [(h + l + c) / 3 for h, l, c in zip(highs, lows, closes)]
        raw_money_flow = [tp * v for tp, v in zip(typical_prices, volumes)]
        positive_flow = 0.0
        negative_flow = 0.0
        for i in range(1, period + 1):
            if typical_prices[i] > typical_prices[i-1]:
                positive_flow += raw_money_flow[i]
            else:
                negative_flow += raw_money_flow[i]
        if negative_flow == 0:
            return 100.0
        mfr = positive_flow / negative_flow
        return 100 - (100 / (1 + mfr))

    def compute_overbought_index(self, rsi, stoch_k, mfi):
        comps = []
        weights = []
        if rsi is not None:
            comps.append(rsi); weights.append(0.4)
        if stoch_k is not None:
            comps.append(stoch_k); weights.append(0.3)
        if mfi is not None:
            comps.append(mfi); weights.append(0.3)
        if not comps:
            return None
        return sum(c * w for c, w in zip(comps, weights)) / sum(weights)
