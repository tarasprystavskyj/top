import unittest

from obw_platform.telegram_signal_tools import fetch_futures_ohlcv_npz_v1 as mod


class FakeExchange:
    def __init__(self, batches):
        self.batches = list(batches)
        self.calls = []

    def parse_timeframe(self, timeframe):
        if timeframe == "1m":
            return 60
        raise ValueError(timeframe)

    def milliseconds(self):
        return 1_700_000_000_000

    def fetch_ohlcv(self, symbol, timeframe, since, limit):
        self.calls.append({"symbol": symbol, "timeframe": timeframe, "since": since, "limit": limit})
        if self.batches:
            return self.batches.pop(0)
        return []


class FetchFuturesOhlcvNpzTest(unittest.TestCase):
    def test_parse_utc_ms_handles_z_and_offsets(self):
        self.assertEqual(mod.parse_utc_ms("1970-01-01T00:00:00Z"), 0)
        self.assertEqual(mod.parse_utc_ms("1970-01-01T02:00:00+02:00"), 0)
        self.assertIsNone(mod.parse_utc_ms(""))

    def test_fetch_ohlcv_window_honors_since_until_and_dedups(self):
        batches = [
            [
                [0, 1, 2, 0.5, 1.5, 10],
                [60_000, 2, 3, 1.5, 2.5, 11],
            ],
            [
                [60_000, 2, 3, 1.5, 2.5, 11],
                [120_000, 3, 4, 2.5, 3.5, 12],
            ],
        ]
        ex = FakeExchange(batches)
        rows = mod.fetch_ohlcv_window(ex, "HYPE/USDT:USDT", "1m", 10, 0.0, since_ms=0, until_ms=180_000)

        self.assertEqual([int(row[0]) for row in rows], [0, 60_000, 120_000])
        self.assertEqual(ex.calls[0]["since"], 0)
        self.assertEqual(ex.calls[1]["since"], 120_000)


if __name__ == "__main__":
    unittest.main()
