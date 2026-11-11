"""
patterns.py  --  Rulebook Section 4: candle patterns, context checks, fakeout filter.

INTERPRETATION NOTES (rulebook conflicts resolved conservatively; change only with your approval):
 * 4.2 "body < 30% of range = banned" is applied to engulfing / multi-candle patterns. Pin bars
   (hammer / shooting star) are defined by a small body, so they are exempt from that one test.
 * 4.3 fakeout tests are applied to the IMPULSE (breakout) candle, not to the sweep candle (which is
   SUPPOSED to have a long rejection wick) nor to the pin-bar confirmation candle.
"""
from dataclasses import dataclass
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from .indicators import ema, rsi, volume_ratio_at

RELIABILITY = {
    "Bearish Three Line Strike": 84, "Three White Soldiers": 82, "Bearish Engulfing": 79,
    "Bullish Engulfing": 79, "Morning Star": 78, "Three Black Crows": 78,
    "Piercing Line": 64, "Hammer": 60, "Shooting Star": 59,
}


def _b(df, i):
    r = df.iloc[i]
    o, h, l, c = float(r.open), float(r.high), float(r.low), float(r.close)
    rng = max(h - l, 1e-12)
    return o, h, l, c, abs(c - o), rng


def _body_ratio(df, i):
    o, h, l, c, body, rng = _b(df, i)
    return body / rng


def bullish_engulfing(df) -> bool:
    o1, _, _, c1, b1, _ = _b(df, -2)
    o2, _, _, c2, b2, _ = _b(df, -1)
    return c1 < o1 and c2 > o2 and o2 <= c1 and c2 >= o1 and b2 > b1 and _body_ratio(df, -1) >= 0.30


def bearish_engulfing(df) -> bool:
    o1, _, _, c1, b1, _ = _b(df, -2)
    o2, _, _, c2, b2, _ = _b(df, -1)
    return c1 > o1 and c2 < o2 and o2 >= c1 and c2 <= o1 and b2 > b1 and _body_ratio(df, -1) >= 0.30


def hammer(df) -> bool:
    o, h, l, c, body, rng = _b(df, -1)
    lw, uw = min(o, c) - l, h - max(o, c)
    return lw >= 2 * max(body, rng * 0.05) and uw <= max(body, rng * 0.15) and min(o, c) >= l + rng * 0.55


def shooting_star(df) -> bool:
    o, h, l, c, body, rng = _b(df, -1)
    lw, uw = min(o, c) - l, h - max(o, c)
    return uw >= 2 * max(body, rng * 0.05) and lw <= max(body, rng * 0.15) and max(o, c) <= l + rng * 0.45


def three_white_soldiers(df) -> bool:
    for k in (-3, -2, -1):
        o, _, _, c, _, _ = _b(df, k)
        if c <= o or _body_ratio(df, k) < 0.5:
            return False
    c = [_b(df, k)[3] for k in (-3, -2, -1)]
    o = [_b(df, k)[0] for k in (-3, -2, -1)]
    return c[0] < c[1] < c[2] and o[1] > o[0] and o[1] < c[0] and o[2] > o[1] and o[2] < c[1]


def three_black_crows(df) -> bool:
    for k in (-3, -2, -1):
        o, _, _, c, _, _ = _b(df, k)
        if c >= o or _body_ratio(df, k) < 0.5:
            return False
    c = [_b(df, k)[3] for k in (-3, -2, -1)]
    o = [_b(df, k)[0] for k in (-3, -2, -1)]
    return c[0] > c[1] > c[2] and o[1] < o[0] and o[1] > c[0] and o[2] < o[1] and o[2] > c[1]


def morning_star(df) -> bool:
    o1, _, _, c1, b1, r1 = _b(df, -3)
    o2, _, _, c2, b2, r2 = _b(df, -2)
    o3, _, _, c3, b3, r3 = _b(df, -1)
    return (c1 < o1 and b1 / r1 >= 0.5 and b2 / r2 <= 0.30 and max(o2, c2) < c1 + b1 * 0.25
            and c3 > o3 and c3 > (o1 + c1) / 2 and b3 / r3 >= 0.5)


def piercing_line(df) -> bool:
    o1, _, l1, c1, _, _ = _b(df, -2)
    o2, _, _, c2, _, _ = _b(df, -1)
    return c1 < o1 and c2 > o2 and o2 < c1 and (o1 + c1) / 2 < c2 < o1 and _body_ratio(df, -1) >= 0.30


