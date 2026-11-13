"""quotex_feed.py -- optional LIVE Quotex feed (pyquotex, incl. OTC). Falls back to biquote if login fails
(Cloudflare 403, library missing, wrong password). Quotex login only works from IPs Cloudflare accepts."""
import asyncio
import os
import threading

import pandas as pd

from .datafeed import COLS, BiQuoteSource, CandleSource, drop_unclosed, utcnow, make_source as _base_make
from .logger import log

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/120.0.0.0 Safari/537.36")


class QuotexSource(CandleSource):
    label = "Quotex live"

    def __init__(self, email, password, demo=True, proxy=None):
        from pyquotex.stable_api import Quotex  # not in requirements.txt on purpose (optional)
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        kw = {"proxies": {"http": proxy, "https": proxy}} if proxy else {}
        self.client = Quotex(email=email, password=password, **kw)
        cookie = os.getenv("QUOTEX_COOKIE", "")
        if cookie:
            try:
                self.client.set_session(user_agent=UA, cookies=cookie)
            except Exception as e:
                log("quotex_cookie_failed", error=str(e)[:200])
        ok, reason = self._run(self.client.connect(), 60)
        if not ok:
            raise RuntimeError(f"Quotex connect failed: {reason}")
        self.client.change_account("PRACTICE" if demo else "REAL")

    def _run(self, coro, timeout=25):
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def _fetch(self, asset, tf, count, now):
        raw = self._run(self.client.get_candles(asset, int(now.timestamp()), tf * count, tf)) or []
        if not raw:
            return pd.DataFrame(columns=COLS)
        df = pd.DataFrame(raw)
        df.index = pd.to_datetime(df["time"], unit="s", utc=True)
        if "volume" not in df.columns:
            df["volume"] = 0.0
        df = df[COLS].astype(float).sort_index()
        return df[~df.index.duplicated(keep="last")]

    def get_candles(self, asset, tf_seconds, count, now=None):
        now = now or utcnow()
        return drop_unclosed(self._fetch(asset, tf_seconds, count, now), tf_seconds, now).iloc[-count:]

    def get_candles_peek(self, asset, tf_seconds, count, now=None):
        return self._fetch(asset, tf_seconds, count, now or utcnow()).iloc[-count:]

    def get_payout(self, asset):
        try:
            p = self._run(self.client.get_payment())
            v = p.get(asset, {}).get("payment") or p.get(asset, {}).get("turbo_payment")
            return float(v) if v is not None else None
        except Exception:
            return None

    def close(self):
        try:
            self._run(self.client.close())
        except Exception:
            pass


def make_source(cfg, store=None):
    if cfg.data_source == "quotex":
        try:
            src = QuotexSource(cfg.quotex_email, cfg.quotex_password, cfg.account_type == "DEMO",
                               os.getenv("QUOTEX_PROXY") or None)
            log("quotex_connected")
            return src
        except Exception as e:
            log("quotex_unavailable_fallback_biquote", error=str(e)[:200])
            src = BiQuoteSource()
            src.label = "Real market (biquote)"
            return src
    return _base_make(cfg, store)
