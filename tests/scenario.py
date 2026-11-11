"""Hand-built BUY scenario (sweep -> impulse -> OB+FVG -> retrace tap) used to unit-test smc/patterns/chart."""
import numpy as np
import pandas as pd


def bullish_scenario(n_pre: int = 75) -> pd.DataFrame:
    rng = np.random.default_rng(3)
    rows = []
    price = 1.1005
    for i in range(n_pre):
        o = price
        c = o + rng.normal(0, 0.00012)
        hi = max(o, c) + abs(rng.normal(0, 0.00008))
        lo = min(o, c) - abs(rng.normal(0, 0.00008))
        rows.append([o, hi, lo, c])
        price = c
    df = pd.DataFrame(rows, columns=["open", "high", "low", "close"])
    # force an obvious swing low at idx 60 (lowest of 55..65, never broken afterwards)
    df.loc[60, ["low"]] = 1.0990
    df.loc[60, "open"] = 1.0994; df.loc[60, "close"] = 1.0995; df.loc[60, "high"] = 1.0997
    for i in range(50, n_pre):
        if i != 60:
            df.loc[i, "low"] = max(df.loc[i, "low"], 1.0993)
            df.loc[i, "open"] = max(df.loc[i, "open"], 1.0996); df.loc[i, "close"] = max(df.loc[i, "close"], 1.0996)
            df.loc[i, "high"] = max(df.loc[i, "high"], df.loc[i, ["open", "close"]].max())
    extra = [
        # sweep candle s=75 : wick below 1.0990, closes back above
        [1.0996, 1.0997, 1.0985, 1.0995],
        # OB candle 76 (bearish)
        [1.0996, 1.0999, 1.0991, 1.0993],
        # impulse 77 (big bullish, closes above prior high)
        [1.0993, 1.1011, 1.0992, 1.1010],
        # 78 leaves FVG (low 1.1002 > high of 76 = 1.0999 -> gap 1.0999-1.1002)
        [1.1010, 1.1015, 1.1002, 1.1013],
        [1.1013, 1.1015, 1.1008, 1.1011],
        [1.1011, 1.1013, 1.1007, 1.1010],
        [1.1010, 1.1011, 1.1006, 1.1009],
        # 82 confirmation: taps zone (1.0999-1.1002), bullish engulfing
        [1.1008, 1.1016, 1.1000, 1.1015],
    ]
    df = pd.concat([df, pd.DataFrame(extra, columns=df.columns)], ignore_index=True)
    df["volume"] = 0.0
    # volume proxy for impulse: make its range large (already is)
    df.index = pd.date_range("2026-11-10 08:00", periods=len(df), freq="5min", tz="UTC")
    return df
