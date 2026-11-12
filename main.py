"""
main.py  --  entry point. Flask web app + bot thread (paused until web login).
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import threading
import time
from collections import Counter
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import pandas as pd

from bot.chart import cleanup_old, render_result_chart, render_signal_chart
from bot.config import settings, validate
from bot.datafeed import make_source
from bot.executor import Executor
from bot.logger import log
from bot.news import NewsFilter
from bot.risk import RiskManager
from bot.store import Store
from bot.strategy import Decision, Strategy, section9_text
from bot.telegram_out import Telegram
from bot.tracker import Signal, Tracker, outcome_for, pnl_for
from bot.controller import BotController
from bot.webapp import create_app


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Bot:
    def __init__(self, cfg=settings, source=None, telegram=None):
        self.cfg = cfg
        self.store = Store(cfg.db_path)
        self.source = source or make_source(cfg, self.store)
        self.tg = telegram or Telegram(cfg)
        self.news = NewsFilter(cfg) if cfg.news_enabled else None
        self.strategy = Strategy(cfg, self.news)
        self.risk = RiskManager(cfg, self.store)
        self.tracker = Tracker(self.store)
        self.executor = Executor(cfg, self.source)
        self.diag: Counter = Counter()
        self._htf: Dict[tuple, pd.DataFrame] = {}
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()
        os.makedirs(cfg.charts_dir, exist_ok=True)

    def _tz(self) -> int:
        return self.cfg.display_utc_offset_hours

    def _bd(self, iso_or_dt) -> str:
        t = pd.Timestamp(iso_or_dt)
        if t.tzinfo is None:
            t = t.tz_localize("UTC")
        return (t + pd.Timedelta(hours=self._tz())).strftime("%H:%M:%S")

    def _expiry_text(self) -> str:
        mins = self.cfg.expiry_candles * self.cfg.entry_tf_seconds // 60
        return f"{mins} min"

    def _frames(self, asset: str, now: datetime):
        cfg = self.cfg
        m5 = self.source.get_candles(asset, cfg.entry_tf_seconds, 300, now)
        out = []
        for tf in cfg.bias_tf_seconds[::-1]:
            key = (asset, tf, int(now.timestamp() // tf))
            if key not in self._htf:
                self._htf = {k: v for k, v in self._htf.items() if not (k[0] == asset and k[1] == tf)}
                self._htf[key] = self.source.get_candles(asset, tf, 300, now)
            out.append(self._htf[key])
        return m5, out[0], out[1]

    def _payout(self, asset: str) -> float:
        p = self.source.get_payout(asset)
        return float(p) if p else self.cfg.default_payout_pct

    def _viz(self, d: Decision, m5: pd.DataFrame) -> Dict:
        s = d.setup
        if s is None:
            return {}
        t = lambda i: m5.index[i].isoformat() if i is not None and 0 <= i < len(m5) else None
        return {"zone": list(s.zone), "ob": list(s.ob), "ob_time": t(s.ob_idx), "fvg": list(s.fvg) if s.fvg else None,
                "fvg_time": t(s.fvg_idx), "sweep_level": s.sweep_level, "sweep_time": t(s.sweep_idx)}

    def _send_signal(self, sig: Signal, m5: pd.DataFrame, section9: Optional[str]):
        path = os.path.join(self.cfg.charts_dir, f"sig_{sig.id}.png")
        info = {"asset": sig.asset, "direction": sig.direction, "score": sig.score, "step": sig.step,
                "tf_seconds": sig.tf_seconds, "entry_price": sig.entry_price,
                "expiry_text": f"expiry {self._expiry_text()}", "viz": sig.viz}
        try:
            render_signal_chart(m5, info, path, self._tz())
        except Exception as e:
            log("chart_failed", error=str(e)[:200])
            path = ""
        tag = f"🔁 <b>MTG{sig.step}</b> " if sig.step else "📊 "
        side = "🟢 CALL (BUY)" if sig.direction == "BUY" else "🔴 PUT (SELL)"
        cap = (f"{tag}<b>{sig.asset}</b>  {side}\n"
               f"Entry: <b>NOW</b> @ {sig.entry_price:.5f}  ({self._bd(sig.entry_time)} BD)\n"
               f"Expiry: {self._expiry_text()} → {self._bd(sig.expiry_time)} BD\n"
               f"Stake: ${sig.stake:.2f}   Score: {sig.score}   ID: {sig.id}")
        if path and os.path.exists(path):
            self.tg.photo(path, cap)
        else:
            self.tg.message(cap)
        if section9:
            self.tg.message(section9, pre=True)

    def _send_result(self, sig: Signal, m5: pd.DataFrame, outcome: str, pnl: float):
        path = os.path.join(self.cfg.charts_dir, f"res_{sig.id}.png")
        info = {"asset": sig.asset, "direction": sig.direction, "entry_price": sig.entry_price,
                "entry_time": sig.entry_time, "step": sig.step, "tf_seconds": sig.tf_seconds}
        try:
            render_result_chart(m5, info, sig.exit_price, outcome, pnl, path, self._tz())
        except Exception as e:
            log("chart_failed", error=str(e)[:200])
            path = ""
        icon = {"WIN": "✅", "LOSS": "❌", "DRAW": "➖"}.get(outcome, "")
        cap = (f"{icon} <b>{outcome}</b>  {sig.asset}  {'MTG%d ' % sig.step if sig.step else ''}\n"
               f"P/L: <b>{('+$%.2f' % pnl) if pnl > 0 else (('-$%.2f' % abs(pnl)) if pnl < 0 else '$0.00')}</b>   Balance: ${self.risk.balance:.2f}\n"
               f"Entry {sig.entry_price:.5f} → Exit {sig.exit_price:.5f}   ID: {sig.id}")
        if path and os.path.exists(path):
            self.tg.photo(path, cap)
        else:
            self.tg.message(cap)

    def resolve_due(self, now: datetime):
        cfg = self.cfg
        for sig in self.tracker.due(now):
            m5 = self.source.get_candles(sig.asset, cfg.entry_tf_seconds, 120, now)
            exp = pd.Timestamp(sig.expiry_time) - pd.Timedelta(seconds=cfg.entry_tf_seconds)
            if exp not in m5.index:
                sig.tries += 1
                if sig.tries >= 3:
                    self.tracker.close(sig, "VOID")
                    self.tg.message(f"⚠️ {sig.asset} {sig.id}: expiry candle not available → VOID (not counted).")
                else:
                    self.tracker.update(sig)
                continue
            m5 = m5.loc[:exp]
            exit_price = float(m5["close"].iloc[-1])
            outcome = outcome_for(sig.direction, sig.entry_price, exit_price)
            pnl = pnl_for(outcome, sig.stake, sig.payout_pct)
            self.risk.record_result(sig.asset, outcome, pnl, now)
            self.tracker.close(sig, outcome, exit_price, pnl)
            log("result", id=sig.id, asset=sig.asset, outcome=outcome, pnl=pnl, step=sig.step)
            self._send_result(sig, m5, outcome, pnl)
            if outcome == "LOSS":
                self._try_mtg(sig, now)

    def _try_mtg(self, sig: Signal, now: datetime):
        cfg = self.cfg
        if not cfg.mtg_enabled or sig.step >= cfg.mtg_max_steps:
            return
        m5, h1, h4 = self._frames(sig.asset, now)
        ok, why = self.strategy.mtg_precheck(sig.asset, sig.direction, m5, h1, h4, now)
        if not ok:
            self.tg.message(f"⏭ MTG skipped for {sig.asset}: {why}")
            return
        payout = self._payout(sig.asset)
        ok, status = self.risk.can_open(sig.asset, sig.direction, now, payout,
                                        pending=len(self.tracker.pending), is_mtg=True)
        if not ok:
            self.tg.message(f"⏭ MTG skipped for {sig.asset}: {status}")
            return
        base = sig.base_stake or sig.stake
        stake = self.risk.mtg_stake(base, payout)
        if stake is None:
            self.tg.message(f"⏭ MTG skipped for {sig.asset}: sequence risk would exceed 2% of balance [16.2]")
            return
        entry_time = (m5.index[-1] + pd.Timedelta(seconds=cfg.entry_tf_seconds))
        expiry = entry_time + pd.Timedelta(seconds=cfg.entry_tf_seconds * cfg.expiry_candles)
        ms = Signal(asset=sig.asset, direction=sig.direction,
                    entry_time=entry_time.isoformat(), expiry_time=expiry.isoformat(),
                    entry_price=float(m5["close"].iloc[-1]), stake=stake, payout_pct=payout,
                    tf_seconds=cfg.entry_tf_seconds, step=sig.step + 1, parent_id=sig.id,
                    base_stake=base, score=sig.score, viz=sig.viz)
        self.tracker.add(ms)
        self.risk.register_signal(sig.asset, sig.direction, now, is_mtg=True)
        self.executor.execute(ms)
        self._send_signal(ms, m5, None)
        log("mtg_signal", id=ms.id, parent=sig.id, stake=stake)

    def reconnect_source(self):
        """Rebuild data source with the latest cookie and reset HTF cache."""
        from bot.datafeed import make_source
        try:
            self.source.close()
        except Exception:
            pass
        self.source = make_source(self.cfg, self.store)
        self.executor.source = self.source
        self._htf = {}
        log("source_reconnected")

    def scan(self, now: datetime):
        cfg = self.cfg
        if self.tracker.pending:
            return
        cands = []
        for asset in cfg.whitelist:
            try:
                m5, h1, h4 = self._frames(asset, now)
                d = self.strategy.evaluate(asset, m5, h1, h4, now)
            except Exception as e:
                log("scan_error", asset=asset, error=str(e)[:200])
                continue
            self.diag[d.gate or d.decision] += 1
            self.store.log_decision(asset, d.decision, d.score, d.gate, d.to_dict())
            if d.decision != "HOLD":
                cands.append((d, m5))
        if not cands:
            return
        # Rank by score × (payout / 100) — prefers high-confidence AND high-payout setups
        d, m5 = max(cands, key=lambda x: x[0].score * (self._payout(x[0].asset) / 100.0))
        age = (utcnow() - d.candle_close).total_seconds()
        if age > cfg.signal_max_age_seconds:
            log("signal_stale", asset=d.asset, age=round(age, 1))
            self.diag["stale_signal"] += 1
            return
        payout = self._payout(d.asset)
        ok, status = self.risk.can_open(d.asset, d.decision, now, payout, pending=0)
        d.payout_pct, d.risk_status = payout, status
        cu = self.risk.cooldown_active(now)
        d.cooldown = f"Active until {cu.strftime('%H:%M')} UTC" if cu else "Not active"
        if not ok:
            log("risk_block", asset=d.asset, status=status)
            self.diag["risk_block"] += 1
            return
        d.stake = self.risk.base_stake(d.asset, payout)
        entry_time = d.candle_close
        expiry = entry_time + timedelta(seconds=cfg.entry_tf_seconds * cfg.expiry_candles)
        sig = Signal(asset=d.asset, direction=d.decision,
                     entry_time=entry_time.isoformat(), expiry_time=expiry.isoformat(),
                     entry_price=d.entry_price, stake=d.stake, payout_pct=payout,
                     tf_seconds=cfg.entry_tf_seconds, base_stake=d.stake, score=d.score,
                     viz=self._viz(d, m5), notes=d.notes)
        self.tracker.add(sig)
        self.risk.register_signal(d.asset, d.decision, now)
        sig.executed = self.executor.execute(sig)
        self._send_signal(sig, m5, section9_text(d, self._tz(), self._expiry_text()))
        log("signal", id=sig.id, asset=d.asset, direction=d.decision, score=d.score, stake=d.stake)

    def summary_numbers(self, since_iso: Optional[str] = None) -> Dict:
        rows = self.store.signal_rows(since_iso)
        def agg(rs):
            w = sum(r["status"] == "WIN" for r in rs)
            l = sum(r["status"] == "LOSS" for r in rs)
            gp = sum(r["pnl"] for r in rs if r["pnl"] > 0)
            gl = -sum(r["pnl"] for r in rs if r["pnl"] < 0)
            return {"win": w, "loss": l, "draw": sum(r["status"] == "DRAW" for r in rs),
                    "win_rate": round(100 * w / (w + l), 1) if (w + l) else None,
                    "profit_factor": round(gp / gl, 2) if gl else None,
                    "pnl": round(sum(r["pnl"] for r in rs), 2)}
        return {"all": agg(rows),
                "base": agg([r for r in rows if r["step"] == 0]),
                "mtg": agg([r for r in rows if r["step"] > 0])}

    def daily_report(self, now: datetime):
        cfg = self.cfg
        today = now.strftime("%Y-%m-%d")
        if now.hour < cfg.daily_report_hour_utc or self.store.kv_get("last_report") == today:
            return
        self.store.kv_set("last_report", today)
        s = self.summary_numbers(today)
        def line(n, a):
            return f"{n}: W{a['win']} L{a['loss']} D{a['draw']}  WR {a['win_rate'] if a['win_rate'] is not None else '-'}%  P/L ${a['pnl']}"
        txt = (f"📈 <b>Daily report {today}</b>\n{line('Total', s['all'])}\n{line('Base', s['base'])}\n{line('MTG ', s['mtg'])}\n"
               f"Balance: ${self.risk.balance:.2f}   Top blocker: {self.diag.most_common(1)[0][0] if self.diag else '-'}")
        self.tg.message(txt)

    def cycle(self, now: Optional[datetime] = None):
        now = now or utcnow()
        if self.cfg.execution_mode == "execute":
            b = self.source.get_balance()
            if b:
                self.risk.set_balance(b)
        self.resolve_due(now)
        if not self.pause_event.is_set():
            self.scan(now)
        self.daily_report(now)
        cleanup_old(self.cfg.charts_dir)

    def run_forever(self):
        tf = self.cfg.entry_tf_seconds
        for w in validate():
            log("config_warning", msg=w)
        self.tg.message(
            "🤖 Bot thread online | mode: %s | account: %s | MTG: %s | assets: %s\n"
            "⏸ Waiting for web login to begin scanning." %
            (self.cfg.execution_mode, self.cfg.account_type,
             "ON" if self.cfg.mtg_enabled else "OFF", ", ".join(self.cfg.whitelist))
        )
        log("bot_loop_started")
        while not self.stop_event.is_set():
            now = time.time()
            nxt = (int(now // tf) + 1) * tf + self.cfg.scan_delay_seconds
            while time.time() < nxt and not self.stop_event.is_set():
                time.sleep(min(1.0, max(0.0, nxt - time.time())))
            if self.stop_event.is_set():
                break
            try:
                self.cycle()
            except Exception as e:
                log("cycle_error", error=str(e)[:300])
        log("bot_loop_exited")


def main():
    bot = Bot()
    controller = BotController(bot)
    controller.start_thread()
    app = create_app(bot, controller)
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)


if __name__ == "__main__":
    main()
