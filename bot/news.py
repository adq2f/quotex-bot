"""
news.py  --  Rulebook Section 7.1/7.2: block 15 min before and after high-impact news.
Default feed: public weekly ForexFactory-style JSON (NEWS_CALENDAR_URL). Fail policy (NEWS_FAIL_POLICY):
  block (default, capital-preserving): if the calendar has never loaded, trading is blocked.
  allow : trade without news data (NOT recommended).
"""
import re
import threading
import time
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

import pandas as pd
import requests

from .logger import log


def asset_currencies(asset: str) -> List[str]:
    sym = re.sub(r"[^A-Za-z]", "", asset.lower().replace("_otc", "")).upper()
    return [sym[:3], sym[3:6]] if len(sym) >= 6 else [sym]


class NewsFilter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.events: List[Tuple[pd.Timestamp, str, str]] = []   # (time, currency, title)
        self.loaded_at: Optional[float] = None
        self.last_try: float = 0.0
        self._lock = threading.Lock()

    def _refresh(self):
        now = time.time()
        stale = self.loaded_at is None or now - self.loaded_at > 3600
        if not stale or now - self.last_try < 300:
            return
        self.last_try = now
        try:
            r = requests.get(self.cfg.news_calendar_url, timeout=10, headers={"User-Agent": "quotex-bot/2.1"})
            r.raise_for_status()
            ev = []
            for x in r.json():
                if str(x.get("impact", "")).lower() != "high":
                    continue
                ev.append((pd.Timestamp(x["date"]).tz_convert("UTC"), str(x.get("country", "")).upper(), str(x.get("title", ""))))
            with self._lock:
                self.events, self.loaded_at = ev, now
            log("news_loaded", count=len(ev))
        except Exception as e:
            log("news_fetch_failed", error=str(e)[:200])

    def check(self, asset: str, now: datetime) -> Tuple[bool, str]:
        """Returns (blocked, label). label = 'Clear' or 'Blocked (event, time)'."""
        self._refresh()
        if self.loaded_at is None:
            if self.cfg.news_fail_policy == "allow":
                return False, "Clear (calendar unavailable, policy=allow)"
            return True, "Blocked (calendar unavailable)"
        cur = set(asset_currencies(asset))
        t = pd.Timestamp(now)
        b, a = timedelta(minutes=self.cfg.news_block_before_min), timedelta(minutes=self.cfg.news_block_after_min)
        with self._lock:
            for et, ccy, title in self.events:
                if ccy in cur and et - b <= t <= et + a:
                    return True, f"Blocked ({title[:24]}, {et.strftime('%H:%M')} UTC)"
        return False, "Clear"
