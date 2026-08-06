import json
import logging
import os
import threading
from functools import wraps
from pathlib import Path

from flask import (
    Flask, jsonify, redirect, render_template,
    request, session as flask_session, url_for,
)

from activity_log import ActivityLog
from discord_trader import DiscordCopyTrader, DryRunExecutor
from kill_switch_executor import RemoteKillSwitchExecutor
from local_pause_executor import LocalPauseExecutor

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

PAUSE_FLAG_URL = "https://raw.githubusercontent.com/Dylannn-Source/schwab-copy-trader/claude/discord-alerts-robinhood-copy-ulxwpu/PAUSE_FLAG"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.json"))
config: dict = {}
if CONFIG_PATH.exists():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

DASHBOARD_PASSWORD = os.environ.get(
    "DASHBOARD_PASSWORD", config.get("dashboard_password", "changeme")
)

activity_log = ActivityLog()
_trader: DiscordCopyTrader | None = None
_rh_client = None
_pause_executor: LocalPauseExecutor | None = None
_trader_lock = threading.Lock()


def get_trader() -> DiscordCopyTrader | None:
    return _trader


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def build_executor():
    """Builds the order executor chain: DryRun (default) or, when
    live_trading is on, RobinhoodExecutor wrapped in the remote kill switch
    (message-Claude-to-pause) and then the local pause switch (dashboard
    button) — either layer being paused blocks the order."""
    global _rh_client, _pause_executor

    if not config.get("live_trading"):
        _rh_client = None
        base = DryRunExecutor(activity_log)
    else:
        account_number = config.get("robinhood_account_number")
        if not account_number:
            raise RuntimeError("live_trading is on but robinhood_account_number is not set.")

        from robinhood_executor import RobinhoodExecutor
        from robinhood_mcp_client import RobinhoodMCPClient

        activity_log.append("WARNING", "live_trading is ON — real orders will be placed on Robinhood.")
        _rh_client = RobinhoodMCPClient(
            token_path=config.get("robinhood_mcp_token_path", "robinhood_mcp_token.json"),
            activity_log=activity_log,
        )
        _rh_client.start()
        base = RobinhoodExecutor(_rh_client, account_number, activity_log)
        base = RemoteKillSwitchExecutor(base, PAUSE_FLAG_URL, activity_log)

    _pause_executor = LocalPauseExecutor(base, activity_log)
    return _pause_executor


def init_trader() -> DiscordCopyTrader | None:
    global _trader
    if not config.get("discord_user_token") or not config.get("discord_alert_channel_id"):
        return None
    executor = build_executor()
    with _trader_lock:
        _trader = DiscordCopyTrader(config, activity_log, executor=executor)
    return _trader


init_trader()


# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not flask_session.get("authenticated"):
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------

@app.route("/login", methods=["GET", "POST"])
def login():
    error = None
    if request.method == "POST":
        if request.form.get("password") == DASHBOARD_PASSWORD:
            flask_session["authenticated"] = True
            return redirect(url_for("dashboard"))
        error = "Incorrect password."
    return render_template("login.html", error=error)


@app.route("/logout")
def logout():
    flask_session.clear()
    return redirect(url_for("login"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    error = None
    if request.method == "POST":
        t = get_trader()
        was_running = t.is_running() if t else False
        if t:
            t.stop()
        if _rh_client:
            _rh_client.stop()

        for key in ("discord_alert_channel_id", "robinhood_account_number"):
            value = request.form.get(key, "").strip()
            if value:
                config[key] = value
        token = request.form.get("discord_user_token", "").strip()
        if token:
            config["discord_user_token"] = token
        password = request.form.get("dashboard_password", "").strip()
        if password:
            config["dashboard_password"] = password
        config["live_trading"] = request.form.get("live_trading") == "on"

        save_config()
        try:
            init_trader()
            error = None
        except Exception as e:
            error = str(e)
        if was_running and not error:
            get_trader().start()
        activity_log.append("INFO", "Settings updated.")
        return redirect(url_for("settings"))

    return render_template(
        "settings.html",
        config=config,
        has_token=bool(config.get("discord_user_token")),
        error=error,
    )


# ---------------------------------------------------------------------------
# Dashboard
# ---------------------------------------------------------------------------

@app.route("/")
@login_required
def dashboard():
    t = get_trader()
    return render_template(
        "dashboard.html",
        configured=t is not None,
        running=t.is_running() if t else False,
        live_trading=bool(config.get("live_trading")),
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/status")
@login_required
def api_status():
    t = get_trader()
    return jsonify({
        "configured": t is not None,
        "running": t.is_running() if t else False,
        "live_trading": bool(config.get("live_trading")),
        "paused": _pause_executor.paused if _pause_executor else False,
        "log": activity_log.entries()[-100:],
    })


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    t = get_trader()
    if not t:
        return jsonify({"error": "Not configured — fill in Settings first"}), 400
    t.start()
    return jsonify({"running": True})


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    t = get_trader()
    if t:
        t.stop()
    if _rh_client:
        _rh_client.stop()
    return jsonify({"running": False})


@app.route("/api/pause", methods=["POST"])
@login_required
def api_pause():
    if _pause_executor:
        _pause_executor.set_paused(True)
    return jsonify({"paused": True})


@app.route("/api/resume", methods=["POST"])
@login_required
def api_resume():
    if _pause_executor:
        _pause_executor.set_paused(False)
    return jsonify({"paused": False})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5050)), debug=False)
