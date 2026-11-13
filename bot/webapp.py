"""webapp.py  --  Flask: auth + dashboard + bot control + settings + history + diagnostics."""
import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import Flask, jsonify, request, render_template, redirect, url_for, session, flash
from werkzeug.security import generate_password_hash, check_password_hash

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


def create_app(bot, controller: BotController) -> Flask:
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app = Flask(__name__, template_folder=os.path.join(root, "templates"),
                static_folder=os.path.join(root, "static"))
    app.secret_key = os.getenv("SESSION_SECRET", secrets.token_hex(32))
    app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax",
                      SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
                      PERMANENT_SESSION_LIFETIME=timedelta(days=7))
    store = bot.store
    if store.user_count() == 0:
        username = os.getenv("WEB_ADMIN_USERNAME", "admin")
        password = os.getenv("WEB_ADMIN_PASSWORD", "")
        if not password:
            password = secrets.token_urlsafe(12)
            log("admin_seeded_random", username=username, password=password)
        store.create_user(username, generate_password_hash(password))
        log("admin_created", username=username)

    @app.get("/health")
    def health():
        return "ok", 200, {"Content-Type": "text/plain"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            user = store.get_user(username)
            if user and check_password_hash(user["password_hash"], request.form.get("password", "")):
                session.clear()
                session.update(user=username, sid=secrets.token_hex(8))
                session.permanent = True
                store.update_last_login(username)
                controller.on_login(session["sid"], username)
                controller.touch_keepalive()
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
        controller.touch_keepalive()
        return render_template("dashboard.html", username=session["user"])

    @app.get("/api/bot/status")
    @_auth_required
    def api_bot_status():
        controller.touch_keepalive()
        return jsonify({
            "controller": controller.status(), 
            "risk": bot.risk.snapshot(datetime.now(timezone.utc)),
            "mode": bot.cfg.execution_mode, 
            "account": bot.cfg.account_type,
            "mtg_enabled": bot.cfg.mtg_enabled, 
            "whitelist": list(bot.cfg.whitelist),
            "summary": bot.summary_numbers(), 
            "recent": store.list_signals(limit=20),
            "strategy_mode": bot.cfg.strategy_mode, 
            "timeframe_seconds": bot.cfg.entry_tf_seconds,
            "feed": bot.feed_label(),
            "signals_today": bot.risk.s.get("signals_today", 0),
            "max_signals": bot.cfg.max_signals_per_day
        })

    @app.post("/api/bot/start")
    @_auth_required
    def api_bot_start():
        controller.touch_keepalive()
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
        tf = int(store.get_setting("web_timeframe_seconds", bot.cfg.web_timeframe_seconds) or 300)
        max_sig = int(store.get_setting("max_signals_per_day", bot.cfg.max_signals_per_day))
        return jsonify({"web_timeframe_seconds": tf, "strategy_mode": strategy_for_timeframe(tf),
                        "mtg_enabled": bool(store.get_setting("mtg_enabled", bot.cfg.mtg_enabled)),
                        "max_signals_per_day": max_sig})

    @app.post("/api/settings")
    @_auth_required
    def api_set_settings():
        data = request.get_json() or {}
        if "web_timeframe_seconds" in data:
            tf = int(data["web_timeframe_seconds"] or 0)
            if tf not in (60, 300, 900, 3600):
                return jsonify({"error": "invalid web_timeframe_seconds"}), 400
            store.set_setting("web_timeframe_seconds", tf)
        if "mtg_enabled" in data:
            store.set_setting("mtg_enabled", bool(data["mtg_enabled"]))
        if "max_signals_per_day" in data:
            ms = int(data["max_signals_per_day"])
            if ms < 1 or ms > 100:
                return jsonify({"error": "invalid max_signals_per_day"}), 400
            store.set_setting("max_signals_per_day", ms)
            
        bot._build_strategy()
        return jsonify({"ok": True, "applied": {
            "web_timeframe_seconds": bot.cfg.entry_tf_seconds, "strategy_mode": bot.cfg.strategy_mode,
            "mtg_enabled": bot.cfg.mtg_enabled, "max_signals_per_day": bot.cfg.max_signals_per_day}})

    @app.get("/api/history")
    @_auth_required
    def api_history():
        return jsonify(fx.history_blocks(store))

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
        controller.touch_keepalive()
        return jsonify({"risk": bot.risk.snapshot(datetime.now(timezone.utc)),
                        "pending": [s.to_dict() for s in bot.tracker.pending], "summary": bot.summary_numbers(),
                        "strategy_mode": bot.cfg.strategy_mode, "entry_tf_seconds": bot.cfg.entry_tf_seconds})

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
