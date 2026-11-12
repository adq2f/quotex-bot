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

# ---- biquote import ----
try:
    import biquote as bq
except Exception:
    bq = None

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
        return self.get_candles(asset, tf_seconds, count, now)


class BiQuoteSource(CandleSource):
    """Forex/CFD OHLC data via biquote (no API key, MT5 feed)."""

    # Map Quotex OTC symbols to biquote symbols
    SYMBOL_MAP = {
        "EURUSD": "EURUSD", "GBPUSD": "GBPUSD", "USDJPY": "USDJPY",
        "AUDUSD": "AUDUSD", "USDCAD": "USDCAD", "USDCHF": "USDCHF",
        "NZDUSD": "NZDUSD", "EURGBP": "EURGBP", "EURJPY": "EURJPY",
        "GBPJPY": "GBPJPY", "XAUUSD": "XAUUSD", "BTCUSD": "BTCUSD",
        "ETHUSD": "ETHUSD",
    }

    def __init__(self):
        if bq is None:
            raise RuntimeError("biquote not installed. Run: pip install biquote")
        print("[BiQuoteSource] Using biquote (MT5 feed, no API key)")

    @classmethod
    def _map_asset(cls, asset: str) -> str:
        base = asset.lower().replace("_otc", "").replace("/", "").upper()
        return cls.SYMBOL_MAP.get(base, base)

    def get_candles(self, asset, tf_seconds, count, now=None):
        try:
            symbol = self._map_asset(asset)
            interval = tf_rule(tf_seconds).replace("1min", "1m").replace("5min", "5m").replace("15min", "15m").replace("1h", "1h").replace("4h", "4h")
            limit = min(count, 1000)  # biquote max ~1000 bars
            bars = bq.ohlc(symbol, interval=interval, limit=limit)
            if not bars:
                return pd.DataFrame(columns=COLS)
            df = pd.DataFrame(bars)
            # biquote returns list of dicts with keys: time, open, high, low, close, tickVolume
            if "time" in df.columns:
                df.index = pd.to_datetime(df["time"], utc=True)
            if "tickVolume" in df.columns:
                df["volume"] = df["tickVolume"].astype(float)
            else:
                df["volume"] = 0.0
            df = df[["open", "high", "low", "close", "volume"]].astype(float)
            df = df.sort_index()
            df = df[~df.index.duplicated(keep="last")]
            df = drop_unclosed(df, tf_seconds, now or utcnow())
            return df.iloc[-count:]
        except Exception as e:
            print(f"[BiQuoteSource] get_candles failed for {asset}: {e}")
            return pd.DataFrame(columns=COLS)

    def get_payout(self, asset):
        return None

    def close(self):
        pass


# Optional sources for future use (not active when DATA_SOURCE=biquote)
class SyntheticSource(CandleSource):
    def get_candles(self, asset, tf_seconds, count, now=None):
        return pd.DataFrame(columns=COLS)

    def get_payout(self, asset):
        return 85.0


def make_source(cfg, store=None) -> CandleSource:
    src = (cfg.data_source or "synthetic").lower().strip()
    if src == "biquote":
        return BiQuoteSource()
    return SyntheticSource()
