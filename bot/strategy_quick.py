"""
strategy_quick.py  --  1-minute scalping with HTF bias + momentum filter.
Fixes counter-trend BUY in downtrend (the source of the losing streak).
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
    MIN_CONFLUENCE = 2           # 2 of 5
    RSI_BUY = 35.0               # tightened from 40
    RSI_SELL = 65.0              # tightened from 60
    BB_PROXIMITY = 0.10          # tightened from 0.15
    REQUIRE_MOMENTUM = True      # EMA or MACD must agree
    USE_HTF_BIAS = True          # H4 EMA20/EMA50 direction filter

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

    def _stoch(self, df, n=14):
        lo = df["low"].rolling(n).min()
        hi = df["high"].rolling(n).max()
        k = 100 * (df["close"] - lo) / (hi - lo).replace(0, np.nan)
        d = k.rolling(3).mean()
        return k, d

    def _htf_bias(self, h1, h4):
        """BULLISH / BEARISH / NEUTRAL from H4 EMA20 vs EMA50 and price."""
        if len(h4) < 60:
            return "NEUTRAL"
        e20 = float(self._ema(h4["close"], 20).iloc[-1])
        e50 = float(self._ema(h4["close"], 50).iloc[-1])
        price = float(h4["close"].iloc[-1])
        if price > e20 > e50:
            return "BULLISH"
        if price < e20 < e50:
            return "BEARISH"
        return "NEUTRAL"

    def evaluate(self, asset, m5, h1, h4, now):
        d = QuickDecision(asset=asset)
        if len(m5) < 60:
            d.notes = "warm-up"
            return d

        tf = self.cfg.entry_tf_seconds
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=tf)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        close = m5["close"]
        ema5 = self._ema(close, 5)
        ema10 = self._ema(close, 10)
        ema50 = self._ema(close, 50)
        rsi = self._rsi(close, 14)
        _, _, hist = self._macd(close)
        bb_up, bb_mid, bb_lo = self._bb(close, 20, 2.0)
        stoch_k, stoch_d = self._stoch(m5, 14)

        i = len(close) - 1
        c = float(close.iloc[i])
        e5, e10, e50 = float(ema5.iloc[i]), float(ema10.iloc[i]), float(ema50.iloc[i])
        r = float(rsi.iloc[i])
        mh = float(hist.iloc[i])
        bu, bl = float(bb_up.iloc[i]), float(bb_lo.iloc[i])
        sk, sd = float(stoch_k.iloc[i]), float(stoch_d.iloc[i])

        band_width = bu - bl
        near_upper = (bu - c) <= band_width * self.BB_PROXIMITY
        near_lower = (c - bl) <= band_width * self.BB_PROXIMITY

        buy = 0
        sell = 0

        # 1. EMA alignment
        if e5 > e10 > e50 and c > e5: buy += 1
        elif e5 < e10 < e50 and c < e5: sell += 1

        # 2. RSI
        if r < self.RSI_BUY: buy += 1
        elif r > self.RSI_SELL: sell += 1

        # 3. MACD histogram
        if mh > 0: buy += 1
        else: sell += 1

        # 4. Bollinger
        if near_lower: buy += 1
        elif near_upper: sell += 1

        # 5. Stochastic cross
        if sk < 30 and sk > sd: buy += 1
        elif sk > 70 and sk < sd: sell += 1

        # ---- Filters ----
        bias = self._htf_bias(h1, h4) if self.USE_HTF_BIAS else "NEUTRAL"
        buy_momentum = (e5 > e10) or (mh > 0)
        sell_momentum = (e5 < e10) or (mh < 0)

        bias_ok_buy = bias in ("BULLISH", "NEUTRAL")
        bias_ok_sell = bias in ("BEARISH", "NEUTRAL")
        momentum_ok_buy = buy_momentum if self.REQUIRE_MOMENTUM else True
        momentum_ok_sell = sell_momentum if self.REQUIRE_MOMENTUM else True

        if buy >= self.MIN_CONFLUENCE and bias_ok_buy and momentum_ok_buy:
            d.decision = "BUY"
            d.score = 50 + buy * 10
            d.notes = f"BUY {buy}/5 bias={bias} RSI={r:.0f} MACD={mh:+.5f} Stoch={sk:.0f}/{sd:.0f}"
        elif sell >= self.MIN_CONFLUENCE and bias_ok_sell and momentum_ok_sell:
            d.decision = "SELL"
            d.score = 50 + sell * 10
            d.notes = f"SELL {sell}/5 bias={bias} RSI={r:.0f} MACD={mh:+.5f} Stoch={sk:.0f}/{sd:.0f}"
        else:
            d.gate = "filtered"
            d.notes = f"buy={buy} sell={sell} bias={bias} momB={buy_momentum} momS={sell_momentum}"
        return d
