"""
smc.py  --  Rulebook Section 2 (SMC / ICT triggers). Implemented in-house (no external SMC lib).
Setup for BUY (mirror for SELL):
  sweep of a prior swing low (SSL) with rejection wick  ->  impulsive up candle  ->
  order block (last bearish candle before impulse) overlapping a FRESH bullish FVG  ->
  last closed candle (confirmation) taps the zone and closes back out of it.
Zone freshness (2.6): untouched from creation until the confirmation candle, and <= fresh_zone_lookback old.
"""
from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

import numpy as np
import pandas as pd

from .bias import swing_points


@dataclass
class Setup:
    direction: str                    # BUY | SELL
    sweep_idx: int
    sweep_level: float
    sweep_kind: str                   # SSL | BSL
    impulse_idx: int
    ob: Tuple[float, float]           # (low, high)
    ob_idx: int
    fvg: Optional[Tuple[float, float]]
    fvg_idx: Optional[int]
    zone: Tuple[float, float]         # entry zone (OB ∩ FVG, or OB if FVG not required)
    note: str = ""


def _arrays(df):
    return (df["open"].values, df["high"].values, df["low"].values, df["close"].values)


def find_sweep(df: pd.DataFrame, direction: str, cfg):
    """2.1: sweep + rejection back inside. Returns (idx, level) or None. Looks at the last sweep_lookback candles
    before the confirmation candle."""
    o, h, l, c = _arrays(df)
    n = len(df)
    sh, sl = swing_points(df, cfg.swing_lookback)
    lo_w = max(cfg.swing_lookback + 1, n - 1 - cfg.sweep_lookback)
    for s in range(n - 2, lo_w - 1, -1):
        rng = h[s] - l[s]
        if rng <= 0:
            continue
        if direction == "BUY":
            cands = [(i, p) for i, p in sl if i + cfg.swing_lookback < s]
            for i, p in reversed(cands):
                if s - 1 >= i + 1 and l[i + 1:s].min() < p:
                    continue                                    # level already broken earlier
                lower_wick = min(o[s], c[s]) - l[s]
                if l[s] < p and c[s] > p and lower_wick / rng >= cfg.sweep_min_wick_ratio:
                    return s, float(p)
        else:
            cands = [(i, p) for i, p in sh if i + cfg.swing_lookback < s]
            for i, p in reversed(cands):
                if s - 1 >= i + 1 and h[i + 1:s].max() > p:
                    continue
                upper_wick = h[s] - max(o[s], c[s])
                if h[s] > p and c[s] < p and upper_wick / rng >= cfg.sweep_min_wick_ratio:
                    return s, float(p)
    return None


def find_impulse(df: pd.DataFrame, direction: str, start: int, cfg) -> Optional[int]:
    """Displacement candle after the sweep (start..start+5), never the last (confirmation) candle."""
    o, h, l, c = _arrays(df)
    n = len(df)
    body = np.abs(c - o)
    avg_body = pd.Series(body).rolling(20).mean().shift(1).values
    for j in range(max(start, 1), min(start + 6, n - 1)):
        if np.isnan(avg_body[j]):
            continue
        if direction == "BUY" and c[j] > o[j] and body[j] >= cfg.impulse_body_mult * avg_body[j] and c[j] > h[j - 1]:
            return j
        if direction == "SELL" and c[j] < o[j] and body[j] >= cfg.impulse_body_mult * avg_body[j] and c[j] < l[j - 1]:
            return j
    return None


def find_order_block(df: pd.DataFrame, direction: str, sweep_idx: int, impulse_idx: int):
    """2.2: BUY -> last bearish candle before the impulse; SELL -> last bullish candle."""
    o, _, _, c = _arrays(df)
    for k in range(impulse_idx - 1, max(-1, sweep_idx - 4), -1):
        if direction == "BUY" and c[k] < o[k]:
            return k
        if direction == "SELL" and c[k] > o[k]:
            return k
    return None


def _touched(df, lo, hi, a, b) -> bool:
    """any candle in [a, b] overlaps zone [lo, hi]"""
    if b < a:
        return False
    seg = df.iloc[a:b + 1]
    return bool(((seg["low"] <= hi) & (seg["high"] >= lo)).any())


