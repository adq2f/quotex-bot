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

    def _get_htf_bias(self, h1, h4):
        if len(h4) < 50 or len(h1) < 50: return "NEUTRAL"
        h4_e20 = self._ema(h4["close"], 20).iloc[-1]
        h4_e50 = self._ema(h4["close"], 50).iloc[-1]
        h4_c = h4["close"].iloc[-1]
        h1_e20 = self._ema(h1["close"], 20).iloc[-1]
        h1_e50 = self._ema(h1["close"], 50).iloc[-1]
        h1_c = h1["close"].iloc[-1]

        if h4_c > h4_e20 > h4_e50 and h1_c > h1_e20 > h1_e50: return "STRONG_BULLISH"
        if h4_c < h4_e20 < h4_e50 and h1_c < h1_e20 < h1_e50: return "STRONG_BEARISH"
        if h4_c > h4_e20 > h4_e50: return "BULLISH"
        if h4_c < h4_e20 < h4_e50: return "BEARISH"
        return "NEUTRAL"

    def evaluate(self, asset, m5, h1, h4, now):
        d = QuickDecision(asset=asset)
        if len(m5) < 20: 
            d.gate = "warmup"
            return d

        tf = self.cfg.entry_tf_seconds
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=tf)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        close = m5["close"]
        ema9 = self._ema(close, 9)
        ema21 = self._ema(close, 21)
        rsi = self._rsi(close, 14)

        i = len(close) - 1
        c = float(close.iloc[i])
        o = float(m5["open"].iloc[i])
        e9 = float(ema9.iloc[i])
        e21 = float(ema21.iloc[i])
        r = float(rsi.iloc[i])

        is_green = c > o
        is_red = c < o
        bias = self._get_htf_bias(h1, h4)

        # ১. High Accuracy (ট্রেন্ডের সাথে মিললে) -> Score 85
        if bias in ["STRONG_BULLISH", "BULLISH"]:
            if r < 55 and is_green and e9 > e21:
                d.decision = "BUY"
                d.score = 85
                d.notes = f"HTF Pullback BUY (RSI {r:.0f})"
                return d
        if bias in ["STRONG_BEARISH", "BEARISH"]:
            if r > 45 and is_red and e9 < e21:
                d.decision = "SELL"
                d.score = 85
                d.notes = f"HTF Pullback SELL (RSI {r:.0f})"
                return d

        # ২. Normal Signal (ফ্ল্যাট মার্কেট) -> Score 70
        if is_green and r > 40:
            d.decision = "BUY"
            d.score = 70
            d.notes = f"Normal Scalp BUY (RSI {r:.0f})"
            return d
        if is_red and r < 60:
            d.decision = "SELL"
            d.score = 70
            d.notes = f"Normal Scalp SELL (RSI {r:.0f})"
            return d

        d.gate = "filtered"
        return d
