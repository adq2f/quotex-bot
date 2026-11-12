"""webapp.py  --  Flask: auth + dashboard + bot control + diagnostics."""
import os
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps

from flask import (Flask, jsonify, request, render_template, redirect,
                   url_for, session, flash)
from werkzeug.security import generate_password_hash, check_password_hash

from .logger import log
from .controller import BotController
from .config import strategy_for_timeframe


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
            log("admin_seeded_random", username=username, password=password)
        store.create_user(username, generate_password_hash(password))
        log("admin_created", username=username)

    # ------------------------------------------------------------ public
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
                if not nxt.startswith("/"):
                    nxt = url_for("dashboard")
                return redirect(nxt)
            flash("Invalid username or password", "danger")
        return render_template("login.html")

    @app.get("/logout")
    def logout():
        sid = session.get("sid")
        username = session.get("user")
        if sid:
            controller.on_logout(sid, username or "")
        session.clear()
        return redirect(url_for("login"))

    @app.get("/")
    def index():
        return redirect(url_for("dashboard") if session.get("user") else url_for("login"))

    # ------------------------------------------------------------ authenticated
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

    @app.get("/api/settings")
    @_auth_required
    def api_get_settings():
        tf = int(store.get_setting("web_timeframe_seconds", bot.cfg.web_timeframe_seconds) or 300)
        return jsonify({
            "web_timeframe_seconds": tf,
            "strategy_mode": strategy_for_timeframe(tf),
        })

    @app.post("/api/settings")
    @_auth_required
    def api_set_settings():
        data = request.get_json() or {}
        tf = data.get("web_timeframe_seconds")
        if not tf or int(tf) not in (60, 300, 900, 3600):
            return jsonify({"error": "invalid web_timeframe_seconds"}), 400
        store.set_setting("web_timeframe_seconds", int(tf))
        bot._build_strategy()
        return jsonify({"ok": True, "applied": {
            "web_timeframe_seconds": int(tf),
            "strategy_mode": strategy_for_timeframe(int(tf)),
        }})

    # ------------------------------------------------------------ diagnostics
    @app.get("/api/test/scan")
    @_auth_required
    def api_test_scan():
        """Manual diagnostic — tests ONE asset end-to-end and returns details."""
        asset = request.args.get("asset") or (bot.cfg.whitelist[0] if bot.cfg.whitelist else "EURUSD_otc")
        now = datetime.now(timezone.utc)
        result = {
            "asset": asset,
            "source_class": type(bot.source).__name__ if bot.source else None,
            "data_source_setting": bot.cfg.data_source,
            "whitelist_count": len(bot.cfg.whitelist),
            "entry_tf_seconds": bot.cfg.entry_tf_seconds,
            "strategy": bot.cfg.strategy_mode,
        }
        try:
            if bot.source is None:
                result["error"] = "source is None (init failed)"
                return jsonify(result)

            # test raw get_candles for each timeframe
            for name, tf_sec in [("m_entry", bot.cfg.entry_tf_seconds), ("h1", 3600), ("h4", 14400)]:
                try:
                    df = bot.source.get_candles(asset, tf_sec, 300, now)
                    rows = len(df) if df is not None else 0
                    result[f"{name}_rows"] = rows
                    if rows > 0:
                        result[f"{name}_last_time"] = str(df.index[-1])
                        result[f"{name}_last_close"] = float(df["close"].iloc[-1])
                    else:
                        result[f"{name}_empty"] = True
                except Exception as e:
                    result[f"{name}_exception"] = str(e)[:250]

            # try full evaluate
            try:
                m5, h1, h4 = bot._frames(asset, now)
                result["frames_lengths"] = {"m5": len(m5), "h1": len(h1), "h4": len(h4)}
                if len(m5) > 0 and len(h1) >= 50 and len(h4) >= 50:
                    d = bot.strategy.evaluate(asset, m5, h1, h4, now)
                    result["evaluate_ok"] = True
                    result["decision"] = d.decision
                    result["score"] = d.score
                    result["gate"] = getattr(d, "gate", "")
                    result["notes"] = getattr(d, "notes", "")
                else:
                    result["evaluate_skipped"] = "not enough candles"
            except Exception as e:
                result["evaluate_error"] = str(e)[:300]

        except Exception as e:
            result["error"] = str(e)[:300]
        return jsonify(result)

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
