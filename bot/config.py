"""
config.py  --  Rulebook Sections 1, 5, 6, 7, 8, 15, 16, 17
All settings come from environment variables (Render-friendly).
HARD RULES are clamped here so no env value can break them:
  - risk per trade capped at 2%            (6.1)
  - daily loss limit cannot be 0/disabled  (6.3, 13.6)
  - drawdown guard cannot be disabled      (6.9, 13.6)
  - MTG steps capped at 2, default OFF     (6.5, 13.5, 16.1)
  - indicator reads on CLOSED candles only (3.5)
"""
import os
from dataclasses import dataclass, field
from typing import List, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass


def _f(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except ValueError:
        return float(default)


def _i(name: str, default: int) -> int:
    try:
        return int(float(os.getenv(name, default)))
    except ValueError:
        return int(default)


def _b(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")


def _list(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # ---- Account / mode (Section 11, 17.4) ----
    account_type: str = os.getenv("ACCOUNT_TYPE", "DEMO").upper()
    execution_mode: str = os.getenv("EXECUTION_MODE", "signal_only").lower()  # signal_only | execute
    quotex_email: str = os.getenv("QUOTEX_EMAIL", "")
    quotex_password: str = os.getenv("QUOTEX_PASSWORD", "")

    # ---- Telegram (Section 17) ----
    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    display_utc_offset_hours: int = 6          # Bangladesh (17.2)

    # ---- Assets (5.5) ----
    whitelist: Tuple[str, ...] = tuple(_list("ASSET_WHITELIST", "EURUSD_otc,GBPUSD_otc,USDJPY_otc"))

    # ---- Timeframes / expiry (15.1) ----
    entry_tf_seconds: int = _i("ENTRY_TF_SECONDS", 300)   # 300 = M5
    bias_tf_seconds: Tuple[int, int] = (14400, 3600)       # H4, H1 (1.1)
    expiry_candles: int = min(max(_i("EXPIRY_CANDLES", 1), 1), 2)
    signal_max_age_seconds: int = min(max(_i("SIGNAL_MAX_AGE_SECONDS", 3), 1), 10)   # 15.3

    # ---- Risk (Section 6) -- CLAMPED ----
    max_risk_pct: float = min(max(_f("MAX_RISK_PCT", 1.0), 0.1), 2.0)          # 6.1 hard cap 2
    daily_loss_pct: float = min(max(_f("DAILY_LOSS_PCT", 5.0), 0.5), 5.0)      # 6.3 cannot disable
    daily_profit_pct: float = _f("DAILY_PROFIT_PCT", 3.0)                      # 6.4
    max_consec_losses: int = min(max(_i("MAX_CONSEC_LOSSES", 3), 1), 3)        # 6.2
    cooldown_minutes: int = max(_i("COOLDOWN_MINUTES", 30), 30)                # 6.2
    drawdown_guard_pct: float = min(max(_f("DRAWDOWN_GUARD_PCT", 15.0), 5.0), 15.0)  # 6.9
    min_payout_pct: float = max(_f("MIN_PAYOUT_PCT", 80.0), 80.0)              # 6.10
    max_concurrent_trades: int = 1                                             # 6.7

    # ---- Extra v2.1 limits (advanced) ----
    max_signals_per_day: int = _i("MAX_SIGNALS_PER_DAY", 6)
    asset_loss_lock: int = _i("ASSET_LOSS_LOCK", 2)        # same asset loses N times today -> lock for the day

    # ---- MTG (Section 16) -- default OFF, hard cap 2 steps, we use 1 ----
    mtg_enabled: bool = _b("MTG_ENABLED", False)
    mtg_max_steps: int = min(max(_i("MTG_MAX_STEPS", 1), 1), 2)
    mtg_base_risk_pct: float = min(max(_f("MTG_BASE_RISK_PCT", 0.5), 0.1), 1.0)

    # ---- Scoring (Section 8) ----
    score_threshold: int = _i("SCORE_THRESHOLD", 75)
    watchlist_threshold: int = 60

    # ---- Indicators (Section 3) ----
    rsi_period: int = 14
    rsi_buy_max: float = 30.0
    rsi_sell_min: float = 70.0
    rsi_buy_strict: float = 25.0
    rsi_sell_strict: float = 75.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    ema_bias: int = 200

    # ---- SMC (Section 2) ----
    fresh_zone_lookback: int = 20      # 2.6
    swing_lookback: int = 5

    # ---- Fakeout filter (Section 4) ----
    min_body_ratio: float = 0.50       # 4.3
    min_pattern_body_ratio: float = 0.30  # 4.2
    max_wick_body_mult: float = 2.0    # 4.3
    volume_lookback: int = 20

    # ---- Sessions (Section 5) -- UTC ----
    london_kz: Tuple[int, int] = (8, 10)
    newyork_kz: Tuple[int, int] = (13, 15)
    otc_size_multiplier: float = 0.5   # 5.2
    otc_block_minutes: int = 15        # 5.2 first/last minutes
    otc_atr_max_mult: float = 2.0

    # ---- News / volatility (Section 7) ----
    news_block_before_min: int = 15
    news_block_after_min: int = 15
    atr_lock_mult: float = 2.0
    atr_lock_pause_min: int = 10
    gap_lock_pct: float = 1.0
    gap_lock_candles: int = 3
    news_api_key: str = os.getenv("NEWS_API_KEY", "")

    # ---- Storage ----
    database_url: str = os.getenv("DATABASE_URL", "")

    # ---- v2.1 additions ----
    virtual_balance: float = _f("VIRTUAL_BALANCE", 1000.0)
    default_payout_pct: float = _f("DEFAULT_PAYOUT_PCT", 85.0)
    stop_on_daily_profit: bool = _b("STOP_ON_DAILY_PROFIT", True)
    indicator_window: int = max(_i("INDICATOR_WINDOW", 1), 1)
    smc_require_fvg: bool = _b("SMC_REQUIRE_FVG", True)
    sweep_lookback: int = 8
    sweep_min_wick_ratio: float = 0.40
    impulse_body_mult: float = 1.5
    three_line_strike_min_move_pct: float = _f("TLS_MIN_MOVE_PCT", 5.0)
    min_entry_candles: int = 60
    min_bias_candles: int = 200
    stake_min: float = 1.0
    news_calendar_url: str = os.getenv("NEWS_CALENDAR_URL", "https://nfs.faireconomy.media/ff_calendar_thisweek.json")
    news_enabled: bool = _b("NEWS_ENABLED", True)
    news_fail_policy: str = os.getenv("NEWS_FAIL_POLICY", "block").lower()
    rollover_block_utc: str = os.getenv("ROLLOVER_BLOCK_UTC", "21:45-22:15")
    otc_session_utc: str = os.getenv("OTC_SESSION_UTC", "00:00-24:00")
    db_path: str = os.getenv("DB_PATH", "quotex_bot.db")
    charts_dir: str = os.getenv("CHARTS_DIR", "charts_tmp")
    admin_key: str = os.getenv("ADMIN_KEY", "")
    allow_real_execution: bool = _b("ALLOW_REAL_EXECUTION", False)
    scan_delay_seconds: float = _f("SCAN_DELAY_SECONDS", 0.5)
    daily_report_hour_utc: int = _i("DAILY_REPORT_HOUR_UTC", 17)

    # ---- Data source selection ----
    #   yfinance  -> FREE real forex data via Yahoo Finance (no API key) — for testing
    #   csv       -> offline CSV replay
    #   synthetic -> deterministic fake data (smoke test only)
    data_source: str = os.getenv("DATA_SOURCE", "synthetic").lower()

    csv_dir: str = os.getenv("CSV_DIR", "data")

    # ---- Server ----
    port: int = _i("PORT", 10000)


settings = Settings()


def is_otc(asset: str) -> bool:
    """Quotex OTC assets end with _otc."""
    return asset.lower().endswith("_otc")


def validate() -> List[str]:
    """Return list of warnings; main.py logs them at startup."""
    warnings = []
    if settings.account_type != "DEMO":
        warnings.append("ACCOUNT_TYPE is not DEMO. Section 12.6 requires 2 weeks DEMO validation first.")
    if settings.execution_mode != "signal_only":
        warnings.append("EXECUTION_MODE=execute. Default should be signal_only until Section 12 passes.")
    if settings.mtg_enabled:
        warnings.append("MTG is ENABLED. Max %d step(s). Variance is higher; track MTG stats separately." % settings.mtg_max_steps)
    if not settings.whitelist:
        warnings.append("ASSET_WHITELIST empty -> bot will never trade (5.5).")
    if not settings.telegram_token or not settings.telegram_chat_id:
        warnings.append("Telegram token/chat id missing -> signals cannot be delivered.")
    if settings.data_source not in ("yfinance", "csv", "synthetic"):
        warnings.append(
            f"DATA_SOURCE='{settings.data_source}' is not supported in this build. "
            f"Use 'yfinance' (free forex) or 'synthetic' for testing."
        )
    return warnings
