import json
import logging
import os
import threading
import time
from functools import wraps
from pathlib import Path

import schwab
from authlib.integrations.requests_client import OAuth2Session
from flask import (
    Flask, jsonify, redirect, render_template,
    request, session as flask_session, url_for,
)

from trader import ActivityLog, CopyTrader

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)

SCHWAB_AUTH_URL = "https://api.schwabapi.com/v1/oauth/authorize"
SCHWAB_TOKEN_URL = "https://api.schwabapi.com/v1/oauth/token"

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.json"))
config: dict = {}
if CONFIG_PATH.exists():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

# Environment variables override config.json — used on Railway/cloud hosts.
for _key, _env in [
    ("app_key",               "APP_KEY"),
    ("app_secret",            "APP_SECRET"),
    ("redirect_uri",          "REDIRECT_URI"),
    ("leader_account_hash",   "LEADER_ACCOUNT_HASH"),
    ("follower_account_hash", "FOLLOWER_ACCOUNT_HASH"),
    ("token_path",            "TOKEN_PATH"),
]:
    if os.environ.get(_env):
        config[_key] = os.environ[_env]

DASHBOARD_PASSWORD = os.environ.get(
    "DASHBOARD_PASSWORD", config.get("dashboard_password", "changeme")
)

activity_log = ActivityLog()
_trader: CopyTrader | None = None
_trader_lock = threading.Lock()


def get_trader() -> CopyTrader | None:
    return _trader


def get_client():
    """Build a Schwab client from the saved token, if credentials and a token exist."""
    if not config.get("app_key") or not config.get("app_secret"):
        return None
    token_path = config.get("token_path", "schwab_token.json")
    if not Path(token_path).exists():
        return None
    try:
        return schwab.auth.client_from_token_file(
            token_path, config["app_key"], config["app_secret"],
        )
    except Exception as e:
        activity_log.append("ERROR", f"Failed to load Schwab client: {e}")
        return None


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def init_trader() -> CopyTrader | None:
    global _trader
    client = get_client()
    if not client or not config.get("leader_account_hash") or not config.get("follower_account_hash"):
        return None
    with _trader_lock:
        _trader = CopyTrader(config, client, activity_log)
    return _trader


# Try to connect on startup if a token already exists
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
# Dashboard auth routes
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
# Schwab OAuth routes
# ---------------------------------------------------------------------------

@app.route("/auth/connect")
@login_required
def auth_connect():
    if not config.get("app_key") or not config.get("redirect_uri"):
        return redirect(url_for("settings"))
    oauth = OAuth2Session(config["app_key"], redirect_uri=config["redirect_uri"])
    url, state = oauth.create_authorization_url(SCHWAB_AUTH_URL)
    flask_session["oauth_state"] = state
    return redirect(url)


@app.route("/callback")
def oauth_callback():
    oauth = OAuth2Session(
        config["app_key"],
        redirect_uri=config["redirect_uri"],
        state=flask_session.pop("oauth_state", None),
    )
    token = oauth.fetch_token(
        SCHWAB_TOKEN_URL,
        authorization_response=request.url,
        auth=(config["app_key"], config["app_secret"]),
    )
    token_path = config.get("token_path", "schwab_token.json")
    wrapped_token = {
        "creation_timestamp": int(time.time()),
        "token": dict(token),
    }
    with open(token_path, "w") as f:
        json.dump(wrapped_token, f)

    if init_trader() is not None:
        activity_log.append("INFO", "Schwab account connected successfully.")
    else:
        activity_log.append("ERROR", "Schwab token saved but client failed to initialise.")
    return redirect(url_for("dashboard"))


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

@app.route("/settings", methods=["GET", "POST"])
@login_required
def settings():
    if request.method == "POST":
        for key in ("app_key", "redirect_uri", "leader_account_hash", "follower_account_hash"):
            value = request.form.get(key, "").strip()
            if value:
                config[key] = value
        app_secret = request.form.get("app_secret", "").strip()
        if app_secret:
            config["app_secret"] = app_secret
        save_config()
        init_trader()
        activity_log.append("INFO", "Settings updated.")
        return redirect(url_for("settings"))

    error = None
    accounts = []
    client = get_client()
    if client:
        try:
            resp = client.get_account_numbers()
            resp.raise_for_status()
            accounts = resp.json()
        except Exception as e:
            error = f"Could not fetch accounts: {e}"

    return render_template(
        "settings.html",
        config=config,
        accounts=accounts,
        error=error,
        has_secret=bool(config.get("app_secret")),
        trader_ready=get_trader() is not None,
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
        connected=t is not None,
        running=t.is_running() if t else False,
        multiplier=t.multiplier if t else config.get("size_multiplier", 1.0),
    )


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------

@app.route("/api/status")
@login_required
def api_status():
    t = get_trader()
    return jsonify({
        "connected": t is not None,
        "running": t.is_running() if t else False,
        "multiplier": t.multiplier if t else config.get("size_multiplier", 1.0),
        "log": activity_log.entries()[-100:],
    })


@app.route("/api/positions")
@login_required
def api_positions():
    t = get_trader()
    if not t:
        return jsonify({"leader": [], "follower": []})
    try:
        return jsonify({
            "leader": t.get_positions(config["leader_account_hash"]),
            "follower": t.get_positions(config["follower_account_hash"]),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/start", methods=["POST"])
@login_required
def api_start():
    t = get_trader()
    if not t:
        return jsonify({"error": "Not connected to Schwab"}), 400
    t.start()
    return jsonify({"running": True})


@app.route("/api/stop", methods=["POST"])
@login_required
def api_stop():
    t = get_trader()
    if t:
        t.stop()
    return jsonify({"running": False})


@app.route("/api/multiplier", methods=["POST"])
@login_required
def api_multiplier():
    t = get_trader()
    value = float(request.get_json().get("value", 1.0))
    if t:
        t.set_multiplier(value)
    config["size_multiplier"] = value
    save_config()
    return jsonify({"multiplier": value})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=False)
