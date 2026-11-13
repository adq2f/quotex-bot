"""
main.py  --  Web app + bot thread. Signals via Telegram + dashboard.
"""
import sys
import os
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Dict

import pandas as pd

from bot import features as fx
from bot.chart import cleanup_old, render_result_chart, render_signal_chart
from bot.config import settings, validate, strategy_for_timeframe
from bot.quotex_feed import make_source
from bot.executor import Executor
from bot.logger import log
from bot.news import NewsFilter
from bot.risk import RiskManager
from bot.store import Store
from bot.strategy import Strategy
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
        self.source = source
        
        # --- NEW VERSION LOG ---
        log("BOT_STARTUP", VERSION="2024-11-13-V4-FINAL")
        
        if self.source is None:
            try:
                self.source = make_source(cfg, self.store)
                log("source_initialized", feed=self.feed_label())
            except Exception as e:
                log("source_init_failed", error=str(e)[:300])
                self.source = None

        self.tg = telegram or Telegram(cfg)
        self.news = NewsFilter(cfg) if cfg.news_enabled else None
        self.risk = RiskManager(cfg, self.store)
        self.tracker = Tracker(self.store)
        self.executor = Executor(cfg, self.source)
        self.diag: Counter = Counter()
        self._htf: Dict[tuple, pd.DataFrame] = {}
        self.pause_event = threading.Event()
        self.pause_event.set()
        self.stop_event = threading.Event()
        self._pre_scan_candidate = None
        
        # ডাটাবেস থেকে টাইমার লোড করা (যাতে রিস্টার্টেও মুছে না যায়)
        self.last_signal_time = float(self.store.kv_get("last_signal_time", 0))
        self.last_result_time = float(self.store.kv_get("last_result_time", 0))
        
        # আটকে থাকা সিগন্যাল ক্লিয়ার
        if self.tracker.pending:
            for s in list(self.tracker.pending):
                self.tracker.close(s, "VOID")
                
        os.makedirs(cfg.charts_dir, exist_ok=True)
        self.strategy = None
        self._build_strategy()

    def feed_label(self) -> str:
        return getattr(self.source, "label", "Real market (biquote)") if self.source else "none"

    def _build_strategy(self):
        store = self.store
        tf = int(store.get_setting("web_timeframe_seconds", self.cfg.web_timeframe_seconds) or 300)
        if tf not in (60, 300, 900, 3600):
            tf = 300
        mode = strategy_for_timeframe(tf)
        mtg = bool(store.get_setting("mtg_enabled", self.cfg.mtg_enabled))
        max_sig = int(store.get_setting("max_signals_per_day", self.cfg.max_signals_per_day))
        
        self.cfg = replace(self.cfg, web_timeframe_seconds=tf, entry_tf_seconds=tf,
                           strategy_mode=mode, mtg_enabled=mtg, max_signals_per_day=max_sig)
        self.risk.cfg = self.cfg
        self.executor.cfg = self.cfg
        if mode == "quick":
            from bot.strategy_quick import QuickStrategy
            self.strategy = QuickStrategy(self.cfg, self.news)
        else:
            self.strategy = Strategy(self.cfg, self.news)
        fx.attach_precheck(self.strategy)

    def _tz(self): return self.cfg.display_utc_offset_hours

    def _expiry_text(self):
        return f"{self.cfg.expiry_candles * self.cfg.entry_tf_seconds // 60} min"

    def _frames(self, asset, now):
        cfg = self.cfg
        if self.source is None:
            empty = pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
            return empty, empty, empty
        m5 = self.source.get_candles(asset, cfg.entry_tf_seconds, 300, now)
        out = []
        for tf in cfg.bias_tf_seconds[::-1]:
            key = (asset, tf, int(now.timestamp() // tf))
            if key not in self._htf:
                self._htf = {k: v for k, v in self._htf.items() if not (k[0] == asset and k[1] == tf)}
                self._htf[key] = self.source.get_candles(asset, tf, 300, now)
            out.append(self._htf[key])
        return m5, out[0], out[1]

    def _payout(self, asset):
        if self.source is None:
            return self.cfg.default_payout_pct
        p = self.source.get_payout(asset)
        return float(p) if p else self.cfg.default_payout_pct

    def _viz(self, d, m5):
        s = getattr(d, "setup", None)
        if s is None: return {}
        t = lambda i: m5.index[i].isoformat() if i is not None and 0 <= i < len(m5) else None
        return {"zone": list(s.zone), "ob": list(s.ob), "ob_time": t(s.ob_idx),
                "fvg": list(s.fvg) if s.fvg else None, "fvg_time": t(s.fvg_idx),
                "sweep_level": s.sweep_level, "sweep_time": t(s.sweep_idx)}

    def _send_signal(self, sig, m5):
        path = os.path.join(self.cfg.charts_dir, f"sig_{sig.id}.png")
        info = {"asset": sig.asset, "direction": sig.direction, "score": sig.score, "step": sig.step,
                "tf_seconds": sig.tf_seconds, "entry_price": sig.entry_price,
                "expiry_text": f"expiry {self._expiry_text()}", "viz": sig.viz}
        try:
            render_signal_chart(m5, info, path, self._tz())
        except Exception as e:
            log("chart_failed", error=str(e)[:200])
            path = ""
            
        line = "🟢⬆️  UP  (BUY / CALL)" if sig.direction == "BUY" else "🔴⬇️  DOWN  (SELL / PUT)"
        
        score = getattr(sig, "score", 0)
        if score >= 85:
            signal_type = "🔥🔥 HIGH ACCURACY SIGNAL ✅ 🔥🔥"
        else:
            signal_type = "⚡ NORMAL SIGNAL ⚡"
            
        feed = self.feed_label()
        mtg_tag = f"  ·  MTG{sig.step}" if sig.step else ""
        
        cap = (f"📢 SIGNAL{mtg_tag}\n"
               f"<b>{signal_type}</b>\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"<b>{line}</b>\n"
               f"Pair: <b>{sig.asset}</b>\n"
               f"Timeframe: {self.cfg.entry_tf_seconds // 60} min\n"
               f"Entry: <b>NOW</b> @ {sig.entry_price:.5f}\n"
               f"Expiry: {self._expiry_text()}\n"
               f"Confidence: <b>{sig.score}%</b>\n"
               f"Stake: ${sig.stake:.2f}\n"
               f"Feed: {feed}\n"
               f"━━━━━━━━━━━━━━━━━━\n🆔 {sig.id}")
               
        if path and os.path.exists(path):
            self.tg.photo(path, "")
            time.sleep(1) # ১ সেকেন্ড গ্যাপ
            self.tg.message(cap)
        else:
            self.tg.message(cap)

    def _send_result(self, sig, m5, outcome, pnl):
        path = os.path.join(self.cfg.charts_dir, f"res_{sig.id}.png")
        info = {"asset": sig.asset, "direction": sig.direction, "entry_price": sig.entry_price,
                "entry_time": sig.entry_time, "step": sig.step, "tf_seconds": sig.tf_seconds}
        try:
            render_result_chart(m5, info, sig.exit_price, outcome, pnl, path, self._tz())
        except Exception as e:
            log("chart_failed", error=str(e)[:200])
            path = ""
        header = {"WIN": "✅ WIN", "LOSS": "❌ LOSS"}.get(outcome, "➖ DRAW")
        pnl_str = f"+${pnl:.2f}" if pnl > 0 else (f"-${abs(pnl):.2f}" if pnl < 0 else "$0.00")
        cap = (f"<b>{header}</b>\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"Pair: <b>{sig.asset}</b>\n"
               f"Result: <b>{pnl_str}</b>\n"
               f"Balance: ${self.risk.balance:.2f}\n"
               f"━━━━━━━━━━━━━━━━━━\n🆔 {sig.id}")
               
        if path and os.path.exists(path):
            self.tg.photo(path, "")
            time.sleep(1)
            self.tg.message(cap)
        else:
            self.tg.message(cap)

    def _pre_scan(self, now):
        cfg = self.cfg
        self._pre_scan_candidate = None
        if cfg.prepare_alert_seconds <= 0 or self.source is None or self.pause_event.is_set():
            return
        if self.tracker.pending:
            return
        
        # ৩ মিনিট গ্যাপ চেক
        if (time.time() - self.last_signal_time) < 180 or (time.time() - self.last_result_time) < 180:
            return
            
        peek = getattr(self.source, "get_candles_peek", None)
        if peek is None: return
        bad = fx.bad_assets(self)
        candidates = []
        for asset in cfg.whitelist:
            if asset in bad: continue
            try:
                m5 = peek(asset, cfg.entry_tf_seconds, 300, now)
                if len(m5) < cfg.min_entry_candles: continue
                h1 = self.source.get_candles(asset, 3600, 300, now)
                h4 = self.source.get_candles(asset, 14400, 300, now)
                if len(h1) < 50 or len(h4) < 50: continue
                d = self.strategy.evaluate(asset, m5, h1, h4, now)
                if d.decision == "HOLD" or d.score < cfg.prepare_min_score: continue
                entry_time = d.candle_close or now
                if entry_time <= now: continue
                candidates.append((d, entry_time, m5))
            except Exception as e:
                pass
        candidates.sort(key=lambda x: x[0].score, reverse=True)
        
        if not candidates: return
        top = candidates[0]
        d, entry_time, m5_peek = top
        self._pre_scan_candidate = (d.asset, d.decision)
        
        path = os.path.join(self.cfg.charts_dir, f"watch_{d.asset}.png")
        info = {"asset": d.asset, "direction": d.decision, "score": d.score, "step": 0,
                "tf_seconds": cfg.entry_tf_seconds, "entry_price": d.entry_price,
                "expiry_text": f"expiry {self._expiry_text()}", "viz": self._viz(d, m5_peek)}
        try:
            render_signal_chart(m5_peek, info, path, self._tz())
        except Exception as e:
            path = ""
            
        cap = f"👀 <b>Watch Market:</b> {d.asset}\n<i>Analyzing setup... Wait for 5s direction.</i>"
        
        if path and os.path.exists(path):
            self.tg.photo(path, cap)
        else:
            self.tg.message(cap)

    def resolve_due(self, now):
        cfg = self.cfg
        if self.source is None: return
        for sig in self.tracker.due(now):
            try:
                m5 = self.source.get_candles(sig.asset, cfg.entry_tf_seconds, 10, now)
                if len(m5) == 0: continue
                
                exit_price = float(m5["close"].iloc[-1])
                outcome = outcome_for(sig.direction, sig.entry_price, exit_price)
                pnl = pnl_for(outcome, sig.stake, sig.payout_pct)
                self.risk.record_result(sig.asset, outcome, pnl, now)
                self.tracker.close(sig, outcome, exit_price, pnl)
                
                # রেজাল্ট টাইম ডাটাবেসে সেভ
                self.last_result_time = time.time()
                self.store.kv_set("last_result_time", self.last_result_time)
                
                log("RESULT_SENT", id=sig.id, asset=sig.asset, outcome=outcome, pnl=pnl)
                self._send_result(sig, m5, outcome, pnl)
                
                created = False
                if outcome == "LOSS":
                    before = len(self.tracker.pending)
                    self._try_mtg(sig, now)
                    created = len(self.tracker.pending) > before
                if not created:
                    fx.maybe_history_report(self)
                    
                if self.risk.s["signals_today"] >= self.cfg.max_signals_per_day:
                    if self.store.kv_get("last_daily_summary") != now.strftime("%Y-%m-%d"):
                        self.tg.message(f"🛑 <b>Daily Limit Reached!</b> Bot has sent {self.cfg.max_signals_per_day} signals for today.")
                        self.send_daily_summary(now)
            except Exception as e:
                log("resolve_error", id=sig.id, error=str(e)[:200])

    def _try_mtg(self, sig, now):
        cfg = self.cfg
        if self.source is None or not cfg.mtg_enabled or sig.step >= cfg.mtg_max_steps: return
        m5, h1, h4 = self._frames(sig.asset, now)
        ok, why = self.strategy.mtg_precheck(sig.asset, sig.direction, m5, h1, h4, now)
        if not ok: return
        payout = self._payout(sig.asset)
        ok, status = self.risk.can_open(sig.asset, sig.direction, now, payout, pending=len(self.tracker.pending), is_mtg=True)
        if not ok: return
        base = sig.base_stake or sig.stake
        stake = self.risk.mtg_stake(base, payout)
        if stake is None: return
        entry_time = m5.index[-1] + pd.Timedelta(seconds=cfg.entry_tf_seconds)
        expiry = entry_time + pd.Timedelta(seconds=cfg.entry_tf_seconds * cfg.expiry_candles)
        ms = Signal(asset=sig.asset, direction=sig.direction, entry_time=entry_time.isoformat(),
                    expiry_time=expiry.isoformat(), entry_price=float(m5["close"].iloc[-1]),
                    stake=stake, payout_pct=payout, tf_seconds=cfg.entry_tf_seconds,
                    step=sig.step + 1, parent_id=sig.id, base_stake=base, score=sig.score, viz=sig.viz)
        self.tracker.add(ms)
        self.risk.register_signal(sig.asset, sig.direction, now, is_mtg=True)
        self.executor.execute(ms)
        self._send_signal(ms, m5)

    def scan(self, now):
        cfg = self.cfg
        if self.source is None: return
        
        # ডাটাবেস থেকে টাইমার রিলোড (নিরাপত্তার জন্য)
        self.last_signal_time = float(self.store.kv_get("last_signal_time", 0))
        self.last_result_time = float(self.store.kv_get("last_result_time", 0))
        
        if self.tracker.pending:
            for sig in list(self.tracker.pending):
                exp = pd.Timestamp(sig.expiry_time)
                if now > exp + pd.Timedelta(minutes=5):
                    self.tracker.close(sig, "VOID")
            if self.tracker.pending: return
                
        if self.risk.s["signals_today"] >= self.cfg.max_signals_per_day: return
            
        cands = []
        bad = fx.bad_assets(self)
        for asset in cfg.whitelist:
            if asset in bad: continue
            try:
                m5, h1, h4 = self._frames(asset, now)
                if len(m5) == 0 or len(h1) < 50 or len(h4) < 50: continue
                d = self.strategy.evaluate(asset, m5, h1, h4, now)
                if d.decision != "HOLD":
                    cands.append((d, m5))
            except: continue
                
        if not cands: return
        d, m5 = max(cands, key=lambda x: x[0].score * (self._payout(x[0].asset) / 100.0))
        payout = self._payout(d.asset)
        ok, status = self.risk.can_open(d.asset, d.decision, now, payout, pending=0)
        if not ok: return
        stake = self.risk.base_stake(d.asset, payout)
        entry_time = d.candle_close
        expiry = entry_time + timedelta(seconds=cfg.entry_tf_seconds * cfg.expiry_candles)
        sig = Signal(asset=d.asset, direction=d.decision, entry_time=entry_time.isoformat(),
                     expiry_time=expiry.isoformat(), entry_price=d.entry_price, stake=stake,
                     payout_pct=payout, tf_seconds=cfg.entry_tf_seconds, base_stake=stake,
                     score=d.score, viz=self._viz(d, m5), notes=getattr(d, "notes", ""))
        self.tracker.add(sig)
        self.risk.register_signal(d.asset, d.decision, now)
        sig.executed = self.executor.execute(sig)
        
        # সিগন্যাল টাইম ডাটাবেসে সেভ
        self.last_signal_time = time.time()
        self.store.kv_set("last_signal_time", self.last_signal_time)
        
        log("SIGNAL_SENT", id=sig.id, asset=d.asset, direction=d.decision, score=d.score)
        self._send_signal(sig, m5)

    def summary_numbers(self, since_iso=None):
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
        return {"all": agg(rows), "base": agg([r for r in rows if r["step"] == 0]),
                "mtg": agg([r for r in rows if r["step"] > 0])}

    def send_daily_summary(self, now):
        today = now.strftime("%Y-%m-%d")
        if self.store.kv_get("last_daily_summary") == today: return 
        rows = self.store.signal_rows(today)
        if not rows: return
        self.store.kv_set("last_daily_summary", today)
        
        total = len(rows)
        wins = sum(1 for r in rows if r["status"] == "WIN")
        losses = sum(1 for r in rows if r["status"] == "LOSS")
        draws = sum(1 for r in rows if r["status"] == "DRAW")
        pnl = sum(r["pnl"] for r in rows)
        win_rate = round((wins / (wins + losses) * 100), 1) if (wins + losses) > 0 else 0
        
        assets = {}
        for r in rows:
            a = r["asset"]
            if a not in assets: assets[a] = {"wins": 0, "losses": 0, "pnl": 0.0}
            if r["status"] == "WIN": assets[a]["wins"] += 1
            elif r["status"] == "LOSS": assets[a]["losses"] += 1
            assets[a]["pnl"] += r["pnl"]

        msg = f"📊 <b>DAILY SIGNAL REPORT - {today}</b>\n━━━━━━━━━━━━━━━━━━\n"
        msg += f"Total Signals: <b>{total}</b> (Max: {self.cfg.max_signals_per_day})\n"
        msg += f"✅ Wins: <b>{wins}</b> | ❌ Losses: <b>{losses}</b> | ➖ Draws: <b>{draws}</b>\n"
        msg += f"🎯 Win Rate: <b>{win_rate}%</b>\n💰 Net P/L: <b>{pnl:+.2f}$</b>\n━━━━━━━━━━━━━━━━━━\n<b>Market Breakdown:</b>\n"
        for a, data in assets.items():
            msg += f"🔹 <b>{a}</b>: {data['wins']}W - {data['losses']}L | {data['pnl']:+.2f}$\n"
        self.tg.message(msg)

    def daily_report(self, now):
        cfg = self.cfg
        today = now.strftime("%Y-%m-%d")
        if now.hour < cfg.daily_report_hour_utc or self.store.kv_get("last_report") == today: return
        self.store.kv_set("last_report", today)
        self.send_daily_summary(now)

    def cycle(self, now=None):
        now = now or utcnow()
        if self.source is None:
            self.daily_report(now)
            cleanup_old(self.cfg.charts_dir)
            return
        self.resolve_due(now)
        if not self.pause_event.is_set():
            self.scan(now)
        self.daily_report(now)
        cleanup_old(self.cfg.charts_dir)

    def run_forever(self):
        for w in validate(): log("config_warning", msg=w)
        log("bot_loop_started", mode=self.cfg.strategy_mode, tf=self.cfg.entry_tf_seconds, assets=len(self.cfg.whitelist), feed=self.feed_label())
        while not self.stop_event.is_set():
            tf = self.cfg.entry_tf_seconds
            next_boundary = (int(time.time() // tf) + 1) * tf
            prepare_sec = min(max(0, self.cfg.prepare_alert_seconds), max(0, tf - 5))
            
            if prepare_sec > 0:
                pre_at = next_boundary - prepare_sec
                while time.time() < pre_at and not self.stop_event.is_set():
                    if self.cfg.entry_tf_seconds != tf: break
                    time.sleep(min(0.5, max(0.0, pre_at - time.time())))
                
                if self.stop_event.is_set(): break
                
                if self.cfg.entry_tf_seconds == tf and time.time() >= pre_at:
                    try:
                        self._pre_scan(utcnow())
                        
                        at_5s = next_boundary - 5
                        while time.time() < at_5s and not self.stop_event.is_set():
                            if self.cfg.entry_tf_seconds != tf: break
                            time.sleep(min(0.5, max(0.0, at_5s - time.time())))
                        
                        if not self.stop_event.is_set() and self.cfg.entry_tf_seconds == tf and time.time() >= at_5s:
                            if getattr(self, "_pre_scan_candidate", None):
                                asset, direction = self._pre_scan_candidate
                                line = "🟢 UP (BUY)" if direction == "BUY" else "🔴 DOWN (SELL)"
                                self.tg.message(f"⏳ <b>ENTRY IN 5s</b>\n{line}\nPair: <b>{asset}</b>")
                                self._pre_scan_candidate = None
                    except Exception as e:
                        log("pre_scan_cycle_error", error=str(e)[:300])

            while time.time() < next_boundary and not self.stop_event.is_set():
                if self.cfg.entry_tf_seconds != tf: break
                time.sleep(min(0.5, max(0.0, next_boundary - time.time())))
                
            if self.stop_event.is_set(): break
            if self.cfg.entry_tf_seconds != tf: continue
            
            # ৩ মিনিটের কড়া গ্যাপ (মেইন লুপে)
            self.last_signal_time = float(self.store.kv_get("last_signal_time", 0))
            self.last_result_time = float(self.store.kv_get("last_result_time", 0))
            
            if (time.time() - self.last_signal_time) < 180 or (time.time() - self.last_result_time) < 180:
                log("GAP_ACTIVE", sec_since_sig=round(time.time() - self.last_signal_time), sec_since_res=round(time.time() - self.last_result_time))
                continue
                
            time.sleep(max(0.0, self.cfg.scan_delay_seconds))
            try: self.cycle()
            except Exception as e: log("cycle_error", error=str(e)[:300])
        log("bot_loop_exited")


def main():
    bot = Bot()
    controller = BotController(bot)
    controller.start_thread()
    app = create_app(bot, controller)
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))


if __name__ == "__main__":
    main()