def find_fresh_fvg(df: pd.DataFrame, direction: str, sweep_idx: int, impulse_idx: int, cfg):
    """2.3: 3-candle imbalance created around the impulse, untouched until the confirmation candle."""
    _, h, l, _ = _arrays(df)
    n = len(df)
    best = None
    for i in range(sweep_idx + 1, min(impulse_idx + 2, n - 1)):    # i = candle 3 index
        if i < 2:
            continue
        mid = i - 1
        if not (sweep_idx <= mid <= impulse_idx):
            continue
        if direction == "BUY" and l[i] > h[i - 2]:
            zone = (float(h[i - 2]), float(l[i]))
        elif direction == "SELL" and h[i] < l[i - 2]:
            zone = (float(h[i]), float(l[i - 2]))
        else:
            continue
        if n - 1 - i > cfg.fresh_zone_lookback:
            continue
        if _touched(df, zone[0], zone[1], i + 1, n - 2):             # mitigated before confirmation -> stale
            continue
        best = (zone, i)
    return best


def last_structure_break(df: pd.DataFrame, cfg, lookback: int = 15) -> Optional[str]:
    """Most recent close-through of a confirmed swing within `lookback` candles: 'BULL' | 'BEAR' | None."""
    sh, sl = swing_points(df, cfg.swing_lookback)
    c = df["close"].values
    n = len(df)
    res = None
    for t in range(max(cfg.swing_lookback + 2, n - lookback), n):
        hs = [p for i, p in sh if i + cfg.swing_lookback < t]
        ls = [p for i, p in sl if i + cfg.swing_lookback < t]
        if hs and c[t] > hs[-1] and c[t - 1] <= hs[-1]:
            res = "BULL"
        if ls and c[t] < ls[-1] and c[t - 1] >= ls[-1]:
            res = "BEAR"
    return res


def detect_setup(df: pd.DataFrame, direction: str, cfg) -> Tuple[Optional[Setup], str]:
    """Returns (Setup, '') or (None, failed_gate). The confirmation candle is df.iloc[-1]."""
    if len(df) < cfg.min_entry_candles:
        return None, "not_enough_candles"
    sw = find_sweep(df, direction, cfg)
    if not sw:
        return None, "no_sweep"                                      # 2.1 / 2.5
    s, level = sw
    j = find_impulse(df, direction, s, cfg)
    if j is None:
        return None, "no_impulse"
    k = find_order_block(df, direction, s, j)
    if k is None:
        return None, "no_order_block"                                # 2.2
    ob = (float(df["low"].iloc[k]), float(df["high"].iloc[k]))
    n = len(df)
    if n - 1 - k > cfg.fresh_zone_lookback:
        return None, "ob_too_old"
    if _touched(df, ob[0], ob[1], j + 1, n - 2):
        return None, "ob_mitigated"                                  # 2.6
    fv = find_fresh_fvg(df, direction, s, j, cfg)
    fvg, fvg_idx, zone = None, None, ob
    if fv:
        fvg, fvg_idx = fv
        # OB and FVG usually share a boundary (the OB candle is candle 1 of the FVG), so 'overlap' = overlap OR touch.
        if fvg[0] <= ob[1] + 1e-12 and fvg[1] >= ob[0] - 1e-12:
            zone = (min(ob[0], fvg[0]), max(ob[1], fvg[1]))      # entry zone = OB+FVG span
        elif cfg.smc_require_fvg:
            return None, "ob_fvg_no_overlap"                         # 2.3
    elif cfg.smc_require_fvg:
        return None, "no_fresh_fvg"                                  # 2.3

    # 2.4: confirmation candle must sit INSIDE the zone (tap it, not close through it)
    lo_c, hi_c, cl = float(df["low"].iloc[-1]), float(df["high"].iloc[-1]), float(df["close"].iloc[-1])
    if direction == "BUY":
        inside = lo_c <= zone[1] and lo_c >= zone[0] - (zone[1] - zone[0]) * 0.25 and cl >= zone[0]
    else:
        inside = hi_c >= zone[0] and hi_c <= zone[1] + (zone[1] - zone[0]) * 0.25 and cl <= zone[1]
    if not inside:
        return None, "confirmation_outside_zone"

    brk = last_structure_break(df, cfg)                              # 1.5: CHoCH against bias = WAIT
    if (direction == "BUY" and brk == "BEAR") or (direction == "SELL" and brk == "BULL"):
        return None, "choch_against_bias"

    return Setup(direction=direction, sweep_idx=s, sweep_level=level,
                 sweep_kind="SSL" if direction == "BUY" else "BSL",
                 impulse_idx=j, ob=ob, ob_idx=k, fvg=fvg, fvg_idx=fvg_idx, zone=zone), ""
