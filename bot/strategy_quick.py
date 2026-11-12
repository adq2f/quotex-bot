"""
strategy_quick.py  --  1-minute scalping strategy for Quotex OTC.
Relaxed: 2 of 5 confluence instead of 3 → more frequent signals.
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
    # ---- Tunables ----
    MIN_CONFLUENCE = 2           # 2 of 5 (was 3) → more signals
    RSI_BUY = 40.0               # was 30 (relaxed)
    RSI_SELL = 60.0              # was 70 (relaxed)
    BB_PROXIMITY = 0.15          # within 15% of band width counts as "touch"

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
        bu, bm, bl = float(bb_up.iloc[i]), float(bb_mid.iloc[i]), float(bb_lo.iloc[i])
        sk, sd = float(stoch_k.iloc[i]), float(stoch_d.iloc[i])

        band_width = bu - bl
        near_upper = (bu - c) <= band_width * self.BB_PROXIMITY
        near_lower = (c - bl) <= band_width * self.BB_PROXIMITY

        buy = 0
        sell = 0

        # 1. EMA alignment
        if e5 > e10 > e50 and c > e5: buy += 1
        elif e5 < e10 < e50 and c < e5: sell += 1

        # 2. RSI (relaxed thresholds)
        if r < self.RSI_BUY: buy += 1
        elif r > self.RSI_SELL: sell += 1

        # 3. MACD histogram
        if mh > 0: buy += 1
        else: sell += 1

        # 4. Bollinger (proximity instead of exact touch)
        if near_lower: buy += 1
        elif near_upper: sell += 1

        # 5. Stochastic cross (relaxed)
        if sk < 30 and sk > sd: buy += 1
        elif sk > 70 and sk < sd: sell += 1

        if buy >= self.MIN_CONFLUENCE:
            d.decision = "BUY"
            d.score = 50 + buy * 10
            d.notes = f"BUY {buy}/5 | RSI={r:.0f} MACD={mh:+.5f} Stoch={sk:.0f}/{sd:.0f} BB={'lo' if near_lower else 'mid'}"
        elif sell >= self.MIN_CONFLUENCE:
            d.decision = "SELL"
            d.score = 50 + sell * 10
            d.notes = f"SELL {sell}/5 | RSI={r:.0f} MACD={mh:+.5f} Stoch={sk:.0f}/{sd:.0f} BB={'hi' if near_upper else 'mid'}"
        else:
            d.gate = "no_confluence"
            d.notes = f"buy={buy} sell={sell} RSI={r:.0f} MACD={mh:+.5f} Stoch={sk:.0f}/{sd:.0f}"
        return d
