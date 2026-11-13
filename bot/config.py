"""
config.py  --  Rulebook settings, timeframe→strategy lock, full asset list.
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
    try: return float(os.getenv(name, default))
    except ValueError: return float(default)

def _i(name: str, default: int) -> int:
    try: return int(float(os.getenv(name, default)))
    except ValueError: return int(default)

def _b(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).strip().lower() in ("1", "true", "yes", "on")

def _list(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default)
    return [x.strip() for x in raw.split(",") if x.strip()]

# OTC পেয়ার বাদ দেওয়া হয়েছে, শুধু রিয়েল মার্কেটের পেয়ার রাখা হয়েছে
DEFAULT_WHITELIST = (
    "EURUSD,GBPUSD,USDJPY,AUDUSD,USDCAD,XAUUSD"
)

@dataclass(frozen=True)
class Settings:
    account_type: str = os.getenv("ACCOUNT_TYPE", "DEMO").upper()
    execution_mode: str = os.getenv("EXECUTION_MODE", "signal_only").lower()
    quotex_email: str = os.getenv("QUOTEX_EMAIL", "")
    quotex_password: str = os.getenv("QUOTEX_PASSWORD", "")

    telegram_token: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
    telegram_chat_id: str = os.getenv("TELEGRAM_CHAT_ID", "")
    display_utc_offset_hours: int = 6

    whitelist: Tuple[str, ...] = tuple(_list("ASSET_WHITELIST", DEFAULT_WHITELIST))

    entry_tf_seconds: int = _i("ENTRY_TF_SECONDS", 60)
    bias_tf_seconds: Tuple[int, int] = (14400, 3600)
    expiry_candles: int = min(max(_i("EXPIRY_CANDLES", 1), 1), 2)
    
    # সিগন্যাল বাতিল (Stale) হওয়া বন্ধ করতে ১২০ সেকেন্ড করা হলো
    signal_max_age_seconds: int = min(max(_i("SIGNAL_MAX_AGE_SECONDS", 120), 1), 300)

    strategy_mode: str = os.getenv("STRATEGY_MODE", "quick").lower()
    web_timeframe_seconds: int = _i("WEB_TIMEFRAME_SECONDS", 60)

    prepare_alert_seconds: int = _i("PREPARE_ALERT_SECONDS", 0)
    prepare_min_score: int = _i("PREPARE_MIN_SCORE", 45)
    prepare_max_per_cycle: int = _i("PREPARE_MAX_PER_CYCLE", 1)

    max_risk_pct: float = min(max(_f("MAX_RISK_PCT", 1.0), 0.1), 2.0)
    daily_loss_pct: float = min(max(_f("DAILY_LOSS_PCT", 5.0), 0.5), 5.0)
    daily_profit_pct: float = _f("DAILY_PROFIT_PCT", 3.0)
    max_consec_losses: int = min(max(_i("MAX_CONSEC_LOSSES", 3), 1), 3)
    cooldown_minutes: int = max(_i("COOLDOWN_MINUTES", 30), 30)
    drawdown_guard_pct: float = min(max(_f("DRAWDOWN_GUARD_PCT", 15.0), 5.0), 15.0)
    min_payout_pct: float = max(_f("MIN_PAYOUT_PCT", 65.0), 65.0)
    max_concurrent_trades: int = 1

    max_signals_per_day: int = _i("MAX_SIGNALS_PER_DAY", 50)
    asset_loss_lock: int = _i("ASSET_LOSS_LOCK", 3)

    mtg_enabled: bool = _b("MTG_ENABLED", False)
    mtg_max_steps: int = min(max(_i("MTG_MAX_STEPS", 1), 1), 2)
    mtg_base_risk_pct: float = min(max(_f("MTG_BASE_RISK_PCT", 0.5), 0.1), 1.0)

    score_threshold: int = _i("SCORE_THRESHOLD", 65)
    watchlist_threshold: int = 45

    rsi_period: int = 14
    rsi_buy_max: float = 35.0
    rsi_sell_min: float = 65.0
    rsi_buy_strict: float = 25.0
    rsi_sell_strict: float = 75.0
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    bb_period: int = 20
    bb_std: float = 2.0
    atr_period: int = 14
    ema_bias: int = 200

    fresh_zone_lookback: int = 40
    swing_lookback: int = 3

    min_body_ratio: float = 0.50
    min_pattern_body_ratio: float = 0.30
    max_wick_body_mult: float = 2.0
    volume_lookback: int = 20

    london_kz: Tuple[int, int] = (0, 24)
    newyork_kz: Tuple[int, int] = (0, 24)
    otc_size_multiplier: float = 0.5
    otc_block_minutes: int = 5
    otc_atr_max_mult: float = 2.0

    news_block_before_min: int = 15
    news_block_after_min: int = 15
    atr_lock_mult: float = 2.0
    atr_lock_pause_min: int = 10
    gap_lock_pct: float = 1.0
    gap_lock_candles: int = 3
    news_api_key: str = os.getenv("NEWS_API_KEY", "")

    database_url: str = os.getenv("DATABASE_URL", "")

    virtual_balance: float = _f("VIRTUAL_BALANCE", 1000.0)
    default_payout_pct: float = _f("DEFAULT_PAYOUT_PCT", 85.0)
    stop_on_daily_profit: bool = _b("STOP_ON_DAILY_PROFIT", True)
    indicator_window: int = max(_i("INDICATOR_WINDOW", 2), 1)
    smc_require_fvg: bool = _b("SMC_REQUIRE_FVG", False)
    sweep_lookback: int = 8
    sweep_min_wick_ratio: float = _f("SWEEP_MIN_WICK_RATIO", 0.30)
    impulse_body_mult: float = _f("IMPULSE_BODY_MULT", 1.2)
    three_line_strike_min_move_pct: float = _f("TLS_MIN_MOVE_PCT", 5.0)
    
    # ১ মিনিটে দ্রুত ডাটা পেতে ২০ ক্যান্ডেল করা হলো
    min_entry_candles: int = 20
    min_bias_candles: int = 100
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

    data_source: str = os.getenv("DATA_SOURCE", "biquote").lower()
    csv_dir: str = os.getenv("CSV_DIR", "data")

    port: int = _i("PORT", 10000)

settings = Settings()

def is_otc(asset: str) -> bool:
    return asset.lower().endswith("_otc")

def validate() -> List[str]:
    warnings = []
    if settings.account_type != "DEMO": warnings.append("ACCOUNT_TYPE is not DEMO.")
    if settings.execution_mode != "signal_only": warnings.append("EXECUTION_MODE=execute. Default should be signal_only.")
    if settings.mtg_enabled: warnings.append("MTG is ENABLED. Max %d step(s)." % settings.mtg_max_steps)
    if not settings.whitelist: warnings.append("ASSET_WHITELIST empty -> bot will never trade.")
    if not settings.telegram_token or not settings.telegram_chat_id: warnings.append("Telegram token/chat id missing.")
    if settings.data_source not in ("biquote", "csv", "synthetic", "quotex"): warnings.append(f"DATA_SOURCE='{settings.data_source}' not supported.")
    return warnings

# সব টাইমফ্রেমেই Universal Best Strategy (quick) চলবে
TIMEFRAME_STRATEGY_MAP = {
    60:   "quick",
    300:  "quick",
    900:  "quick",
    3600: "quick",
}

def strategy_for_timeframe(tf_seconds: int) -> str:
    return TIMEFRAME_STRATEGY_MAP.get(int(tf_seconds), "quick")
