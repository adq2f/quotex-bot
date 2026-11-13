"""
strategy_quick.py  --  Multi-Strategy Engine with Market Context Analysis.
Determines if market is Strong Up, Normal Up, Stable, Normal Down, or Strong Down.
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
    market_context: str = ""
    strategy_used: str = ""

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

    def _bb(self, s, n=20, k=2.0):
        mid = s.rolling(n).mean()
        sd = s.rolling(n).std(ddof=0)
        return mid + k * sd, mid, mid - k * sd
        
    def _atr(self, df, n=14):
        tr = pd.concat([df["high"] - df["low"], (df["high"] - df["close"].shift()).abs(), (df["low"] - df["close"].shift()).abs()], axis=1).max(axis=1)
        return tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()

    def _get_market_context(self, df, ema20, ema50, ema200, adx):
        c = df["close"].iloc[-1]
        e20, e50, e200 = ema20.iloc[-1], ema50.iloc[-1], ema200.iloc[-1]
        adx_val = adx.iloc[-1]
        
        dist = abs(c - e20) / e20
        
        if adx_val < 20:
            return "STABLE"
        if c > e20 > e50 > e200:
            return "STRONG_UP" if dist > 0.0005 else "NORMAL_UP"
        if c < e20 < e50 < e200:
            return "STRONG_DOWN" if dist > 0.0005 else "NORMAL_DOWN"
        if c > e20 > e50:
            return "NORMAL_UP"
        if c < e20 < e50:
            return "NORMAL_DOWN"
        return "STABLE"

    def evaluate(self, asset, m5, h1, h4, now):
        d = QuickDecision(asset=asset)
        if len(m5) < 100: 
            d.gate = "warmup"
            return d

        tf = self.cfg.entry_tf_seconds
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=tf)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        close = m5["close"]
        ema20 = self._ema(close, 20)
        ema50 = self._ema(close, 50)
        ema200 = self._ema(close, 200)
        rsi = self._rsi(close, 14)
        _, _, hist = self._macd(close)
        bb_up, bb_mid, bb_lo = self._bb(close, 20, 2.0)
        
        # ADX
        high, low = m5["high"], m5["low"]
        up, dn = high.diff(), -low.diff()
        plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=m5.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=m5.index)
        atr = self._atr(m5, 14)
        plus_di = 100 * (plus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan))
        minus_di = 100 * (minus_dm.ewm(alpha=1/14, adjust=False).mean() / atr.replace(0, np.nan))
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1/14, adjust=False).mean()

        i = len(close) - 1
        c = float(close.iloc[i])
        r = float(rsi.iloc[i])
        mh = float(hist.iloc[i])
        bu, bl = float(bb_up.iloc[i]), float(bb_lo.iloc[i])
        adx_val = float(adx.iloc[i])
        pdi, mdi = float(plus_di.iloc[i]), float(minus_di.iloc[i])

        # মার্কেট অ্যানালাইসিস
        context = self._get_market_context(m5, ema20, ema50, ema200, adx)
        d.market_context = context

        # --- স্ট্র্যাটেজি ১: Bollinger Band Bounce (Stable Market) ---
        if context == "STABLE":
            d.strategy_used = "Bollinger Bounce"
            if c <= bl and r < 30 and c > float(m5["open"].iloc[i]):
                d.decision = "BUY"
                d.score = 75 + (30 - r)
                d.notes = f"Stable Market BB Bounce (RSI {r:.0f})"
                return d
            if c >= bu and r > 70 and c < float(m5["open"].iloc[i]):
                d.decision = "SELL"
                d.score = 75 + (r - 70)
                d.notes = f"Stable Market BB Bounce (RSI {r:.0f})"
                return d

        # --- স্ট্র্যাটেজি ২: Trend Pullback (Normal Trend) ---
        elif context in ["NORMAL_UP", "STRONG_UP"]:
            d.strategy_used = "Trend Pullback"
            if c > ema50.iloc[i] and c <= ema20.iloc[i] * 1.002 and r < 45 and mh > float(hist.iloc[i-1]):
                d.decision = "BUY"
                d.score = 80
                d.notes = f"{context} Pullback to EMA20 (RSI {r:.0f})"
                return d
                
        elif context in ["NORMAL_DOWN", "STRONG_DOWN"]:
            d.strategy_used = "Trend Pullback"
            if c < ema50.iloc[i] and c >= ema20.iloc[i] * 0.998 and r > 55 and mh < float(hist.iloc[i-1]):
                d.decision = "SELL"
                d.score = 80
                d.notes = f"{context} Pullback to EMA20 (RSI {r:.0f})"
                return d

        # --- স্ট্র্যাটেজি ৩: Momentum Breakout (Strong Trend) ---
        if context in ["STRONG_UP", "STRONG_DOWN"] and adx_val > 30:
            d.strategy_used = "Momentum Breakout"
            if context == "STRONG_UP" and c > m5["high"].iloc[i-3:i].max() and pdi > mdi:
                d.decision = "BUY"
                d.score = 85
                d.notes = f"STRONG_UP Breakout (ADX {adx_val:.0f})"
                return d
            if context == "STRONG_DOWN" and c < m5["low"].iloc[i-3:i].min() and mdi > pdi:
                d.decision = "SELL"
                d.score = 85
                d.notes = f"STRONG_DOWN Breakout (ADX {adx_val:.0f})"
                return d

        d.gate = "filtered"
        return d
