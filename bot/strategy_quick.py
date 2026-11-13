"""
strategy_quick.py  --  Strict 1M trend strategy.

v2.2 rewrite: the previous version fired on any EMA9>EMA21 with RSI<55, which is
basically noise. New rules require ALL of:
  * EMA9 > EMA21 > EMA50  (real uptrend, not a one-candle blip)
  * EMA9/EMA21 separation > 0.02%  (filter for flat/ranging)
  * MACD line > signal AND histogram > 0  (momentum confirming)
  * RSI not overbought (<70) and rising  (healthy pullback, not top-buying)
  * Last candle bullish with body >= 50% of range  (no dojis)
SELL mirrors all conditions.

Score = 85..95 for valid signals (all >= HIGH ACCURACY threshold).
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import numpy as np
import pandas as pd


@dataclass
class QuickDecision:
    asset: str
    decision: str = "HOLD"
    score: int = 0
    entry_price: float = 0.0
    candle_close: Optional[datetime] = None
    notes: str = ""
    gate: str = ""


MIN_CANDLES = 60                 # EMA50 needs warm-up
MIN_EMA_SEP_PCT = 0.02           # 0.02% trend separation
MIN_BODY_RATIO = 0.50            # no dojis
RSI_BUY_MAX = 70.0
RSI_SELL_MIN = 30.0


class QuickStrategy:
    def __init__(self, cfg, news=None):
        self.cfg = cfg
        self.news = news

    def _ema(self, s, n):
        return s.ewm(span=n, adjust=False).mean()

    def _rsi(self, s, n=14):
        d = s.diff()
        up = d.clip(lower=0.0).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        dn = (-d.clip(upper=0.0)).ewm(alpha=1 / n, adjust=False, min_periods=n).mean()
        rs = up / dn.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    def _macd(self, s):
        line = self._ema(s, 12) - self._ema(s, 26)
        sig = self._ema(line, 9)
        return line, sig, line - sig

    def evaluate(self, asset, m5, h1, h4, now):
        d = QuickDecision(asset=asset)
        if len(m5) < MIN_CANDLES:
            d.gate = "warmup"
            return d

        tf = self.cfg.entry_tf_seconds
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=tf)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        close = m5["close"]
        ema9 = self._ema(close, 9)
        ema21 = self._ema(close, 21)
        ema50 = self._ema(close, 50)
        rsi = self._rsi(close, 14)
        macd_line, macd_sig, macd_hist = self._macd(close)

        i = len(close) - 1
        c = float(close.iloc[i])
        o = float(m5["open"].iloc[i])
        h = float(m5["high"].iloc[i])
        l = float(m5["low"].iloc[i])
        e9 = float(ema9.iloc[i])
        e21 = float(ema21.iloc[i])
        e50 = float(ema50.iloc[i])
        r = float(rsi.iloc[i])
        r_prev = float(rsi.iloc[i - 1])
        ml = float(macd_line.iloc[i])
        ms = float(macd_sig.iloc[i])
        mh = float(macd_hist.iloc[i])
        mh_prev = float(macd_hist.iloc[i - 1])
        mh_prev2 = float(macd_hist.iloc[i - 2])

        rng = h - l
        if rng <= 0:
            d.gate = "flat_candle"
            return d
        body = abs(c - o)
        body_ratio = body / rng
        is_green = c > o
        is_red = c < o
        ema_sep_pct = abs(e9 - e21) / e21 * 100.0

        # ---------- BUY ----------
        trend_up = e9 > e21 > e50
        sep_ok_up = ema_sep_pct >= MIN_EMA_SEP_PCT and e9 > e21
        macd_up = ml > ms and mh > 0
        rsi_up = r < RSI_BUY_MAX and r > r_prev
        candle_up = is_green and body_ratio >= MIN_BODY_RATIO

        if trend_up and sep_ok_up and macd_up and rsi_up and candle_up:
            d.decision = "BUY"
            score = 85
            if ema_sep_pct > 0.05:
                score += 4
            if body_ratio > 0.65:
                score += 3
            # fresh MACD cross in the last 2 bars
            if mh > 0 and (mh_prev <= 0 or (mh_prev > 0 and mh_prev2 <= 0)):
                score += 3
            d.score = min(95, score)
            d.notes = f"BUY trend (sep {ema_sep_pct:.2f}%, RSI {r:.0f}↑, MACD↑)"
            return d

        # ---------- SELL ----------
        trend_dn = e9 < e21 < e50
        sep_ok_dn = ema_sep_pct >= MIN_EMA_SEP_PCT and e9 < e21
        macd_dn = ml < ms and mh < 0
        rsi_dn = r > RSI_SELL_MIN and r < r_prev
        candle_dn = is_red and body_ratio >= MIN_BODY_RATIO

        if trend_dn and sep_ok_dn and macd_dn and rsi_dn and candle_dn:
            d.decision = "SELL"
            score = 85
            if ema_sep_pct > 0.05:
                score += 4
            if body_ratio > 0.65:
                score += 3
            if mh < 0 and (mh_prev >= 0 or (mh_prev < 0 and mh_prev2 >= 0)):
                score += 3
            d.score = min(95, score)
            d.notes = f"SELL trend (sep {ema_sep_pct:.2f}%, RSI {r:.0f}↓, MACD↓)"
            return d

        d.gate = "filtered"
        return d
