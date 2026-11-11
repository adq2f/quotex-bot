"""
strategy.py  --  orchestrator (Section 10): filters -> bias -> SMC -> indicators -> patterns -> score.
`evaluate()` is PURE with respect to market data (used by live loop and backtest).
Risk limits are applied afterwards by risk.py (they override everything, Section 8 hard overrides).

FIX LOG v2.2:
  - mtg_precheck() now re-validates the SMC setup AND the fakeout filter on the CURRENT m5 window
    before allowing MTG1. The old version only re-checked session/volatility/news/HTF-bias, so MTG1
    could fire blind after a structural break — inconsistent with the base signal's strictness.
"""
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from typing import Any, Dict, Optional, Tuple

import pandas as pd

from .bias import htf_bias
from .filters import VolatilityGuard, session_ok
from .indicators import indicator_confluence, volume_ratio_at
from .patterns import confirmation_candle, fakeout_check, strong_pattern
from .scoring import compute_score
from .smc import detect_setup


@dataclass
class Decision:
    asset: str
    is_otc: bool
    tf_seconds: int
    candle_close: Optional[datetime] = None        # close time of the signal candle (= entry time)
    htf_bias: str = "NEUTRAL"
    bias_note: str = ""
    sweep: str = "NO"
    ob_fvg: str = "NO"
    confirmation: str = "None"
    rsi_text: str = "-"
    macd_text: str = "-"
    bb_text: str = "-"
    volume_ratio: float = 0.0
    score: int = 0
    parts: Dict[str, int] = field(default_factory=dict)
    decision: str = "HOLD"                          # BUY | SELL | HOLD
    gate: str = ""                                  # first failed gate (for diagnostics)
    notes: str = ""
    news: str = "Clear"
    pattern: str = ""
    entry_price: float = 0.0
    payout_pct: float = 0.0
    stake: float = 0.0
    risk_status: str = "Normal"
    cooldown: str = "Not active"
    setup: Any = None                               # smc.Setup (not serialised)
    divergence: bool = False
    rsi_strict: bool = False

    @property
    def call_put(self) -> str:
        return {"BUY": "CALL", "SELL": "PUT"}.get(self.decision, "-")

    def to_dict(self) -> Dict:
        d = asdict(self)
        d.pop("setup", None)
        return d


