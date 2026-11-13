"""
strategy_quick.py  --  Ultra Aggressive 1-Minute Scalper (Guarantees Signals in Flat Market)
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

        # ==========================================
        # ১. মূল স্ট্র্যাটেজি: EMA + RSI (কন্ডিশন অনেক শিথিল করা হলো)
        # ==========================================
        # আপ ট্রেন্ড + সবুজ ক্যান্ডেল + RSI ৪০ এর উপরে
        if e9 > e21 and is_green and r > 40:
            d.decision = "BUY"
            d.score = 80
            d.notes = f"Trend BUY (RSI {r:.0f})"
            return d
            
        # ডাউন ট্রেন্ড + লাল ক্যান্ডেল + RSI ৬০ এর নিচে
        if e9 < e21 and is_red and r < 60:
            d.decision = "SELL"
            d.score = 80
            d.notes = f"Trend SELL (RSI {r:.0f})"
            return d

        # ==========================================
        # ২. ফলব্যাক স্ট্র্যাটেজি (ফ্ল্যাট মার্কেটের জন্য)
        # ==========================================
        # যদি ট্রেন্ড একদম ফ্ল্যাট থাকে, শুধু RSI আর ক্যান্ডেলের রঙ দেখে ট্রেড নেবে
        if r < 35 and is_green:
            d.decision = "BUY"
            d.score = 75
            d.notes = f"Flat Market BUY (RSI {r:.0f})"
            return d
            
        if r > 65 and is_red:
            d.decision = "SELL"
            d.score = 75
            d.notes = f"Flat Market SELL (RSI {r:.0f})"
            return d

        # ৩. একদম শেষ চেষ্টা (যদি RSI ৩৫-৬৫ এর মধ্যে থাকে)
        # ক্রমাগত ৩টি সবুজ বা ৩টি লাল ক্যান্ডেল হলে ট্রেড নেবে
        if is_green and float(m5["close"].iloc[i-1]) > float(m5["open"].iloc[i-1]) and float(m5["close"].iloc[i-2]) > float(m5["open"].iloc[i-2]):
            d.decision = "BUY"
            d.score = 70
            d.notes = "3 Green Candles Momentum"
            return d
            
        if is_red and float(m5["close"].iloc[i-1]) < float(m5["open"].iloc[i-1]) and float(m5["close"].iloc[i-2]) < float(m5["open"].iloc[i-2]):
            d.decision = "SELL"
            d.score = 70
            d.notes = "3 Red Candles Momentum"
            return d

        d.gate = "filtered"
        return d
