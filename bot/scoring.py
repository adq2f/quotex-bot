"""
scoring.py  --  Rulebook Section 8. Hard gates first, then the 0-100 score.
NOTE: because every gate is mandatory, any setup that passes the gates already scores >= 85,
so the 75 threshold only matters if you later relax a gate (with your approval).
"""
from dataclasses import dataclass, field
from typing import Dict


@dataclass
class ScoreResult:
    total: int
    parts: Dict[str, int] = field(default_factory=dict)
    verdict: str = "IGNORE"       # VALID | WATCHLIST | IGNORE


def compute_score(bias_ok: bool, sweep_ok: bool, zone_conf_ok: bool, indicators_ok: bool,
                  volume_ratio: float, pattern_reliability: int, divergence: bool, cfg) -> ScoreResult:
    parts = {
        "htf_bias": 20 if bias_ok else 0,
        "sweep": 20 if sweep_ok else 0,
        "ob_fvg_confirmation": 20 if zone_conf_ok else 0,
        "rsi_macd_bb": 25 if indicators_ok else 0,
        "volume>1.5x": 10 if volume_ratio > 1.5 else 0,
        "pattern_strength": 5 if pattern_reliability >= 75 else 0,
        "divergence_bonus": 1 if (divergence and indicators_ok) else 0,
    }
    total = min(100, sum(parts.values()))
    if total >= cfg.score_threshold:
        verdict = "VALID"
    elif total >= cfg.watchlist_threshold:
        verdict = "WATCHLIST"
    else:
        verdict = "IGNORE"
    return ScoreResult(total, parts, verdict)
