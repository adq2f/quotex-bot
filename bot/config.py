
# ============================================================
# Timeframe → Strategy auto-mapping (locked)
# ============================================================
TIMEFRAME_STRATEGY_MAP = {
    60:   "quick",       # 1 min → Quick M1 (EMA+RSI+MACD+BB+Stoch)
    300:  "rulebook",    # 5 min → Rulebook (SMC)
    900:  "rulebook",    # 15 min → Rulebook
    3600: "rulebook",    # 1 hour → Rulebook
}


def strategy_for_timeframe(tf_seconds: int) -> str:
    """Return the ONE correct strategy for this timeframe (no user choice)."""
    return TIMEFRAME_STRATEGY_MAP.get(int(tf_seconds), "rulebook")
