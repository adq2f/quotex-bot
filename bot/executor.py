"""
executor.py  --  trade execution (Section 10/14.8). DEFAULT = signal_only (nothing is ever placed).
Execution requires EXECUTION_MODE=execute, and on a REAL account additionally ALLOW_REAL_EXECUTION=true
(only after the Section 12 backtest + 2 weeks DEMO validation).
"""
from .logger import log


class Executor:
    def __init__(self, cfg, source):
        self.cfg, self.source = cfg, source

    def execute(self, sig) -> bool:
        c = self.cfg
        if c.execution_mode != "execute":
            return False
        if c.account_type != "DEMO" and not c.allow_real_execution:
            log("execution_refused", reason="REAL account without ALLOW_REAL_EXECUTION")
            return False
        try:
            expiry = int(c.expiry_candles * c.entry_tf_seconds)
            ok = self.source.buy(sig.asset, "CALL" if sig.direction == "BUY" else "PUT", sig.stake, expiry)
            log("trade_placed", id=sig.id, asset=sig.asset, ok=ok, stake=sig.stake)
            return bool(ok)
        except Exception as e:
            log("execution_failed", error=str(e)[:200])
            return False
