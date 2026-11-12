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

# Monkey-patch for pyquotex compatibility with newer curl_cffi
try:
    from curl_cffi.requests.models import Response
    if not hasattr(Response, 'reason_phrase'):
        Response.reason_phrase = property(lambda self: getattr(self, 'status_code', 'Unknown'))
except Exception:
    pass

COLS = ["open", "high", "low", "close", "volume"]


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def drop_unclosed(df: pd.DataFrame, tf_seconds: int, now: datetime) -> pd.DataFrame:
    """Remove the still-forming candle: a candle is closed once open_time + tf <= now."""
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


# --------------------------------------------------------------------------- synthetic
class SyntheticSource(CandleSource):
    """Deterministic pseudo-market so the whole pipeline can be exercised without network."""

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


# --------------------------------------------------------------------------- csv replay
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


# --------------------------------------------------------------------------- quotex (unofficial)
class QuotexSource(CandleSource):
    """Adapter around an unofficial async Quotex client (e.g. `pyquotex`)."""

    def __init__(self, email: str, password: str, demo: bool = True,
                 proxy: str = None, cookie: str = None):
        try:
            from pyquotex.stable_api import Quotex  # type: ignore
        except Exception as e:
            raise RuntimeError("Install pyquotex to use DATA_SOURCE=quotex") from e

        self._email, self._password, self._proxy = email, password, proxy
        self._loop = asyncio.new_event_loop()
        self._t = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._t.start()
        self.demo = demo

        self.client = self._make_client(cookie)
        ok, reason = self._run(self.client.connect())
        if not ok:
            raise RuntimeError(f"Quotex connect failed: {reason}")
        self.client.change_account("PRACTICE" if demo else "REAL")

    def _make_client(self, cookie: str):
        from pyquotex.stable_api import Quotex  # type: ignore
        if self._proxy:
            print(f"[QuotexSource] Using proxy: {self._proxy}")
            self.client = Quotex(email=self._email, password=self._password,
                                 proxies={"http": self._proxy, "https": self._proxy})
        else:
            self.client = Quotex(email=self._email, password=self._password)
        cookie_str = cookie if cookie is not None else os.getenv("QUOTEX_COOKIE", "")
        if cookie_str:
            print("[QuotexSource] Applying cookie...")
            try:
                self.client.set_session(
                    user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/120.0.0.0 Safari/537.36",
                    cookies=cookie_str,
                )
            except Exception as e:
                print(f"[QuotexSource] Cookie apply failed: {e}")
        return self.client

    def reconnect(self, cookie: str = None):
        """Reconnect with a new cookie (called from web UI when user updates it)."""
        try:
            self._run(self.client.close())
        except Exception:
            pass
        self.client = self._make_client(cookie)
        ok, reason = self._run(self.client.connect())
        if not ok:
            raise RuntimeError(f"Quotex reconnect failed: {reason}")
        self.client.change_account("PRACTICE" if self.demo else "REAL")
        print("[QuotexSource] Reconnected successfully")

    def _run(self, coro, timeout=20):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def get_candles(self, asset, tf_seconds, count, now=None):
        now = now or utcnow()
        end = int(now.timestamp())
        raw = self._run(self.client.get_candles(asset, end, tf_seconds * count, tf_seconds)) or []
        if not raw:
            return pd.DataFrame(columns=COLS)
        df = pd.DataFrame(raw)
        df.index = pd.to_datetime(df["time"], unit="s", utc=True)
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = df[COLS].astype(float).sort_index()
        df = df[~df.index.duplicated(keep="last")]
        return drop_unclosed(df, tf_seconds, now).iloc[-count:]

    def get_payout(self, asset):
        try:
            p = self._run(self.client.get_payment())
            v = p.get(asset, {}).get("payment") or p.get(asset, {}).get("turbo_payment")
            return float(v) if v is not None else None
        except Exception:
            return None

    def get_balance(self):
        try:
            return float(self._run(self.client.get_balance()))
        except Exception:
            return None

    def get_all_payouts(self) -> dict:
        """Return {asset: payout_pct} for all available assets."""
        try:
            p = self._run(self.client.get_payment()) or {}
            out = {}
            for asset, info in p.items():
                v = info.get("payment") or info.get("turbo_payment")
                if v is not None:
                    try:
                        out[asset] = float(v)
                    except Exception:
                        pass
            return out
        except Exception:
            return {}

    def buy(self, asset, direction, stake, expiry_seconds):
        status, _info = self._run(self.client.buy(stake, asset, direction.lower(), expiry_seconds))
        return bool(status)

    def close(self):
        try:
            self._run(self.client.close())
        except Exception:
            pass


def make_source(cfg, store=None) -> CandleSource:
    if cfg.data_source == "quotex":
        proxy = os.getenv("QUOTEX_PROXY", None)
        cookie = (store.kv_get("quotex_cookie") if store else None) or os.getenv("QUOTEX_COOKIE", "")
        return QuotexSource(cfg.quotex_email, cfg.quotex_password,
                            demo=(cfg.account_type == "DEMO"),
                            proxy=proxy, cookie=cookie)
    if cfg.data_source == "csv":
        return CSVSource(cfg.csv_dir)
    return SyntheticSource()
