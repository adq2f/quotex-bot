"""
webapp.py  --  Flask: auth + dashboard + bot control + settings + history + diagnostics.

v2.2 fix log:
  * /api/settings POST now updates bot.cfg for TF and MTG (previously only max_signals),
    and uses overrides so a failing DB can't revert the change.
  * Every /api/* endpoint that touches the store is wrapped so a Turso outage
    cannot 500 the dashboard.
  * /api/bot/status exposes the in-memory signals_sent_today directly.
"""
import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, jsonify, request, render_template, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash
import dataclasses

from . import features as fx
from .config import strategy_for_timeframe
from .controller import BotController
from .logger import log


def _auth_required(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("user"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "auth required"}), 401
            return redirect(url_for("login", next=request.path))
        return f(*a, **kw)
    return wrapper


def _safe(fn, fallback):
    """Run fn(); return fallback on any exception (typically a DB outage)."""
    try:
        return fn()
    except Exception as e:
        log("api_safe_swallow", error=str(e)[:200])
        return fallback


def create_app(bot, controller: BotController) -> Flask:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__, template_folder=os.path.join(root, "templates"),
                static_folder=os.path.join(root, "static"))
    app.secret_key = os.getenv("SESSION_SECRET", secrets.token_hex(32))
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=7))
    store = bot.store

    try:
        if store.user_count() == 0:
            username = os.getenv("WEB_ADMIN_USERNAME", "admin")
            password = os.getenv("WEB_ADMIN_PASSWORD", "")
            if not password:
                password = secrets.token_urlsafe(12)
                log("admin_seeded_random", username=username, password=password)
            store.create_user(username, generate_password_hash(password))
            log("admin_created", username=username)
    except Exception as e:
        log("admin_seed_error", error=str(e)[:300])

    @app.get("/health")
    def health():
        return "ok", 200, {"Content-Type": "text/plain"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            user = _safe(lambda: store.get_user(username), None)
            if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
                session.clear()
                session.update(user=username, sid=secrets.token_hex(8))
                session.permanent = True
                _safe(lambda: store.update_last_login(username), None)
                controller.on_login(session["sid"], username)
                _safe(controller.touch_keepalive, None)
                nxt = request.args.get("next") or url_for("dashboard")
                return redirect(nxt if nxt.startswith("/") else url_for("dashboard"))
            flash("Invalid username or password", "danger")
            log("login_fail", username=username)
        return render_template("login.html")

    @app.get("/logout")
    def logout():
        if session.get("sid"):
            controller.on_logout(session["sid"], session.get("user") or "")
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return redirect(url_for("dashboard") if session.get("user") else url_for("login"))

    @app.get("/dashboard")
    @_auth_required
    def dashboard():
        _safe(controller.touch_keepalive, None)
        return render_template("dashboard.html", username=session["user"])

    @app.get("/api/bot/status")
    @_auth_required
    def api_bot_status():
        _safe(controller.touch_keepalive, None)
        risk = _safe(lambda: bot.risk.snapshot(datetime.now(timezone.utc)),
                     {"balance": 0, "day_pnl": 0, "consec_losses": 0,
                      "cooldown_until": None, "read_only": False,
                      "signals_today": 0, "asset_losses": {}})
        summary = _safe(bot.summary_numbers, {"all": {}, "base": {}, "mtg": {}})
        recent = _safe(lambda: store.list_signals(limit=20), [])
        return jsonify({
            "controller": controller.status(),
            "risk": risk,
            "mode": bot.cfg.execution_mode,
            "account": bot.cfg.account_type,
            "mtg_enabled": bot.cfg.mtg_enabled,
            "whitelist": list(bot.cfg.whitelist),
            "summary": summary,
            "recent": recent,
            "strategy_mode": bot.cfg.strategy_mode,
            "timeframe_seconds": bot.cfg.entry_tf_seconds,
            "feed": bot.feed_label(),
            "signals_today": bot.signals_sent_today,     # in-memory counter
            "max_signals": bot.cfg.max_signals_per_day,
        })

    @app.post("/api/bot/start")
    @_auth_required
    def api_bot_start():
        _safe(controller.touch_keepalive, None)
        controller.resume(reason=f"manual:{session.get('user')}")
        return jsonify({"ok": True, "status": controller.status()})

    @app.post("/api/bot/stop")
    @_auth_required
    def api_bot_stop():
        controller.pause(reason=f"manual:{session.get('user')}")
        return jsonify({"ok": True, "status": controller.status()})

    @app.get("/api/settings")
    @_auth_required
    def api_get_settings():
        tf = _safe(lambda: int(store.get_setting("web_timeframe_seconds", bot.cfg.web_timeframe_seconds) or 300),
                   bot.cfg.web_timeframe_seconds)
        max_sig = _safe(lambda: int(store.get_setting("max_signals_per_day", bot.cfg.max_signals_per_day)),
                        bot.cfg.max_signals_per_day)
        mtg = _safe(lambda: bool(store.get_setting("mtg_enabled", bot.cfg.mtg_enabled)),
                    bot.cfg.mtg_enabled)
        return jsonify({"web_timeframe_seconds": tf, "strategy_mode": strategy_for_timeframe(tf),
                        "mtg_enabled": mtg, "max_signals_per_day": max_sig})

    @app.post("/api/settings")
    @_auth_required
    def api_set_settings():
        data = request.get_json() or {}
        overrides = {}

        if "web_timeframe_seconds" in data:
            tf = int(data["web_timeframe_seconds"] or 0)
            if tf not in (60, 300, 900, 3600):
                return jsonify({"error": "invalid web_timeframe_seconds"}), 400
            _safe(lambda: store.set_setting("web_timeframe_seconds", tf), None)
            overrides["web_timeframe_seconds"] = tf

        if "mtg_enabled" in data:
            mtg = bool(data["mtg_enabled"])
            _safe(lambda: store.set_setting("mtg_enabled", mtg), None)
            overrides["mtg_enabled"] = mtg

        if "max_signals_per_day" in data:
            try:
                ms = int(data["max_signals_per_day"])
            except Exception:
                return jsonify({"error": "invalid max_signals_per_day"}), 400
            if ms < 1 or ms > 100:
                return jsonify({"error": "invalid max_signals_per_day"}), 400
            _safe(lambda: store.set_setting("max_signals_per_day", ms), None)
            overrides["max_signals_per_day"] = ms

        bot._build_strategy(overrides=overrides)
        return jsonify({"ok": True, "applied": {
            "web_timeframe_seconds": bot.cfg.entry_tf_seconds,
            "strategy_mode": bot.cfg.strategy_mode,
            "mtg_enabled": bot.cfg.mtg_enabled,
            "max_signals_per_day": bot.cfg.max_signals_per_day,
        }})

    @app.get("/api/history")
    @_auth_required
    def api_history():
        return jsonify(_safe(lambda: fx.history_blocks(store), {"total": {}, "blocks": []}))

    @app.get("/api/test/scan")
    @_auth_required
    def api_test_scan():
        asset = request.args.get("asset") or (bot.cfg.whitelist[0] if bot.cfg.whitelist else "EURUSD")
        now = datetime.now(timezone.utc)
        res = {"asset": asset, "feed": bot.feed_label(), "data_source_setting": bot.cfg.data_source,
               "entry_tf_seconds": bot.cfg.entry_tf_seconds, "strategy": bot.cfg.strategy_mode}
        if bot.source is None:
            res["error"] = "source is None (init failed)"
            return jsonify(res)
        for name, tf in [("entry", bot.cfg.entry_tf_seconds), ("h1", 3600), ("h4", 14400)]:
            try:
                df = bot.source.get_candles(asset, tf, 300, now)
                res[f"{name}_rows"] = len(df)
                if len(df):
                    res[f"{name}_last"] = [str(df.index[-1]), float(df["close"].iloc[-1])]
            except Exception as e:
                res[f"{name}_exception"] = str(e)[:250]
        try:
            m5, h1, h4 = bot._frames(asset, now)
            if len(m5) and len(h1) >= 50 and len(h4) >= 50:
                d = bot.strategy.evaluate(asset, m5, h1, h4, now)
                res.update(decision=d.decision, score=d.score, gate=getattr(d, "gate", ""),
                           notes=getattr(d, "notes", ""))
            else:
                res["evaluate_skipped"] = "not enough candles"
        except Exception as e:
            res["evaluate_error"] = str(e)[:300]
        return jsonify(res)

    @app.get("/api/test/telegram")
    @_auth_required
    def api_test_telegram():
        try:
            return jsonify(bot.tg.test())
        except Exception as e:
            return jsonify({"ok": False, "error": str(e)[:300]}), 500

    @app.get("/stats")
    @_auth_required
    def stats():
        _safe(controller.touch_keepalive, None)
        return jsonify({
            "risk": _safe(lambda: bot.risk.snapshot(datetime.now(timezone.utc)), {}),
            "pending": [s.to_dict() for s in bot.tracker.pending],
            "summary": _safe(bot.summary_numbers, {}),
            "signals_today": bot.signals_sent_today,
            "max_signals": bot.cfg.max_signals_per_day,
            "strategy_mode": bot.cfg.strategy_mode,
            "entry_tf_seconds": bot.cfg.entry_tf_seconds,
        })

    @app.get("/diag")
    @_auth_required
    def diag():
        return jsonify(dict(bot.diag.most_common()))

    @app.route("/admin/reset_readonly", methods=["GET", "POST"])
    @_auth_required
    def reset_ro():
        if not bot.cfg.admin_key or request.args.get("key") != bot.cfg.admin_key:
            return "forbidden", 403
        bot.risk.reset_read_only()
        return "read-only reset", 200

    return app
