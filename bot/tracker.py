"""
tracker.py  --  signal lifecycle (Section 15): PENDING -> WIN / LOSS / DRAW (/ VOID), plus MTG chain linkage.
CALL wins if expiry close > entry price; PUT wins if <; equal = DRAW (refund).
"""
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple


@dataclass
class Signal:
    asset: str
    direction: str                 # BUY | SELL
    entry_time: str                # ISO UTC (= signal candle close)
    expiry_time: str
    entry_price: float
    stake: float
    payout_pct: float
    tf_seconds: int = 300
    step: int = 0                  # 0 = base signal, 1 = MTG1
    parent_id: Optional[str] = None
    base_stake: float = 0.0        # stake of step 0 (for MTG sizing)
    score: int = 0
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "PENDING"
    exit_price: float = 0.0
    pnl: float = 0.0
    tries: int = 0
    viz: Dict = field(default_factory=dict)
    notes: str = ""
    executed: bool = False

    def to_dict(self) -> Dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: Dict) -> "Signal":
        return Signal(**{k: v for k, v in d.items() if k in Signal.__dataclass_fields__})


def outcome_for(direction: str, entry: float, exit_: float) -> str:
    if exit_ == entry:
        return "DRAW"
    win = exit_ > entry if direction == "BUY" else exit_ < entry
    return "WIN" if win else "LOSS"


def pnl_for(outcome: str, stake: float, payout_pct: float) -> float:
    if outcome == "WIN":
        return round(stake * payout_pct / 100.0, 2)
    if outcome == "LOSS":
        return round(-stake, 2)
    return 0.0


class Tracker:
    def __init__(self, store=None):
        self.store = store
        self.pending: List[Signal] = []
        if store:
            self.pending = [Signal.from_dict(d) for d in store.load_pending()]

    def add(self, sig: Signal):
        self.pending.append(sig)
        if self.store:
            self.store.save_signal(sig.to_dict())

    def due(self, now: datetime) -> List[Signal]:
        return [s for s in self.pending if datetime.fromisoformat(s.expiry_time) <= now]

    def close(self, sig: Signal, status: str, exit_price: float = 0.0, pnl: float = 0.0):
        sig.status, sig.exit_price, sig.pnl = status, exit_price, pnl
        self.pending = [s for s in self.pending if s.id != sig.id]
        if self.store:
            self.store.save_signal(sig.to_dict())

    def update(self, sig: Signal):
        if self.store:
            self.store.save_signal(sig.to_dict())
