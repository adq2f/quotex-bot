"""
datafeed.py  --  candle sources.
Contract: get_candles() returns CLOSED candles only, index = candle OPEN time (UTC).
"""
import asyncio
import os
import threading
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd

# ---- yfinance import ----
try:
    import yfinance as yf
except Exception:
    yf = None

COLS = ["open", "high", "low", "close", "volume"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def drop_unclosed(df: pd.DataFrame, tf_seconds: int, now: datetime) -> pd.DataFrame:
    if df.empty:
        return df
    close_time = df.index + pd.Timedelta(seconds=tf_seconds)
    return df[close_time <= pd.Timestamp(now)]


def resample_ohlc(df: pd.DataFrame, rule: str) -> pd.DataFrame:
    agg = {"open": "first", "high": "max", "low": "min", "close": "last"}
    if "volume" in df.columns:
        agg["volume"] = "sum"
    out = df.resample(rule, label="left", closed="left").agg(agg).dropna(subset=["open", "close"])
    if "volume" not in out.columns:
        out["volume"] = 0.0
    return out


def tf_rule(tf_seconds: int) -> str:
    return {60: "1min", 300: "5min", 900: "15min", 3600: "1h", 14400: "4h"}.get(tf_seconds, f"{tf_seconds}s")


class CandleSource(ABC):
    @abstractmethod
    def get_candles(self, asset: str, tf_seconds: int, count: int, now: Optional[datetime] = None) -> pd.DataFrame: ...

    def get_payout(self, asset: str) -> Optional[float]:
        return None

    def get_balance(self) -> Optional[float]:
        return None

    def buy(self, asset: str, direction: str, stake: float, expiry_seconds: int) -> bool:
        raise NotImplementedError

    def close(self) -> None:
        pass

    def get_candles_peek(self, asset: str, tf_seconds: int, count: int,
                         now: Optional[datetime] = None) -> pd.DataFrame:
        """Default: fall back to get_candles (no forming candle)."""
        return self.get_candles(asset, tf_seconds, count, now)


class SyntheticSource(CandleSource):
    def __init__(self, seed: int = 7, base_price: float = 1.1000, days: int = 120):
        self.seed, self.base, self.days = seed, base_price, days
        self._cache = {}

    def _build(self, asset: str, now: datetime) -> pd.DataFrame:
        rng = np.random.default_rng(abs(hash((asset, self.seed))) % (2**32))
        n = self.days * 24 * 12
        end = pd.Timestamp(now).floor("5min")
        idx = pd.date_range(end=end, periods=n, freq="5min", tz="UTC")
        vol = 0.00035 * (1 + 0.8 * np.abs(np.sin(np.arange(n) / 90.0))) * np.exp(rng.normal(0, 0.25, n))
        drift = 0.00002 * np.sin(np.arange(n) / 700.0)
        ret = drift + vol * rng.standard_normal(n)
        close = self.base * np.exp(np.cumsum(ret))
        open_ = np.r_[close[0], close[:-1]]
        rng_hl = np.abs(rng.normal(0, 1, n)) * vol * close
        high = np.maximum(open_, close) + rng_hl * rng.random(n)
        low = np.minimum(open_, close) - rng_hl * rng.random(n)
        return pd.DataFrame({"open": open_, "high": high, "low": low, "close": close,
                             "volume": np.zeros(n)}, index=idx)

    def get_candles(self, asset, tf_seconds, count, now=None):
        now = now or utcnow()
        key = (asset, pd.Timestamp(now).floor("5min"))
        if key not in self._cache:
            self._cache = {key: self._build(asset, now)}
        m5 = self._cache[key]
        df = m5 if tf_seconds == 300 else resample_ohlc(m5, tf_rule(tf_seconds))
        df = drop_unclosed(df, tf_seconds, now)
        return df.iloc[-count:]

    def get_payout(self, asset):
        return 85.0


class CSVSource(CandleSource):
    def __init__(self, directory: str):
        self.dir = directory
        self._m5 = {}

    def load(self, asset: str) -> pd.DataFrame:
        if asset not in self._m5:
            self._m5[asset] = load_csv(f"{self.dir}/{asset}_M5.csv")
        return self._m5[asset]

    def get_candles(self, asset, tf_seconds, count, now=None):
        now = now or utcnow()
        m5 = self.load(asset)
        df = m5 if tf_seconds == 300 else resample_ohlc(m5, tf_rule(tf_seconds))
        return drop_unclosed(df, tf_seconds, now).iloc[-count:]


def load_csv(path: str) -> pd.DataFrame:
    df = pd.read_csv(path)
    df.columns = [c.strip().lower() for c in df.columns]
    tcol = "time" if "time" in df.columns else ("timestamp" if "timestamp" in df.columns else df.columns[0])
    t = df[tcol]
    if np.issubdtype(t.dtype, np.number):
        v = float(t.iloc[0])
        unit = "ns" if v > 1e17 else "us" if v > 1e14 else "ms" if v > 1e11 else "s"
        idx = pd.to_datetime(t, unit=unit, utc=True)
    else:
        idx = pd.to_datetime(t, utc=True)
    df.index = idx
    if "volume" not in df.columns:
        df["volume"] = 0.0
    return df[COLS].astype(float).sort_index()


class YFinanceSource(CandleSource):
    """Free forex data via Yahoo Finance."""

    SYMBOL_MAP = {
        "EURUSD": "EURUSD=X", "GBPUSD": "GBPUSD=X", "USDJPY": "USDJPY=X",
        "AUDUSD": "AUDUSD=X", "USDCAD": "USDCAD=X", "USDCHF": "USDCHF=X",
        "NZDUSD": "NZDUSD=X", "EURGBP": "EURGBP=X", "EURJPY": "EURJPY=X",
        "GBPJPY": "GBPJPY=X", "XAUUSD": "GC=F", "BTCUSD": "BTC-USD", "ETHUSD": "ETH-USD",
    }

    def __init__(self):
        if yf is None:
            raise RuntimeError("yfinance not installed. Run: pip install yfinance")

    @classmethod
    def _map_asset(cls, asset: str) -> str:
        base = asset.lower().replace("_otc", "").replace("/", "").upper()
        return cls.SYMBOL_MAP.get(base, f"{base}=X")

    def get_candles(self, asset, tf_seconds, count, now=None):
        try:
            symbol = self._map_asset(asset)
            interval = tf_rule(tf_seconds).replace("min", "m").replace("1h", "60m").replace("4h", "1h")
            period = "5d" if tf_seconds <= 300 else "1mo"
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=False, threads=False)
            if df is None or len(df) == 0:
                return pd.DataFrame(columns=COLS)
            df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
            if "adj close" in df.columns:
                df = df.drop(columns=["adj close"])
            if "volume" not in df.columns:
                df["volume"] = 0.0
            df = df[COLS].astype(float)
            df.index = pd.to_datetime(df.index, utc=True)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
            if tf_seconds not in (60, 300, 900, 3600, 14400):
                df = resample_ohlc(df, tf_rule(tf_seconds))
            df = drop_unclosed(df, tf_seconds, now or utcnow())
            return df.iloc[-count:]
        except Exception as e:
            print(f"[YFinanceSource] get_candles failed for {asset}: {e}")
            return pd.DataFrame(columns=COLS)

    def get_candles_peek(self, asset, tf_seconds, count, now=None):
        """Like get_candles but INCLUDES the forming candle (for PREPARE alerts)."""
        try:
            symbol = self._map_asset(asset)
            interval = tf_rule(tf_seconds).replace("min", "m").replace("1h", "60m").replace("4h", "1h")
            period = "5d" if tf_seconds <= 300 else "1mo"
            df = yf.download(symbol, period=period, interval=interval,
                             progress=False, auto_adjust=False, threads=False)
            if df is None or len(df) == 0:
                return pd.DataFrame(columns=COLS)
            df.columns = [c.lower() if isinstance(c, str) else c for c in df.columns]
            if "adj close" in df.columns:
                df = df.drop(columns=["adj close"])
            if "volume" not in df.columns:
                df["volume"] = 0.0
            df = df[COLS].astype(float)
            df.index = pd.to_datetime(df.index, utc=True)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
            # NOTE: no drop_unclosed — we want the forming candle
            return df.iloc[-count:]
        except Exception as e:
            print(f"[YFinanceSource] peek failed for {asset}: {e}")
            return pd.DataFrame(columns=COLS)

    def get_payout(self, asset):
        return None

    def close(self):
        pass


def make_source(cfg, store=None) -> CandleSource:
    src = (cfg.data_source or "synthetic").lower().strip()
    if src == "yfinance":
        return YFinanceSource()
    if src == "csv":
        return CSVSource(cfg.csv_dir)
    return SyntheticSource()
