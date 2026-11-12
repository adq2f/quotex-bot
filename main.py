"""
main.py  --  Web app + bot thread. Professional signals only.
"""
import sys
import os

sys.path.append(os.path.dirname(os.path.abspath(__file__)))

import threading
import time
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Dict, Optional

import pandas as pd

from bot.chart import cleanup_old, render_result_chart, render_signal_chart
from bot.config import settings, validate, strategy_for_timeframe
from bot.datafeed import make_source
from bot.executor import Executor
from bot.logger import log
from bot.news import NewsFilter
from bot.risk import RiskManager
from bot.store import Store
from bot.strategy import Decision, Strategy
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
        if self.source is None:
            try:
                self.source = make_source(cfg, self.store)
                log("source_initialized")
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
        os.makedirs(cfg.charts_dir, exist_ok=True)

        self.strategy = None
        self._build_strategy()

    # ------------------------------------------------------------------ strategy
    def _build_strategy(self):
        store = self.store
        tf = int(store.get_setting("web_timeframe_seconds", self.cfg.web_timeframe_seconds) or 300)
        if tf not in (60, 300, 900, 3600):
            tf = 300
        mode = strategy_for_timeframe(tf)

        self.cfg = replace(self.cfg, web_timeframe_seconds=tf,
                           entry_tf_seconds=tf, strategy_mode=mode)

        if mode == "quick":
            from bot.strategy_quick import QuickStrategy
            self.strategy = QuickStrategy(self.cfg, self.news)
        else:
            self.strategy = Strategy(self.cfg, self.news)
        log("strategy_built", mode=mode, tf=tf, assets=len(self.cfg.whitelist))

    # ------------------------------------------------------------------ helpers
    def _tz(self): return self.cfg.display_utc_offset_hours

    def _bd(self, iso_or_dt):
        t = pd.Timestamp(iso_or_dt)
        if t.tzinfo is None: t = t.tz_localize("UTC")
        return (t + pd.Timedelta(hours=self._tz())).strftime("%H:%M:%S")

    def _expiry_text(self):
        mins = self.cfg.expiry_candles * self.cfg.entry_tf_seconds // 60
        return f"{mins} min"

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
        if self.source is None: return self.cfg.default_payout_pct
        p = self.source.get_payout(asset)
        return float(p) if p else self.cfg.default_payout_pct

    def _viz(self, d, m5):
        s = getattr(d, "setup", None)
        if s is None: return {}
        t = lambda i: m5.index[i].isoformat() if i is not None and 0 <= i < len(m5) else None
        return {"zone": list(s.zone), "ob": list(s.ob), "ob_time": t(s.ob_idx),
                "fvg": list(s.fvg) if s.fvg else None, "fvg_time": t(s.fvg_idx),
                "sweep_level": s.sweep_level, "sweep_time": t(s.sweep_idx)}

    # ------------------------------------------------------------------ signal msg
    def _send_signal(self, sig, m5):
        path = os.path.join(self.cfg.charts_dir, f"sig_{sig.id}.png")
        info = {"asset": sig.asset, "direction": sig.direction, "score": sig.score, "step": sig.step,
                "tf_seconds": sig.tf_seconds, "entry_price": sig.entry_price,
                "expiry_text": f"expiry {self._expiry_text()}", "viz": sig.viz}
        try:
            render_signal_chart(m5, info, path, self._tz())
        except Exception as e:
            log("chart_failed", error=str(e)[:200]); path = ""

        if sig.direction == "BUY":
            direction_line = "🟢⬆️  UP  (BUY / CALL)"
        else:
            direction_line = "🔴⬇️  DOWN  (SELL / PUT)"

        tf_label = f"{self.cfg.entry_tf_seconds // 60} min"
        mtg_tag = f"  ·  MTG{sig.step}" if sig.step else ""

        cap = (
            f"📢 <b>SIGNAL{mtg_tag}</b>\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"<b>{direction_line}</b>\n"
            f"Pair: <b>{sig.asset}</b>\n"
            f"Timeframe: {tf_label}\n"
            f"Entry: <b>NOW</b> @ {sig.entry_price:.5f}\n"
            f"Expiry: {self._expiry_text()}\n"
            f"Confidence: <b>{sig.score}%</b>\n"
            f"Stake: ${sig.stake:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 {sig.id}"
        )
        if path and os.path.exists(path):
            self.tg.photo(path, cap)
        else:
            self.tg.message(cap)

    # ------------------------------------------------------------------ result msg
    def _send_result(self, sig, m5, outcome, pnl):
        path = os.path.join(self.cfg.charts_dir, f"res_{sig.id}.png")
        info = {"asset": sig.asset, "direction": sig.direction, "entry_price": sig.entry_price,
                "entry_time": sig.entry_time, "step": sig.step, "tf_seconds": sig.tf_seconds}
        try:
            render_result_chart(m5, info, sig.exit_price, outcome, pnl, path, self._tz())
        except Exception as e:
            log("chart_failed", error=str(e)[:200]); path = ""

        if outcome == "WIN":
            header = "✅ <b>WIN</b>"
        elif outcome == "LOSS":
            header = "❌ <b>LOSS</b>"
        else:
            header = "➖ <b>DRAW</b>"

        mtg_tag = f"  ·  MTG{sig.step}" if sig.step else ""
        pnl_str = f"+${pnl:.2f}" if pnl > 0 else (f"-${abs(pnl):.2f}" if pnl < 0 else "$0.00")

        cap = (
            f"{header}{mtg_tag}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"Pair: <b>{sig.asset}</b>\n"
            f"Result: <b>{pnl_str}</b>\n"
            f"Balance: ${self.risk.balance:.2f}\n"
            f"━━━━━━━━━━━━━━━━━━\n"
            f"🆔 {sig.id}"
        )
        if path and os.path.exists(path):
            self.tg.photo(path, cap)
        else:
            self.tg.message(cap)

    # ------------------------------------------------------------------ pre-scan (off by default)
    def _pre_scan(self, now):
        cfg = self.cfg
        if cfg.prepare_alert_seconds <= 0: return
        if self.source is None or self.pause_event.is_set(): return
        peek = getattr(self.source, "get_candles_peek", None)
        if peek is None: return

        candidates = []
        for asset in cfg.whitelist:
            try:
                m5 = peek(asset, cfg.entry_tf_seconds, 300, now)
                if len(m5) < cfg.min_entry_candles: continue
                h1 = self.source.get_candles(asset, 3600, 300, now)
                h4 = self.source.get_candles(asset, 14400, 300, now)
                if len(h1) < 50 or len(h4) < 50: continue
                d = self.strategy.evaluate(asset, m5, h1, h4, now)
                if d.decision == "HOLD": continue
                if d.score < cfg.prepare_min_score: continue
                entry_time = d.candle_close or now
                if entry_time <= now: continue
                candidates.append((d, entry_time))
            except Exception as e:
                log("pre_scan_error", asset=asset, error=str(e)[:200])

        candidates.sort(key=lambda x: x[0].score, reverse=True)
        for d, entry_time in candidates[:cfg.prepare_max_per_cycle]:
            secs = int((entry_time - now).total_seconds())
            line = "🟢⬆️ UP (BUY)" if d.decision == "BUY" else "🔴⬇️ DOWN (SELL)"
            self.tg.message(
                f"⏰ <b>ENTRY IN {secs}s</b>\n"
                f"{line}\n"
                f"Pair: <b>{d.asset}</b>\n"
                f"Confidence: {d.score}%"
            )
            log("prepare_sent", asset=d.asset, direction=d.decision, seconds=secs)

    # ------------------------------------------------------------------ resolve + MTG
    def resolve_due(self, now):
        cfg = self.cfg
        if self.source is None: return
        for sig in self.tracker.due(now):
            try:
                m5 = self.source.get_candles(sig.asset, cfg.entry_tf_seconds, 120, now)
            except Exception as e:
                log("resolve_fetch_failed", id=sig.id, error=str(e)[:200]); continue
            exp = pd.Timestamp(sig.expiry_time) - pd.Timedelta(seconds=cfg.entry_tf_seconds)
            if exp not in m5.index:
                sig.tries += 1
                if sig.tries >= 3:
                    self.tracker.close(sig, "VOID")
                    log("void_signal", asset=sig.asset, id=sig.id)
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
            if outcome == "LOSS": self._try_mtg(sig, now)

    def _try_mtg(self, sig, now):
        cfg = self.cfg
        if self.source is None or not cfg.mtg_enabled or sig.step >= cfg.mtg_max_steps: return
        if not hasattr(self.strategy, "mtg_precheck"):
            log("mtg_skipped", asset=sig.asset, reason="strategy_has_no_mtg"); return
        m5, h1, h4 = self._frames(sig.asset, now)
        ok, why = self.strategy.mtg_precheck(sig.asset, sig.direction, m5, h1, h4, now)
        if not ok: log("mtg_skipped", asset=sig.asset, reason=why); return
        payout = self._payout(sig.asset)
        ok, status = self.risk.can_open(sig.asset, sig.direction, now, payout,
                                        pending=len(self.tracker.pending), is_mtg=True)
        if not ok: log("mtg_skipped", asset=sig.asset, reason=status); return
        base = sig.base_stake or sig.stake
        stake = self.risk.mtg_stake(base, payout)
        if stake is None: log("mtg_skipped", asset=sig.asset, reason="seq_risk"); return
        entry_time = (m5.index[-1] + pd.Timedelta(seconds=cfg.entry_tf_seconds))
        expiry = entry_time + pd.Timedelta(seconds=cfg.entry_tf_seconds * cfg.expiry_candles)
        ms = Signal(asset=sig.asset, direction=sig.direction, entry_time=entry_time.isoformat(),
                    expiry_time=expiry.isoformat(), entry_price=float(m5["close"].iloc[-1]),
                    stake=stake, payout_pct=payout, tf_seconds=cfg.entry_tf_seconds,
                    step=sig.step + 1, parent_id=sig.id, base_stake=base, score=sig.score, viz=sig.viz)
        self.tracker.add(ms)
        self.risk.register_signal(sig.asset, sig.direction, now, is_mtg=True)
        self.executor.execute(ms)
        self._send_signal(ms, m5)
        log("mtg_signal", id=ms.id, parent=sig.id, stake=stake)

    def reconnect_source(self):
        from bot.datafeed import make_source
        try:
            if self.source is not None: self.source.close()
        except Exception: pass
        self.source = make_source(self.cfg, self.store)
        self.executor.source = self.source
        self._htf = {}
        log("source_reconnected")

    # ------------------------------------------------------------------ scan (verbose)
    def scan(self, now):
        cfg = self.cfg
        if self.source is None:
            self.diag["no_source"] += 1
            log("scan_no_source")
            return
        if self.tracker.pending:
            self.diag["pending_trade"] += 1
            return

        scan_started = time.time()
        tried = 0
        empty = 0
        errors = 0
        cands = []

        for asset in cfg.whitelist:
            tried += 1
            try:
                m5, h1, h4 = self._frames(asset, now)
            except Exception as e:
                errors += 1
                self.diag["frames_exception"] += 1
                log("scan_frames_error", asset=asset, error=str(e)[:200])
                continue

            if m5 is None or len(m5) == 0:
                empty += 1
                self.diag["empty_m5"] += 1
                log("scan_empty_m5", asset=asset)
                continue

            if len(h1) < 50 or len(h4) < 50:
                self.diag["short_htf"] += 1
                log("scan_short_htf", asset=asset, h1=len(h1), h4=len(h4))
                continue

            try:
                d = self.strategy.evaluate(asset, m5, h1, h4, now)
            except Exception as e:
                errors += 1
                self.diag["evaluate_exception"] += 1
                log("scan_eval_error", asset=asset, error=str(e)[:200])
                continue

            gate = getattr(d, "gate", "") or ""
            self.diag[gate or d.decision] += 1
            payload = d.to_dict() if hasattr(d, "to_dict") else {
                "asset": getattr(d, "asset", asset), "decision": d.decision,
                "score": d.score, "notes": getattr(d, "notes", "")}
            self.store.log_decision(asset, d.decision, d.score, gate, payload)
            if d.decision != "HOLD":
                cands.append((d, m5))

        elapsed = round(time.time() - scan_started, 1)
        log("scan_completed", tried=tried, empty=empty, errors=errors,
            candidates=len(cands), elapsed_sec=elapsed)

        if not cands:
            return
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
        sig = Signal(asset=d.asset, direction=d.decision, entry_time=entry_time.isoformat(),
                     expiry_time=expiry.isoformat(), entry_price=d.entry_price, stake=d.stake,
                     payout_pct=payout, tf_seconds=cfg.entry_tf_seconds, base_stake=d.stake,
                     score=d.score, viz=self._viz(d, m5), notes=getattr(d, "notes", ""))
        self.tracker.add(sig)
        self.risk.register_signal(d.asset, d.decision, now)
        sig.executed = self.executor.execute(sig)
        self._send_signal(sig, m5)
        log("signal", id=sig.id, asset=d.asset, direction=d.decision,
            score=d.score, stake=d.stake)

    # ------------------------------------------------------------------ reports
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
        return {"all": agg(rows),
                "base": agg([r for r in rows if r["step"] == 0]),
                "mtg": agg([r for r in rows if r["step"] > 0])}

    def daily_report(self, now):
        cfg = self.cfg
        today = now.strftime("%Y-%m-%d")
        if now.hour < cfg.daily_report_hour_utc or self.store.kv_get("last_report") == today: return
        self.store.kv_set("last_report", today)
        s = self.summary_numbers(today)
        def line(n, a):
            return (f"{n}: W{a['win']} L{a['loss']} D{a['draw']}  "
                    f"WR {a['win_rate'] if a['win_rate'] is not None else '-'}%  P/L ${a['pnl']}")
        txt = (f"📈 <b>Daily Report · {today}</b>\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"{line('Total', s['all'])}\n"
               f"{line('Base', s['base'])}\n"
               f"{line('MTG', s['mtg'])}\n"
               f"━━━━━━━━━━━━━━━━━━\n"
               f"Balance: ${self.risk.balance:.2f}")
        self.tg.message(txt)

    # ------------------------------------------------------------------ loop
    def cycle(self, now=None):
        now = now or utcnow()
        if self.source is None:
            self.diag["no_source"] += 1
            self.daily_report(now)
            cleanup_old(self.cfg.charts_dir)
            return
        if self.cfg.execution_mode == "execute":
            b = self.source.get_balance()
            if b: self.risk.set_balance(b)
        self.resolve_due(now)
        if not self.pause_event.is_set():
            self.scan(now)
        self.daily_report(now)
        cleanup_old(self.cfg.charts_dir)

    def run_forever(self):
        for w in validate():
            log("config_warning", msg=w)
        log("bot_loop_started", mode=self.cfg.strategy_mode, tf=self.cfg.entry_tf_seconds,
            assets=len(self.cfg.whitelist))
        while not self.stop_event.is_set():
            tf = self.cfg.entry_tf_seconds
            prepare_sec = max(0, self.cfg.prepare_alert_seconds)
            prepare_sec = min(prepare_sec, max(0, tf - 5))
            now_ts = time.time()
            next_boundary = (int(now_ts // tf) + 1) * tf

            if prepare_sec > 0:
                pre_at = next_boundary - prepare_sec
                while time.time() < pre_at and not self.stop_event.is_set():
                    if self.cfg.entry_tf_seconds != tf: break
                    time.sleep(min(0.5, max(0.0, pre_at - time.time())))
                if self.stop_event.is_set(): break
                if self.cfg.entry_tf_seconds == tf and time.time() >= pre_at:
                    try: self._pre_scan(utcnow())
                    except Exception as e: log("pre_scan_cycle_error", error=str(e)[:300])

            while time.time() < next_boundary and not self.stop_event.is_set():
                if self.cfg.entry_tf_seconds != tf: break
                time.sleep(min(0.5, max(0.0, next_boundary - time.time())))
            if self.stop_event.is_set(): break
            if self.cfg.entry_tf_seconds != tf: continue

            time.sleep(max(0.0, self.cfg.scan_delay_seconds))
            try: self.cycle()
            except Exception as e: log("cycle_error", error=str(e)[:300])
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
