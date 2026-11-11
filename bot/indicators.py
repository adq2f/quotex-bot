"""
indicators.py  --  Rulebook Section 3 (+ helpers used by Sections 2, 4, 7, 8)
Vectorized pandas/numpy. All functions expect CLOSED candles only (3.5);
the data feed guarantees this by dropping the forming candle.
DataFrame columns: open, high, low, close, (volume optional). Index = candle OPEN time, UTC.
"""
from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np
import pandas as pd


def sma(s: pd.Series, n: int) -> pd.Series:
    return s.rolling(n).mean()


def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(close: pd.Series, n: int = 14) -> pd.Series:
    d = close.diff()
    up = d.clip(lower=0.0)
    dn = (-d.clip(upper=0.0))
    au = up.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    ad = dn.ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
    rs = au / ad.replace(0.0, np.nan)
    out = 100.0 - 100.0 / (1.0 + rs)
    out = out.where(~((ad == 0) & (au > 0)), 100.0)
    return out


def macd(close: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    line = ema(close, fast) - ema(close, slow)
    sig = line.ewm(span=signal, adjust=False).mean()
    return pd.DataFrame({"macd": line, "signal": sig, "hist": line - sig})


def bollinger(close: pd.Series, n: int = 20, k: float = 2.0) -> pd.DataFrame:
    mid = close.rolling(n).mean()
    sd = close.rolling(n).std(ddof=0)
    return pd.DataFrame({"mid": mid, "upper": mid + k * sd, "lower": mid - k * sd})


def true_range(df: pd.DataFrame) -> pd.Series:
    pc = df["close"].shift(1)
    return pd.concat([df["high"] - df["low"], (df["high"] - pc).abs(), (df["low"] - pc).abs()], axis=1).max(axis=1)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    return true_range(df).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()


def volume_series(df: pd.DataFrame) -> pd.Series:
    """Real volume if the feed provides it; otherwise candle range as a proxy.
    (Quotex OTC has no real volume -> rulebook 15.5.)"""
    if "volume" in df.columns and float(df["volume"].fillna(0).sum()) > 0:
        return df["volume"].astype(float)
    return (df["high"] - df["low"]).astype(float)


def volume_ratio_at(df: pd.DataFrame, idx: int, n: int = 20) -> float:
    """Volume (or proxy) of candle idx divided by the mean of the n candles BEFORE it."""
    v = volume_series(df).values
    if idx < 1:
        return 0.0
    lo = max(0, idx - n)
    base = v[lo:idx]
    if len(base) == 0 or np.nanmean(base) == 0:
        return 0.0
    return float(v[idx] / np.nanmean(base))


def divergence(df: pd.DataFrame, direction: str, rsi_s: pd.Series, lookback: int = 12) -> bool:
    """Simple regular divergence on the last `lookback` candles (3.1: +1 confluence)."""
    if len(df) < lookback + 2:
        return False
    w = df.iloc[-lookback:]
    r = rsi_s.iloc[-lookback:]
    half = lookback // 2
    if direction == "BUY":
        p1, p2 = w["low"].iloc[:half].min(), w["low"].iloc[half:].min()
        r1, r2 = r.iloc[:half].min(), r.iloc[half:].min()
        return bool(p2 < p1 and r2 > r1)
    p1, p2 = w["high"].iloc[:half].max(), w["high"].iloc[half:].max()
    r1, r2 = r.iloc[:half].max(), r.iloc[half:].max()
    return bool(p2 > p1 and r2 < r1)


@dataclass
class Confluence:
    ok: bool
    rsi_value: float
    rsi_ok: bool
    rsi_strict: bool
    macd_ok: bool
    macd_text: str
    bb_ok: bool
    bb_text: str
    divergence: bool
    detail: Dict[str, float]


def indicator_confluence(df: pd.DataFrame, direction: str, cfg) -> Optional[Confluence]:
    """Section 3: RSI + MACD + Bollinger must ALL agree (3.4).
    window = cfg.indicator_window. 1 = every condition read on the last closed candle (strict)."""
    if len(df) < max(cfg.macd_slow + cfg.macd_signal + 5, cfg.bb_period + 5, cfg.rsi_period + 5):
        return None
    w = cfg.indicator_window
    close = df["close"]
    r = rsi(close, cfg.rsi_period)
    m = macd(close, cfg.macd_fast, cfg.macd_slow, cfg.macd_signal)
    b = bollinger(close, cfg.bb_period, cfg.bb_std)

    r_w = r.iloc[-w:]
    if direction == "BUY":
        rsi_ok = bool((r_w < cfg.rsi_buy_max).any())
        rsi_strict = bool((r_w < cfg.rsi_buy_strict).any())
        rsi_val = float(r_w.min())
        crossed = (m["macd"] > m["signal"]) & (m["macd"].shift(1) <= m["signal"].shift(1))
        hist_ok = bool(m["hist"].iloc[-1] > 0)
        macd_ok = bool(crossed.iloc[-w:].any()) and hist_ok
        macd_text = "Bullish Cross" if macd_ok else "No bullish cross"
        bb_ok = bool((df["low"].iloc[-w:] <= b["lower"].iloc[-w:]).any())
        bb_text = "Lower Band" if bb_ok else "Not at lower band"
    else:
        rsi_ok = bool((r_w > cfg.rsi_sell_min).any())
        rsi_strict = bool((r_w > cfg.rsi_sell_strict).any())
        rsi_val = float(r_w.max())
        crossed = (m["macd"] < m["signal"]) & (m["macd"].shift(1) >= m["signal"].shift(1))
        hist_ok = bool(m["hist"].iloc[-1] < 0)
        macd_ok = bool(crossed.iloc[-w:].any()) and hist_ok
        macd_text = "Bearish Cross" if macd_ok else "No bearish cross"
        bb_ok = bool((df["high"].iloc[-w:] >= b["upper"].iloc[-w:]).any())
        bb_text = "Upper Band" if bb_ok else "Not at upper band"

    div = divergence(df, direction, r)
    return Confluence(
        ok=bool(rsi_ok and macd_ok and bb_ok),
        rsi_value=rsi_val, rsi_ok=rsi_ok, rsi_strict=rsi_strict,
        macd_ok=macd_ok, macd_text=macd_text, bb_ok=bb_ok, bb_text=bb_text,
        divergence=div,
        detail={"rsi": float(r.iloc[-1]), "macd": float(m["macd"].iloc[-1]),
                "signal": float(m["signal"].iloc[-1]), "hist": float(m["hist"].iloc[-1])},
    )
