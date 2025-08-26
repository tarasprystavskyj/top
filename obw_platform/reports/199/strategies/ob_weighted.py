# ob_weighted.py — стратегія на зваженому OB індексі
import pandas as pd
import numpy as np

class OBWeighted:
    @staticmethod
    def prepare(df: pd.DataFrame, cfg: dict):
        """
        Підготовка індикаторів.
        df: DataFrame з колонками open, high, low, close, volume.
        cfg: dict з параметрами стратегії.
        """
        close = df["close"].astype(float)

        # RSI
        delta = close.diff()
        gain = delta.where(delta > 0, 0.0)
        loss = -delta.where(delta < 0, 0.0)
        avg_gain = gain.rolling(14).mean()
        avg_loss = loss.rolling(14).mean()
        rs = avg_gain / avg_loss.replace(0, np.nan)
        df["rsi"] = 100 - (100 / (1 + rs))

        # Stochastic %K
        low14 = df["low"].rolling(14).min()
        high14 = df["high"].rolling(14).max()
        df["stoch"] = 100 * (close - low14) / (high14 - low14).replace(0, np.nan)

        # Money Flow Index (MFI)
        tp = (df["high"] + df["low"] + close) / 3
        mf = tp * df["volume"]
        pos_mf = mf.where(tp > tp.shift(), 0.0)
        neg_mf = mf.where(tp < tp.shift(), 0.0)
        pos_sum = pos_mf.rolling(14).sum()
        neg_sum = neg_mf.rolling(14).sum().abs()
        mfr = pos_sum / neg_sum.replace(0, np.nan)
        df["mfi"] = 100 - (100 / (1 + mfr))

        # High-close proximity (HCP)
        df["hcp"] = 100 * (df["high"] - close) / (df["high"] - df["low"]).replace(0, np.nan)

        return df

    @staticmethod
    def select(df: pd.DataFrame, cfg: dict):
        """
        Повертає True/False, чи входити в шорт по даному символу.
        Використовує зважений OB індекс.
        """
        w_rsi = cfg.get("w_rsi", 0.25)
        w_stoch = cfg.get("w_stoch", 0.25)
        w_mfi = cfg.get("w_mfi", 0.25)
        w_hcp = cfg.get("w_hcp", 0.25)

        # Зважений OB
        ob = (
            w_rsi * df["rsi"].iloc[-1] +
            w_stoch * df["stoch"].iloc[-1] +
            w_mfi * df["mfi"].iloc[-1] +
            w_hcp * df["hcp"].iloc[-1]
        )

        min_ob = cfg.get("min_ob", 85)
        max_atr_ratio = cfg.get("max_atr_ratio", 0.05)

        # ATR ratio
        tr1 = df["high"] - df["low"]
        tr2 = (df["high"] - df["close"].shift()).abs()
        tr3 = (df["low"] - df["close"].shift()).abs()
        tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
        atr = tr.rolling(14).mean()
        atr_ratio = (atr.iloc[-1] / df["close"].iloc[-1]) if df["close"].iloc[-1] > 0 else 0

        if ob >= min_ob and atr_ratio <= max_atr_ratio:
            return True
        return False

    @staticmethod
    def direction():
        """
        Напрямок угоди: для OB > threshold це шорт.
        """
        return "short"
