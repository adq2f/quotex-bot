"""
bias.py  --  Rulebook Section 1 (HTF bias engine). H4 + H1 = BIAS ONLY.
Returns BULLISH / BEARISH / NEUTRAL. NEUTRAL => NO TRADE (1.4, 1.6).
"""
from dataclasses import dataclass
from typing import List, Tuple

import numpy as np
import pandas as pd

from .indicators import atr, ema


def swing_points(df: pd.DataFrame, n: int) -> Tuple[List[Tuple[int, float]], List[Tuple[int, float]]]:
    """Confirmed pivots only: a swing needs n candles on each side. Vectorized via centered rolling."""
    w = 2 * n + 1
    hi = df["high"].rolling(w, center=True).max()
    lo = df["low"].rolling(w, center=True).min()
    is_h = (df["high"] == hi) & hi.notna()
    is_l = (df["low"] == lo) & lo.notna()
    sh = [(int(i), float(df["high"].iloc[i])) for i in np.flatnonzero(is_h.values)]
    sl = [(int(i), float(df["low"].iloc[i])) for i in np.flatnonzero(is_l.values)]
    return sh, sl


@dataclass
class BiasResult:
    bias: str          # BULLISH | BEARISH | NEUTRAL
    h4: str
    h1: str
    note: str


def single_tf_bias(df: pd.DataFrame, cfg, swing_n: int = 3) -> Tuple[str, str]:
    if len(df) < cfg.min_bias_candles:
        return "NEUTRAL", f"only {len(df)} candles (<{cfg.min_bias_candles} for EMA{cfg.ema_bias})"
    e = ema(df["close"], cfg.ema_bias)
    price, e_last = float(df["close"].iloc[-1]), float(e.iloc[-1])
    a = float(atr(df, cfg.atr_period).iloc[-1])
    if abs(price - e_last) < 0.25 * a:               # 1.4: oscillating around EMA200
        return "NEUTRAL", "price hugging EMA200"
    sh, sl = swing_points(df, swing_n)
    if len(sh) < 2 or len(sl) < 2:
        return "NEUTRAL", "not enough swings"
    hh, hl = sh[-1][1] > sh[-2][1], sl[-1][1] > sl[-2][1]
    lh, ll = sh[-1][1] < sh[-2][1], sl[-1][1] < sl[-2][1]
    if price > e_last and hh and hl:
        return "BULLISH", "above EMA200, HH+HL"
    if price < e_last and lh and ll:
        return "BEARISH", "below EMA200, LH+LL"
    return "NEUTRAL", "structure overlapping/choppy"


def htf_bias(h4: pd.DataFrame, h1: pd.DataFrame, cfg) -> BiasResult:
    b4, n4 = single_tf_bias(h4, cfg)
    b1, n1 = single_tf_bias(h1, cfg)
    if b4 == b1 and b4 != "NEUTRAL":
        return BiasResult(b4, b4, b1, f"H4 {n4}; H1 {n1}")
    if b4 != b1:
        return BiasResult("NEUTRAL", b4, b1, f"H4/H1 disagree ({b4} vs {b1}) [1.6]")
    return BiasResult("NEUTRAL", b4, b1, f"H4: {n4}; H1: {n1}")
