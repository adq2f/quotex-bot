"""health.py -- Flask app: /health for UptimeRobot (11.2), plus /stats, /diag, /admin/reset_readonly."""
from datetime import datetime, timezone

from flask import Flask, jsonify, request


def create_app(bot) -> Flask:
    app = Flask(__name__)

    @app.get("/")
    @app.get("/health")
    def health():
        return "ok", 200, {"Content-Type": "text/plain"}

    @app.get("/stats")
    def stats():
        now = datetime.now(timezone.utc)
        return jsonify({"risk": bot.risk.snapshot(now), "pending": [s.to_dict() for s in bot.tracker.pending],
                        "mode": bot.cfg.execution_mode, "account": bot.cfg.account_type, "mtg_enabled": bot.cfg.mtg_enabled,
                        "summary": bot.summary_numbers()})

    @app.get("/diag")
    def diag():
        """Which gate blocks most often. Use this to see WHY there are few/no signals."""
        return jsonify(dict(bot.diag.most_common()))

    @app.route("/admin/reset_readonly", methods=["GET", "POST"])
    def reset_ro():
        if not bot.cfg.admin_key or request.args.get("key") != bot.cfg.admin_key:
            return "forbidden", 403
        bot.risk.reset_read_only()
        return "read-only reset", 200

    return app