def bearish_three_line_strike(df) -> bool:
    for k in (-4, -3, -2):
        o, _, _, c, _, _ = _b(df, k)
        if c >= o:
            return False
    c = [_b(df, k)[3] for k in (-4, -3, -2)]
    if not (c[0] > c[1] > c[2]):
        return False
    o4, _, _, c4, _, _ = _b(df, -1)
    o1 = _b(df, -4)[0]
    return c4 > o4 and o4 <= c[2] and c4 >= o1 and _body_ratio(df, -1) >= 0.30


@dataclass
class PatternHit:
    name: str
    reliability: int
    is_confirmation: bool      # engulfing / pin bar usable as 2.4 confirmation candle


def _pos_in_range(df, n=20) -> float:
    w = df.iloc[-n:]
    lo, hi = float(w["low"].min()), float(w["high"].max())
    return 0.5 if hi == lo else (float(df["close"].iloc[-1]) - lo) / (hi - lo)


def confirmation_candle(df: pd.DataFrame, direction: str, cfg) -> Optional[PatternHit]:
    """2.4: engulfing OR pin bar, with the 4.4 context checks."""
    r = float(rsi(df["close"], cfg.rsi_period).iloc[-1])
    pos = _pos_in_range(df)
    if direction == "BUY":
        if bullish_engulfing(df) and pos <= 0.30:
            return PatternHit("Bullish Engulfing", RELIABILITY["Bullish Engulfing"], True)
        if hammer(df) and r < 40:
            return PatternHit("Hammer (Pin Bar)", RELIABILITY["Hammer"], True)
    else:
        if bearish_engulfing(df) and pos >= 0.70:
            return PatternHit("Bearish Engulfing", RELIABILITY["Bearish Engulfing"], True)
        if shooting_star(df) and r > 60:
            return PatternHit("Shooting Star (Pin Bar)", RELIABILITY["Shooting Star"], True)
    return None


def strong_pattern(df: pd.DataFrame, direction: str, cfg) -> Optional[PatternHit]:
    """Best strong pattern for scoring (+5), with 4.4 context checks. Weak candles never qualify (4.2)."""
    hits = []
    e20, e50 = ema(df["close"], 20).iloc[-1], ema(df["close"], 50).iloc[-1]
    c = df["close"]
    move10 = abs(float(c.iloc[-1]) - float(c.iloc[-11])) / float(c.iloc[-11]) * 100 if len(df) > 11 else 0.0
    if direction == "BUY":
        cc = confirmation_candle(df, "BUY", cfg)
        if cc:
            hits.append(cc)
        if three_white_soldiers(df) and e20 > e50:
            hits.append(PatternHit("Three White Soldiers", 82, False))
        if morning_star(df):
            hits.append(PatternHit("Morning Star", 78, False))
        if piercing_line(df):
            hits.append(PatternHit("Piercing Line", 64, False))
    else:
        cc = confirmation_candle(df, "SELL", cfg)
        if cc:
            hits.append(cc)
        if three_black_crows(df) and e20 < e50:
            hits.append(PatternHit("Three Black Crows", 78, False))
        if bearish_three_line_strike(df) and move10 >= cfg.three_line_strike_min_move_pct:
            hits.append(PatternHit("Bearish Three Line Strike", 84, False))
    return max(hits, key=lambda h: h.reliability) if hits else None


def fakeout_check(df: pd.DataFrame, direction: str, impulse_idx: int, cfg) -> Tuple[bool, str]:
    """4.3 on the impulse (breakout) candle. Returns (passed, reason)."""
    o, h, l, c, body, rng = _b(df, impulse_idx)
    if body / rng < cfg.min_body_ratio:
        return False, "weak_body(<50%)"
    if volume_ratio_at(df, impulse_idx, cfg.volume_lookback) < 1.0:
        return False, "volume_below_avg"
    wick = (h - max(o, c)) if direction == "BUY" else (min(o, c) - l)
    if wick > cfg.max_wick_body_mult * body:
        return False, "wick>2x_body"
    nxt = impulse_idx + 1
    if nxt < len(df) - 1:                                   # a candle between impulse and confirmation
        nc = float(df["close"].iloc[nxt])
        if (direction == "BUY" and nc < o) or (direction == "SELL" and nc > o):
            return False, "re-entered_prior_range"
    return True, ""
