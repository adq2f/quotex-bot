"""
risk.py  --  Rulebook Section 6 + Section 16 (stateful risk manager; cannot be overridden by strategy).
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from .config import is_otc

CORRELATED = [{"EURUSD", "GBPUSD"}, {"AUDUSD", "NZDUSD"}]


def base_symbol(asset: str) -> str:
    return re.sub(r"[^A-Za-z]", "", asset.lower().replace("_otc", "")).upper()


class RiskManager:
    def __init__(self, cfg, store=None, balance: Optional[float] = None):
        self.cfg, self.store = cfg, store
        saved = store.kv_get("risk_state") if store else None
        if saved:
            self.s = saved
        else:
            b = balance if balance is not None else cfg.virtual_balance
            self.s = {"balance": b, "peak": b, "day": None, "day_start": b, "day_pnl": 0.0, "consec": 0,
                      "cooldown_until": None, "signals_today": 0, "asset_losses": {}, "read_only": False,
                      "recent": []}
        self._save()

    def _save(self):
        if self.store:
            self.store.kv_set("risk_state", self.s)

    @property
    def balance(self) -> float:
        return float(self.s["balance"])

    def set_balance(self, b: float):
        self.s["balance"] = float(b)
        self.s["peak"] = max(self.s["peak"], float(b))
        self._save()

    def _roll_day(self, now: datetime):
        d = now.strftime("%Y-%m-%d")
        if self.s["day"] != d:
            self.s.update(day=d, day_start=self.s["balance"], day_pnl=0.0, signals_today=0, asset_losses={})
            self._save()

    def cooldown_active(self, now: datetime) -> Optional[datetime]:
        cu = self.s.get("cooldown_until")
        if cu:
            cu = datetime.fromisoformat(cu)
            if now < cu:
                return cu
        return None

    def can_open(self, asset: str, direction: str, now: datetime, payout_pct: float,
                 pending: int = 0, is_mtg: bool = False) -> Tuple[bool, str]:
        c = self.cfg
        self._roll_day(now)
        if self.s["read_only"]:
            return False, "READ-ONLY (drawdown guard >15%) [6.9]"
        if self.s["day_pnl"] <= -c.daily_loss_pct / 100.0 * self.s["day_start"]:
            return False, "Daily-limit reached [6.3]"
        if c.stop_on_daily_profit and self.s["day_pnl"] >= c.daily_profit_pct / 100.0 * self.s["day_start"]:
            return False, "Daily profit target reached [6.4]"
        cu = self.cooldown_active(now)
        if cu:
            return False, f"Cooldown until {cu.strftime('%H:%M')} UTC [6.2]"
        if pending >= c.max_concurrent_trades:
            return False, "Trade already open [6.7]"
        if payout_pct < c.min_payout_pct:
            return False, f"Payout {payout_pct:.0f}% < {c.min_payout_pct:.0f}% [6.10]"
        
        if not is_mtg:
            if self.s["signals_today"] >= c.max_signals_per_day:
                return False, "Max signals/day reached"
            if self.s["asset_losses"].get(asset, 0) >= c.asset_loss_lock:
                return False, f"{asset} locked today ({c.asset_loss_lock} losses)"
            
            sym = base_symbol(asset)
            for ts, a2, d2 in self.s["recent"]:
                t = datetime.fromisoformat(ts)
                # ১৫ মিনিটের কুলডাউন
                if (now - t) <= timedelta(minutes=15) and d2 == direction and a2 != sym:
                    for grp in CORRELATED:
                        if sym in grp and a2 in grp:
                            return False, "Correlated pair signalled <15 min ago [6.8]"
        return True, "Normal"

    def base_stake(self, asset: str, payout_pct: float) -> float:
        c = self.cfg
        risk = c.mtg_base_risk_pct if c.mtg_enabled else c.max_risk_pct
        stake = self.balance * risk / 100.0
        if is_otc(asset):
            stake *= c.otc_size_multiplier
        stake = min(stake, self.balance * 0.02)
        return round(max(stake, c.stake_min), 2)

    def mtg_stake(self, base_stake: float, payout_pct: float) -> Optional[float]:
        p = payout_pct / 100.0
        m = round(base_stake * (1 + p) / p, 2)
        if base_stake + m > self.balance * 0.02 + 1e-9:
            return None
        return m

    def register_signal(self, asset: str, direction: str, now: datetime, is_mtg: bool = False):
        self._roll_day(now)
        if not is_mtg:
            self.s["signals_today"] += 1
        rec = [r for r in self.s["recent"] if (now - datetime.fromisoformat(r[0])) <= timedelta(minutes=15)]
        rec.append([now.isoformat(), base_symbol(asset), direction])
        self.s["recent"] = rec
        self._save()

    def record_result(self, asset: str, outcome: str, pnl: float, now: datetime):
        c = self.cfg
        self._roll_day(now)
        self.s["balance"] += pnl
        self.s["day_pnl"] += pnl
        self.s["peak"] = max(self.s["peak"], self.s["balance"])
        if outcome == "LOSS":
            self.s["consec"] += 1
            self.s["asset_losses"][asset] = self.s["asset_losses"].get(asset, 0) + 1
            if self.s["consec"] >= c.max_consec_losses:
                self.s["cooldown_until"] = (now + timedelta(minutes=c.cooldown_minutes)).isoformat()
                self.s["consec"] = 0
        elif outcome == "WIN":
            self.s["consec"] = 0
        dd = (self.s["peak"] - self.s["balance"]) / self.s["peak"] * 100.0 if self.s["peak"] else 0.0
        if dd > c.drawdown_guard_pct:
            self.s["read_only"] = True
        self._save()

    def reset_read_only(self):
        self.s["read_only"] = False
        self.s["peak"] = self.s["balance"]
        self._save()

    def snapshot(self, now: datetime) -> Dict:
        cu = self.cooldown_active(now)
        return {"balance": round(self.balance, 2), "day_pnl": round(self.s["day_pnl"], 2), "consec_losses": self.s["consec"],
                "cooldown_until": cu.isoformat() if cu else None, "read_only": self.s["read_only"],
                "signals_today": self.s["signals_today"], "asset_losses": self.s["asset_losses"]}