class Strategy:
    def __init__(self, cfg, news=None):
        self.cfg, self.news = cfg, news
        self.vol = VolatilityGuard(cfg)

    # ------------------------------------------------------------------ main
    def evaluate(self, asset: str, m5: pd.DataFrame, h1: pd.DataFrame, h4: pd.DataFrame, now: datetime) -> Decision:
        cfg = self.cfg
        from .config import is_otc
        d = Decision(asset=asset, is_otc=is_otc(asset), tf_seconds=cfg.entry_tf_seconds)
        if len(m5) == 0:
            d.gate, d.notes = "no_data", "no candles"
            return d
        d.candle_close = (m5.index[-1] + pd.Timedelta(seconds=cfg.entry_tf_seconds)).to_pydatetime()
        d.entry_price = float(m5["close"].iloc[-1])

        # 5.5 whitelist
        if asset not in cfg.whitelist:
            d.gate, d.notes = "not_whitelisted", "asset not on whitelist [5.5]"
            return d
        # Section 5 sessions / rollover
        ok, why = session_ok(asset, now, cfg)
        if not ok:
            d.gate, d.notes = "session", why
            return d
        # Section 7 volatility / gap
        ok, why = self.vol.check(asset, m5, now)
        if not ok:
            d.gate, d.notes = "volatility", why
            return d
        # Section 7.2 news
        if self.news is not None:
            blocked, label = self.news.check(asset, now)
            d.news = label
            if blocked:
                d.gate, d.notes = "news", label
                return d

        # Section 1 bias
        b = htf_bias(h4, h1, cfg)
        d.htf_bias, d.bias_note = b.bias, b.note
        if b.bias == "NEUTRAL":
            d.gate, d.notes = "bias_neutral", b.note
            return d
        direction = "BUY" if b.bias == "BULLISH" else "SELL"       # LTF can never override HTF (1.1)

        # Section 2 SMC
        setup, gate = detect_setup(m5, direction, cfg)
        if setup is None:
            d.gate, d.notes = gate, f"SMC: {gate}"
            return d
        d.setup = setup
        d.sweep = f"YES ({setup.sweep_kind} at {setup.sweep_level:.5f})"
        d.ob_fvg = f"YES (zone {setup.zone[0]:.5f}-{setup.zone[1]:.5f})"

        # 2.4 confirmation candle
        conf = confirmation_candle(m5, direction, cfg)
        if conf is None:
            d.gate, d.notes = "no_confirmation_candle", "needs engulfing/pin bar inside zone (+4.4 context)"
            return d
        d.confirmation = conf.name

        # Section 4.3 fakeout filter on the impulse candle
        passed, why = fakeout_check(m5, direction, setup.impulse_idx, cfg)
        if not passed:
            d.gate, d.notes = "fakeout", why
            return d

        # Section 3 triple confluence
        ind = indicator_confluence(m5, direction, cfg)
        if ind is None:
            d.gate, d.notes = "not_enough_data", "indicator warm-up"
            return d
        d.rsi_text, d.macd_text, d.bb_text = f"{ind.rsi_value:.0f}", ind.macd_text, ind.bb_text
        d.divergence, d.rsi_strict = ind.divergence, ind.rsi_strict
        if not ind.ok:
            miss = [n for n, v in (("RSI", ind.rsi_ok), ("MACD", ind.macd_ok), ("BB", ind.bb_ok)) if not v]
            d.gate, d.notes = "indicators", "disagree: " + "/".join(miss) + " [3.4]"
            return d

        # Section 8 score
        d.volume_ratio = volume_ratio_at(m5, setup.impulse_idx, cfg.volume_lookback)
        pat = strong_pattern(m5, direction, cfg)
        d.pattern = pat.name if pat else ""
        sc = compute_score(True, True, True, True, d.volume_ratio, pat.reliability if pat else 0, ind.divergence, cfg)
        d.score, d.parts = sc.total, sc.parts
        if sc.verdict != "VALID":
            d.gate, d.notes = f"score_{sc.verdict.lower()}", f"score {sc.total} < {cfg.score_threshold}"
            return d

        d.decision = direction
        d.notes = f"{b.bias} bias + {setup.sweep_kind} sweep + {conf.name}"
        return d

    # ------------------------------------------------------------------ MTG pre-check (16.3)
    def mtg_precheck(self, asset: str, direction: str, m5, h1, h4, now: datetime) -> Tuple[bool, str]:
        """Re-validate market conditions before an MTG signal. Risk checks are done by RiskManager.

        FIX v2.2: also re-validates the SMC setup and fakeout filter. If the loss candle broke
        structure (OB mitigated, sweep invalidated, weak impulse, volume collapse), MTG is refused.
        This matches the base-signal strictness and prevents blind recovery trades.
        """
        cfg = self.cfg
        ok, why = session_ok(asset, now, cfg)
        if not ok:
            return False, why
        ok, why = self.vol.check(asset, m5, now)
        if not ok:
            return False, why
        if self.news is not None:
            blocked, label = self.news.check(asset, now)
            if blocked:
                return False, label
        b = htf_bias(h4, h1, cfg)
        want = "BULLISH" if direction == "BUY" else "BEARISH"
        if b.bias != want:
            return False, f"HTF bias changed ({b.bias})"

        # FIX v2.2: SMC setup must still be valid on the CURRENT m5 window, for the SAME direction.
        setup, gate = detect_setup(m5, direction, cfg)
        if setup is None:
            return False, f"SMC setup no longer valid ({gate})"
        passed, why = fakeout_check(m5, direction, setup.impulse_idx, cfg)
        if not passed:
            return False, f"fakeout on new impulse ({why})"
        return True, ""


# ---------------------------------------------------------------------- Section 9 formatting
def _fit(s: str, w: int = 40) -> str:
    return s if len(s) <= w else s[: w - 1] + "…"


def section9_text(d: Decision, tz_hours: int = 6, expiry_text: str = "") -> str:
    tfm = f"M{d.tf_seconds // 60}"
    rows = [
        ("ASSET", f"{d.asset} ({'OTC' if d.is_otc else 'Real'})"),
        ("TIMEFRAME", f"{tfm} (entry) / H4+H1 (bias)"),
        ("HTF BIAS", d.htf_bias),
        ("SWEEP", d.sweep),
        ("OB / FVG", d.ob_fvg),
        ("CONFIRMATION", d.confirmation),
        ("RSI/MACD/BB", f"{d.rsi_text} / {d.macd_text} / {d.bb_text}"),
        ("VOLUME", f"{d.volume_ratio:.1f}x avg" + (" (confirmed)" if d.volume_ratio > 1.5 else "")),
        ("SCORE", f"{d.score} / 100"),
        ("DECISION", f"{d.decision} ({d.call_put})" if d.decision != "HOLD" else "HOLD"),
        ("STAKE", f"${d.stake:.2f}" if d.stake else "-"),
        ("EXPIRY", expiry_text or "-"),
        ("RISK STATUS", d.risk_status),
        ("NEWS FILTER", d.news),
        ("COOLDOWN", d.cooldown),
        ("NOTES", d.notes),
    ]
    w = 40
    top = "┌" + "─" * (15 + w + 3) + "┐"
    bot = "└" + "─" * (15 + w + 3) + "┘"
    lines = [top]
    for k, v in rows:
        lines.append("│ " + k.ljust(13) + ": " + _fit(str(v), w).ljust(w) + " │")
    lines.append(bot)
    return "\n".join(lines)
