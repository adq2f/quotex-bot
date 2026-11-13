"""
strategy_quick.py  --  Universal Best Strategy (Multi-Timeframe High Accuracy)
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
import pandas as pd
import numpy as np

@dataclass
class QuickDecision:
    asset: str
    decision: str = "HOLD"
    score: int = 0
    entry_price: float = 0.0
    candle_close: Optional[datetime] = None
    notes: str = ""
    gate: str = ""

class QuickStrategy:
    def __init__(self, cfg, news=None):
        self.cfg = cfg
        self.news = news

    def _ema(self, s, n): return s.ewm(span=n, adjust=False).mean()
    def _rsi(self, s, n=14):
        d = s.diff()
        up = d.clip(lower=0.0).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
        dn = (-d.clip(upper=0.0)).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
        rs = up / dn.replace(0, np.nan)
        return 100 - 100 / (1 + rs)
    def _macd(self, s):
        line = self._ema(s, 12) - self._ema(s, 26)
        sig = self._ema(line, 9)
        return line, sig, line - sig

    def evaluate(self, asset, m5, h1, h4, now):
        d = QuickDecision(asset=asset)
        if len(m5) < 30: 
            d.gate = "warmup"
            return d

        tf = self.cfg.entry_tf_seconds
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=tf)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        close = m5["close"]
        ema9 = self._ema(close, 9)
        ema21 = self._ema(close, 21)
        rsi = self._rsi(close, 14)
        _, _, macd_hist = self._macd(close)

        i = len(close) - 1
        c = float(close.iloc[i])
        o = float(m5["open"].iloc[i])
        e9 = float(ema9.iloc[i])
        e21 = float(ema21.iloc[i])
        r = float(rsi.iloc[i])
        mh = float(macd_hist.iloc[i])

        is_green = c > o
        is_red = c < o

        # BUY: EMA9 > EMA21 + MACD > 0 + RSI < 55 + Green Candle
        if e9 > e21 and mh > 0 and r < 55 and is_green:
            d.decision = "BUY"
            d.score = 85
            d.notes = f"Trend BUY (RSI {r:.0f})"
            return d
            
        # SELL: EMA9 < EMA21 + MACD < 0 + RSI > 45 + Red Candle
        if e9 < e21 and mh < 0 and r > 45 and is_red:
            d.decision = "SELL"
            d.score = 85
            d.notes = f"Trend SELL (RSI {r:.0f})"
            return d

        d.gate = "filtered"
        return d
