"""
strategy_news.py  --  High-impact news volatility breakout.
Fires only within [-30s, +120s] of a high-impact event for the asset's currencies.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd


@dataclass
class NewsDecision:
    asset: str
    decision: str = "HOLD"
    score: int = 0
    entry_price: float = 0.0
    candle_close: Optional[datetime] = None
    notes: str = ""
    gate: str = ""


class NewsStrategy:
    PRE_WINDOW_SEC = 30
    POST_WINDOW_SEC = 120

    def __init__(self, cfg, news):
        self.cfg = cfg
        self.news = news

    def _currencies(self, asset: str):
        base = asset.lower().replace("_otc", "").replace("/", "").upper()
        return [base[:3], base[3:6]] if len(base) >= 6 else [base[:3]]

    def evaluate(self, asset, m5, h1, h4, now):
        d = NewsDecision(asset=asset)
        if self.news is None:
            d.notes = "news disabled"
            return d

        # ensure events loaded
        try:
            self.news._refresh()
        except Exception:
            pass

        if not self.news.events:
            d.notes = "no events"
            return d

        event = None
        for et, ccy, title in self.news.events:
            if ccy not in self._currencies(asset):
                continue
            delta = (et - pd.Timestamp(now)).total_seconds()
            if -self.PRE_WINDOW_SEC <= delta <= self.POST_WINDOW_SEC:
                event = (et, ccy, title)
                break
        if event is None:
            d.notes = "no news window"
            return d

        if len(m5) < 5:
            d.notes = "warm-up"
            return d

        tf = self.cfg.entry_tf_seconds
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=tf)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        bodies = (m5["close"] - m5["open"]).abs()
        avg = float(bodies.iloc[-21:-1].mean() or 1e-9)
        last_body = float(bodies.iloc[-1])
        last_dir = "BUY" if m5["close"].iloc[-1] > m5["open"].iloc[-1] else "SELL"

        if last_body < 1.5 * avg:
            d.notes = f"news {event[2][:20]} — no breakout yet"
            return d

        d.decision = last_dir
        d.score = 70 if last_body > 2.0 * avg else 60
        d.notes = f"NEWS BREAKOUT: {event[2][:30]} ({last_body/avg:.1f}x avg)"
        return d
