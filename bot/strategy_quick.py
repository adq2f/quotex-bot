"""
strategy_quick.py  --  High-precision 1-min scalping for Quotex OTC.
Filters: session + ADX trend + ATR volatility + 3/5 confluence + HTF bias (H1 & H4)
         + M5 EMA alignment + DI direction + momentum.

HONEST NOTE:
  On 1-minute binary options, no strategy consistently reaches 80-90% win rate.
  This configuration targets 65-75% by REJECTING low-quality setups aggressively.
  Expect fewer signals — quality over quantity.
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
    # ---- Confluence gate ----
    MIN_CONFLUENCE = 3           # 3 of 5 (was 2)

    # ---- Indicator thresholds ----
    RSI_BUY = 35.0
    RSI_SELL = 65.0
    BB_PROXIMITY = 0.10
    STOCH_LOW = 25.0
    STOCH_HIGH = 75.0

    # ---- Quality filters ----
    ADX_MIN = 22.0               # trending market only
    ATR_MIN_RATIO = 0.65         # current ATR >= 65% of 20-period avg
    ATR_MAX_RATIO = 2.5          # and <= 250% (no spike)

    # ---- MTF alignment ----
    REQUIRE_HTF_BIAS = True      # H1 AND H4 must agree
    REQUIRE_M5_ALIGNMENT = True  # M5 EMA20 must agree with signal

    # ---- Session window (UTC) ----
    # Block dead hours (22:00-06:00 UTC). Allow all major forex sessions.
    USE_SESSION_FILTER = True
    ALLOWED_SESSIONS_UTC = [(6, 22)]

    # ---- Momentum & DI ----
    REQUIRE_MOMENTUM = True
    REQUIRE_DI_AGREE = True

    def __init__(self, cfg, news=None):
        self.cfg = cfg
        self.news = news

    # ------------------------------------------------------------------ helpers
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

    def _adx(self, df, n=14):
        """Wilder's ADX with +DI, -DI, ATR."""
        high = df["high"]
        low = df["low"]
        close = df["close"]
        up = high.diff()
        dn = -low.diff()
        plus_dm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=df.index)
        minus_dm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=df.index)
        tr = pd.concat([
            high - low,
            (high - close.shift()).abs(),
            (low - close.shift()).abs(),
        ], axis=1).max(axis=1)
        atr = tr.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
        plus_di = 100 * (plus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / atr.replace(0, np.nan))
        minus_di = 100 * (minus_dm.ewm(alpha=1/n, adjust=False, min_periods=n).mean() / atr.replace(0, np.nan))
        dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di).replace(0, np.nan)
        adx = dx.ewm(alpha=1/n, adjust=False, min_periods=n).mean()
        return adx, plus_di, minus_di, atr

    def _session_ok(self, now):
        if not self.USE_SESSION_FILTER:
            return True
        h = now.hour
        for start, end in self.ALLOWED_SESSIONS_UTC:
            if start <= h < end:
                return True
        return False

    def _htf_bias(self, h1, h4):
        """Both H1 AND H4 must agree. Returns BULLISH / BEARISH / NEUTRAL / UNKNOWN."""
        if len(h1) < 60 or len(h4) < 60:
            return "UNKNOWN"
        h1_e20 = float(self._ema(h1["close"], 20).iloc[-1])
        h1_e50 = float(self._ema(h1["close"], 50).iloc[-1])
        h1_p = float(h1["close"].iloc[-1])
        h4_e20 = float(self._ema(h4["close"], 20).iloc[-1])
        h4_e50 = float(self._ema(h4["close"], 50).iloc[-1])
        h4_p = float(h4["close"].iloc[-1])
        h1_bull = h1_p > h1_e20 > h1_e50
        h1_bear = h1_p < h1_e20 < h1_e50
        h4_bull = h4_p > h4_e20 > h4_e50
        h4_bear = h4_p < h4_e20 < h4_e50
        if h1_bull and h4_bull:
            return "BULLISH"
        if h1_bear and h4_bear:
            return "BEARISH"
        return "NEUTRAL"

    # ------------------------------------------------------------------ main
    def evaluate(self, asset, m5, h1, h4, now):
        d = QuickDecision(asset=asset)
        if len(m5) < 60:
            d.gate = "warmup"
            d.notes = "not enough candles"
            return d

        tf = self.cfg.entry_tf_seconds
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=tf)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        # ---- FILTER 1: Session ----
        if not self._session_ok(now):
            d.gate = "session"
            d.notes = f"outside session (UTC hour {now.hour:02d})"
            return d

        # ---- Compute all indicators ----
        close = m5["close"]
        ema5 = self._ema(close, 5)
        ema10 = self._ema(close, 10)
        ema20 = self._ema(close, 20)
        ema50 = self._ema(close, 50)
        rsi = self._rsi(close, 14)
        _, _, hist = self._macd(close)
        bb_up, bb_mid, bb_lo = self._bb(close, 20, 2.0)
        stoch_k, stoch_d = self._stoch(m5, 14)
        adx_s, plus_di, minus_di, atr_s = self._adx(m5, 14)

        i = len(close) - 1
        c = float(close.iloc[i])
        e5, e10, e20, e50 = (float(ema5.iloc[i]), float(ema10.iloc[i]),
                             float(ema20.iloc[i]), float(ema50.iloc[i]))
        r = float(rsi.iloc[i])
        mh = float(hist.iloc[i])
        bu, bl = float(bb_up.iloc[i]), float(bb_lo.iloc[i])
        sk, sd = float(stoch_k.iloc[i]), float(stoch_d.iloc[i])
        adx_val = float(adx_s.iloc[i])
        pdi = float(plus_di.iloc[i])
        mdi = float(minus_di.iloc[i])

        # ---- FILTER 2: ADX trend strength ----
        if np.isnan(adx_val) or adx_val < self.ADX_MIN:
            d.gate = "low_adx"
            d.notes = f"ADX={adx_val:.1f}<{self.ADX_MIN} (ranging market)"
            return d

        # ---- FILTER 3: ATR volatility ----
        atr_vals = atr_s.dropna()
        if len(atr_vals) >= 20:
            cur_atr = float(atr_vals.iloc[-1])
            avg_atr = float(atr_vals.iloc[-21:-1].mean())
            if avg_atr > 0:
                ratio = cur_atr / avg_atr
                if ratio < self.ATR_MIN_RATIO:
                    d.gate = "low_atr"
                    d.notes = f"ATR ratio={ratio:.2f}<{self.ATR_MIN_RATIO}"
                    return d
                if ratio > self.ATR_MAX_RATIO:
                    d.gate = "atr_spike"
                    d.notes = f"ATR ratio={ratio:.2f}>{self.ATR_MAX_RATIO}"
                    return d

        # ---- FILTER 4: HTF bias (H1 AND H4) ----
        bias = self._htf_bias(h1, h4) if self.REQUIRE_HTF_BIAS else "UNKNOWN"
        if bias == "UNKNOWN":
            d.gate = "htf_warmup"
            d.notes = "HTF data not ready"
            return d
        if self.REQUIRE_HTF_BIAS and bias == "NEUTRAL":
            d.gate = "htf_neutral"
            d.notes = "H1/H4 neutral (no trend)"
            return d

        # ---- 5-signal confluence ----
        band_width = bu - bl
        near_upper = (bu - c) <= band_width * self.BB_PROXIMITY
        near_lower = (c - bl) <= band_width * self.BB_PROXIMITY

        buy = 0
        sell = 0
        if e5 > e10 > e50 and c > e5: buy += 1
        elif e5 < e10 < e50 and c < e5: sell += 1
        if r < self.RSI_BUY: buy += 1
        elif r > self.RSI_SELL: sell += 1
        if mh > 0: buy += 1
        else: sell += 1
        if near_lower: buy += 1
        elif near_upper: sell += 1
        if sk < self.STOCH_LOW and sk > sd: buy += 1
        elif sk > self.STOCH_HIGH and sk < sd: sell += 1

        # ---- FILTER 5: M5 EMA alignment ----
        m5_bull = (c > e20) and (e5 > e20)
        m5_bear = (c < e20) and (e5 < e20)
        m5_ok_buy = m5_bull if self.REQUIRE_M5_ALIGNMENT else True
        m5_ok_sell = m5_bear if self.REQUIRE_M5_ALIGNMENT else True

        # ---- FILTER 6: Momentum ----
        buy_momentum = (e5 > e10) or (mh > 0)
        sell_momentum = (e5 < e10) or (mh < 0)
        mom_ok_buy = buy_momentum if self.REQUIRE_MOMENTUM else True
        mom_ok_sell = sell_momentum if self.REQUIRE_MOMENTUM else True

        # ---- FILTER 7: DI direction ----
        di_buy_ok = (pdi > mdi) if self.REQUIRE_DI_AGREE else True
        di_sell_ok = (mdi > pdi) if self.REQUIRE_DI_AGREE else True

        # ---- FILTER 8: bias must match direction ----
        bias_ok_buy = (bias == "BULLISH") if self.REQUIRE_HTF_BIAS else True
        bias_ok_sell = (bias == "BEARISH") if self.REQUIRE_HTF_BIAS else True

        # ---- Final decision ----
        if (buy >= self.MIN_CONFLUENCE and bias_ok_buy and m5_ok_buy
                and mom_ok_buy and di_buy_ok):
            d.decision = "BUY"
            d.score = 50 + buy * 10
            d.notes = (f"BUY {buy}/5 ADX={adx_val:.0f} bias={bias} "
                       f"RSI={r:.0f} MACD={mh:+.5f} Stoch={sk:.0f}/{sd:.0f}")
            return d

        if (sell >= self.MIN_CONFLUENCE and bias_ok_sell and m5_ok_sell
                and mom_ok_sell and di_sell_ok):
            d.decision = "SELL"
            d.score = 50 + sell * 10
            d.notes = (f"SELL {sell}/5 ADX={adx_val:.0f} bias={bias} "
                       f"RSI={r:.0f} MACD={mh:+.5f} Stoch={sk:.0f}/{sd:.0f}")
            return d

        d.gate = "filtered"
        d.notes = (f"buy={buy} sell={sell} bias={bias} ADX={adx_val:.0f} "
                   f"m5B={m5_ok_buy} m5S={m5_ok_sell} "
                   f"momB={mom_ok_buy} momS={mom_ok_sell} "
                   f"diB={di_buy_ok} diS={di_sell_ok}")
        return d
