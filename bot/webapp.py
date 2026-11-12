"""webapp.py  --  Flask app with session auth + dashboard + bot control + settings API."""
import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (Flask, jsonify, request, render_template, redirect,
                   url_for, session, flash)
from werkzeug.security import generate_password_hash, check_password_hash

from .logger import log
from .controller import BotController


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
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(here)

    app = Flask(__name__,
                template_folder=os.path.join(root, "templates"),
                static_folder=os.path.join(root, "static"))
    app.secret_key = os.getenv("SESSION_SECRET", secrets.token_hex(32))
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_SECURE"] = os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true"
    app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)

    store = bot.store
    if store.user_count() == 0:
        username = os.getenv("WEB_ADMIN_USERNAME", "admin")
        password = os.getenv("WEB_ADMIN_PASSWORD", "")
        if not password:
            password = secrets.token_urlsafe(12)
            log("admin_seeded_random", username=username, password=password,
                note="Set WEB_ADMIN_PASSWORD in env. Shown once only.")
        store.create_user(username, generate_password_hash(password))
        log("admin_created", username=username)

    @app.get("/health")
    def health():
        return "ok", 200, {"Content-Type": "text/plain"}

    @app.route("/login", methods=["GET", "POST"])
    def login():
        if request.method == "POST":
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            user = store.get_user(username)
            if user and check_password_hash(user["password_hash"], password):
                session.clear()
                session["user"] = username
                session["sid"] = secrets.token_hex(8)
                session.permanent = True
                store.update_last_login(username)
                controller.on_login(session["sid"], username)
                controller.touch_keepalive()
                log("login_ok", username=username)
                nxt = request.args.get("next") or url_for("dashboard")
                if not nxt.startswith("/"): nxt = url_for("dashboard")
                return redirect(nxt)
            flash("Invalid username or password", "danger")
            log("login_fail", username=username)
        return render_template("login.html")

    @app.get("/logout")
    def logout():
        sid = session.get("sid")
        username = session.get("user")
        if sid: controller.on_logout(sid, username or "")
        log("logout", username=username or "")
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
        now = datetime.now(timezone.utc)
        return jsonify({
            "controller": controller.status(),
            "risk": bot.risk.snapshot(now),
            "mode": bot.cfg.execution_mode,
            "account": bot.cfg.account_type,
            "mtg_enabled": bot.cfg.mtg_enabled,
            "whitelist": list(bot.cfg.whitelist),
            "summary": bot.summary_numbers(),
            "recent": store.list_signals(limit=20),
            "strategy_mode": bot.cfg.strategy_mode,
            "timeframe_seconds": bot.cfg.entry_tf_seconds,
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

    # ---- settings API ----
    @app.get("/api/settings")
    @_auth_required
    def api_get_settings():
        return jsonify({
            "strategy_mode": store.get_setting("strategy_mode", bot.cfg.strategy_mode),
            "web_timeframe_seconds": int(store.get_setting("web_timeframe_seconds", bot.cfg.web_timeframe_seconds)),
        })

    @app.post("/api/settings")
    @_auth_required
    def api_set_settings():
        data = request.get_json() or {}
        mode = data.get("strategy_mode")
        tf = data.get("web_timeframe_seconds")
        if mode and mode not in ("rulebook", "quick", "news"):
            return jsonify({"error": "invalid strategy_mode"}), 400
        if tf and int(tf) not in (60, 300, 900, 3600):
            return jsonify({"error": "invalid web_timeframe_seconds"}), 400
        if mode: store.set_setting("strategy_mode", mode)
        if tf: store.set_setting("web_timeframe_seconds", int(tf))
        bot._build_strategy()
        return jsonify({"ok": True, "applied": {
            "strategy_mode": store.get_setting("strategy_mode", bot.cfg.strategy_mode),
            "web_timeframe_seconds": int(store.get_setting("web_timeframe_seconds", bot.cfg.web_timeframe_seconds)),
        }})

    @app.get("/stats")
    @_auth_required
    def stats():
        controller.touch_keepalive()
        now = datetime.now(timezone.utc)
        return jsonify({
            "risk": bot.risk.snapshot(now),
            "pending": [s.to_dict() for s in bot.tracker.pending],
            "mode": bot.cfg.execution_mode,
            "account": bot.cfg.account_type,
            "mtg_enabled": bot.cfg.mtg_enabled,
            "strategy_mode": bot.cfg.strategy_mode,
            "entry_tf_seconds": bot.cfg.entry_tf_seconds,
            "summary": bot.summary_numbers(),
        })

    @app.get("/diag")
    @_auth_required
    def diag():
        controller.touch_keepalive()
        return jsonify(dict(bot.diag.most_common()))

    @app.route("/admin/reset_readonly", methods=["GET", "POST"])
    @_auth_required
    def reset_ro():
        if not bot.cfg.admin_key or request.args.get("key") != bot.cfg.admin_key:
            return "forbidden", 403
        bot.risk.reset_read_only()
        return "read-only reset", 200

    return app
