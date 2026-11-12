"""
datafeed.py  --  candle sources.
Contract: get_candles() returns CLOSED candles only, index = candle OPEN time (UTC).
"""
import os
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Optional

import numpy as np
import pandas as pd
import requests

COLS = ["open", "high", "low", "close", "volume"]
BIQUOTE_BASE = "https://biquote.io/api"


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


def tf_to_biquote(tf_seconds: int) -> str:
    """biquote.io supports: 1m 5m 15m 30m 1h 4h 1d"""
    return {60: "1m", 300: "5m", 900: "15m", 3600: "1h", 14400: "4h"}.get(tf_seconds, "1m")


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
        return self.get_candles(asset, tf_seconds, count, now)


class BiQuoteSource(CandleSource):
    """Forex/CFD OHLC via biquote.io REST API (no key, 15k req/min)."""

    SYMBOL_MAP = {
        "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
        "AUDUSD": "AUDUSD", "USDCAD": "USDCAD", "USDCHF": "USDCHF",
        "NZDUSD": "NZDUSD", "EURGBP": "EURGBP", "EURJPY": "EURJPY",
        "GBPJPY": "GBPJPY", "XAUUSD": "XAUUSD", "BTCUSD": "BTCUSD",
        "ETHUSD": "ETHUSD",
    }

    def __init__(self):
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "quotex-signal-bot/1.0"})
        print("[BiQuoteSource] Using biquote.io REST API (no package, no key)")

    @classmethod
    def _map_asset(cls, asset: str) -> str:
        base = asset.lower().replace("_otc", "").replace("/", "").upper()
        return cls.SYMBOL_MAP.get(base, base)

    def _fetch_bars(self, symbol: str, interval: str, limit: int) -> list:
        url = f"{BIQUOTE_BASE}/{symbol}/ohlc"
        params = {"interval": interval, "limit": min(limit, 1000)}
        r = self._session.get(url, params=params, timeout=20)
        r.raise_for_status()
        data = r.json()
        return data.get("bars", [])

    def get_candles(self, asset, tf_seconds, count, now=None):
        try:
            symbol = self._map_asset(asset)
            interval = tf_to_biquote(tf_seconds)
            bars = self._fetch_bars(symbol, interval, count)
            if not bars:
                return pd.DataFrame(columns=COLS)

            df = pd.DataFrame(bars)
            # biquote bars: openTime, open, high, low, close (newest-first)
            if "openTime" in df.columns:
                df.index = pd.to_datetime(df["openTime"], utc=True)
            elif "time" in df.columns:
                df.index = pd.to_datetime(df["time"], utc=True)
            else:
                return pd.DataFrame(columns=COLS)

            if "volume" not in df.columns:
                df["volume"] = 0.0
            df = df[COLS].astype(float)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
            df = drop_unclosed(df, tf_seconds, now or utcnow())
            return df.iloc[-count:]
        except Exception as e:
            print(f"[BiQuoteSource] get_candles failed for {asset}: {e}")
            return pd.DataFrame(columns=COLS)

    def get_candles_peek(self, asset, tf_seconds, count, now=None):
        """Like get_candles but INCLUDES the forming candle."""
        try:
            symbol = self._map_asset(asset)
            interval = tf_to_biquote(tf_seconds)
            bars = self._fetch_bars(symbol, interval, count)
            if not bars:
                return pd.DataFrame(columns=COLS)
            df = pd.DataFrame(bars)
            if "openTime" in df.columns:
                df.index = pd.to_datetime(df["openTime"], utc=True)
            elif "time" in df.columns:
                df.index = pd.to_datetime(df["time"], utc=True)
            else:
                return pd.DataFrame(columns=COLS)
            if "volume" not in df.columns:
                df["volume"] = 0.0
            df = df[COLS].astype(float)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
            return df.iloc[-count:]
        except Exception as e:
            print(f"[BiQuoteSource] peek failed for {asset}: {e}")
            return pd.DataFrame(columns=COLS)

    def get_payout(self, asset):
        return None

    def close(self):
        pass


class SyntheticSource(CandleSource):
    def get_candles(self, asset, tf_seconds, count, now=None):
        return pd.DataFrame(columns=COLS)

    def get_payout(self, asset):
        return 85.0


def make_source(cfg, store=None) -> CandleSource:
    src = (cfg.data_source or "synthetic").lower().strip()
    if src in ("biquote", "yfinance"):
        return BiQuoteSource()
    return SyntheticSource()
