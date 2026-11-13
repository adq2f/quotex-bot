"""
strategy_quick.py  --  1-Minute High Accuracy Scalper (3-in-1 System)
Combines Breakout, Mean Reversion (BB Bounce), and Trend Pullback.
Always provides a signal if momentum and candle color align.
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

    def _macd(self, s):
        line = self._ema(s, 12) - self._ema(s, 26)
        sig = self._ema(line, 9)
        return line, sig, line - sig

    def _bb(self, s, n=20, k=2.0):
        mid = s.rolling(n).mean()
        sd = s.rolling(n).std(ddof=0)
        return mid + k * sd, mid, mid - k * sd

    def _make_buy(self, d, score, reason):
        d.decision = "BUY"
        d.score = score
        d.notes = reason
        return d

    def _make_sell(self, d, score, reason):
        d.decision = "SELL"
        d.score = score
        d.notes = reason
        return d

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
        bb_up, bb_mid, bb_lo = self._bb(close, 20, 2.0)

        i = len(close) - 1
        c = float(close.iloc[i])
        o = float(m5["open"].iloc[i])
        
        e9 = float(ema9.iloc[i])
        e21 = float(ema21.iloc[i])
        r = float(rsi.iloc[i])
        mh = float(macd_hist.iloc[i])
        bu = float(bb_up.iloc[i])
        bl = float(bb_lo.iloc[i])

        is_green = c > o
        is_red = c < o

        # ==========================================
        # 1. BREAKOUT STRATEGY (সবচেয়ে শক্তিশালী সিগন্যাল)
        # ==========================================
        # আগের ৩টি ক্যান্ডেলের হাই/লো ব্রেক করলে
        prev_high = float(m5["high"].iloc[i-3:i].max())
        prev_low = float(m5["low"].iloc[i-3:i].min())
        
        # ব্রেকআউট BUY: দাম আগের ৩ ক্যান্ডেলের উপরে গেল, ক্যান্ডেল সবুজ এবং RSI ৫০+
        if c > prev_high and is_green and r > 50 and mh > 0:
            return self._make_buy(d, 85, f"3-Candle High Breakout (RSI {r:.0f})")
            
        # ব্রেকআউট SELL: দাম আগের ৩ ক্যান্ডেলের নিচে গেল, ক্যান্ডেল লাল এবং RSI ৫০-
        if c < prev_low and is_red and r < 50 and mh < 0:
            return self._make_sell(d, 85, f"3-Candle Low Breakout (RSI {r:.0f})")

        # ==========================================
        # 2. MEAN REVERSION (যখন মার্কেট সাইডওয়ে থাকে)
        # ==========================================
        # বোলিঙ্গার ব্যান্ডের নিচে গিয়ে সবুজ ক্যান্ডেল হলে BUY
        if c <= bl and r < 35 and is_green:
            return self._make_buy(d, 80, f"BB Lower Bounce (RSI {r:.0f})")
            
        # বোলিঙ্গার ব্যান্ডের উপরে গিয়ে লাল ক্যান্ডেল হলে SELL
        if c >= bu and r > 65 and is_red:
            return self._make_sell(d, 80, f"BB Upper Rejection (RSI {r:.0f})")

        # ==========================================
        # 3. TREND PULLBACK (যখন হালকা ট্রেন্ড থাকে)
        # ==========================================
        # আপট্রেন্ড + দাম EMA9-এর নিচে + সবুজ ক্যান্ডেল
        if e9 > e21 and c < e9 and r < 45 and is_green:
            return self._make_buy(d, 75, f"EMA Pullback BUY (RSI {r:.0f})")
            
        # ডাউনট্রেন্ড + দাম EMA9-এর উপরে + লাল ক্যান্ডেল
        if e9 < e21 and c > e9 and r > 55 and is_red:
            return self._make_sell(d, 75, f"EMA Pullback SELL (RSI {r:.0f})")

        # এর বাইরে কোনো শর্ত মিললে সিগন্যাল দেবে না, অপেক্ষা করবে।
        d.gate = "filtered"
        return d
