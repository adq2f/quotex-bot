"""
strategy_quick.py  --  Aggressive 1-Minute Momentum Scalper (High Frequency)
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

    def _ema(self, s, n): 
        return s.ewm(span=n, adjust=False).mean()
    
    def _rsi(self, s, n=14):
        d = s.diff()
        up = d.clip(lower=0.0).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
        dn = (-d.clip(upper=0.0)).ewm(alpha=1/n, adjust=False, min_periods=n).mean()
        rs = up / dn.replace(0, np.nan)
        return 100 - 100 / (1 + rs)

    def evaluate(self, asset, m5, h1, h4, now):
        d = QuickDecision(asset=asset)
        if len(m5) < 50: 
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

        i = len(close) - 1
        c = float(close.iloc[i])
        o = float(m5["open"].iloc[i])
        
        e9 = float(ema9.iloc[i])
        e21 = float(ema21.iloc[i])
        e50 = float(ema50.iloc[i])
        r = float(rsi.iloc[i])

        is_green = c > o
        is_red = c < o

        # ---------------------------------------------------------
        # BUY Logic: ট্রেন্ড আপ (e9 > e21) এবং RSI ৫০ এর নিচে (পুলব্যাক)
        # ---------------------------------------------------------
        if e9 > e21 and c > e50:
            # যদি RSI ৪৫ এর নিচে থাকে এবং ক্যান্ডেল সবুজ হয়, অথবা RSI ক্রস করে ৫০ এর উপরে উঠছে
            if r < 45 and is_green:
                d.decision = "BUY"
                d.score = 80
                d.notes = f"Momentum BUY (RSI {r:.0f})"
                return d
            if r > 50 and float(rsi.iloc[i-1]) <= 50 and is_green:
                d.decision = "BUY"
                d.score = 85
                d.notes = f"RSI Cross 50 BUY"
                return d

        # ---------------------------------------------------------
        # SELL Logic: ট্রেন্ড ডাউন (e9 < e21) এবং RSI ৫০ এর উপরে (পুলব্যাক)
        # ---------------------------------------------------------
        if e9 < e21 and c < e50:
            if r > 55 and is_red:
                d.decision = "SELL"
                d.score = 80
                d.notes = f"Momentum SELL (RSI {r:.0f})"
                return d
            if r < 50 and float(rsi.iloc[i-1]) >= 50 and is_red:
                d.decision = "SELL"
                d.score = 85
                d.notes = f"RSI Cross 50 SELL"
                return d

        # ---------------------------------------------------------
        # Extreme Oversold/Overbought (যখন মার্কেট হঠাৎ জোরে ওঠে/নামে)
        # ---------------------------------------------------------
        if r < 25 and is_green:
            d.decision = "BUY"
            d.score = 90
            d.notes = f"Oversold Bounce (RSI {r:.0f})"
            return d
            
        if r > 75 and is_red:
            d.decision = "SELL"
            d.score = 90
            d.notes = f"Overbought Drop (RSI {r:.0f})"
            return d

        d.gate = "filtered"
        return d
