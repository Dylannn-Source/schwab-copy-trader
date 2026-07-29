import json
import logging
import os
import smtplib
import threading
import time
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText
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

# Schwab refresh tokens are only valid for 7 days regardless of activity —
# after that, reauthenticating via the OAuth flow is required no matter how
# recently the access token itself was refreshed.
REFRESH_TOKEN_LIFETIME_DAYS = 7
REAUTH_WARNING_DAYS = 2

COMBINED_FOLLOWERS = "combined_followers"

# Email alerting (e.g. Gmail SMTP with an app password) — all optional; if
# unset, alerts are simply skipped rather than erroring the whole app.
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
ALERT_EMAIL_TO = os.environ.get("ALERT_EMAIL_TO", SMTP_USERNAME)
TOKEN_WATCHER_INTERVAL_SECONDS = 6 * 3600

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", os.urandom(24))

CONFIG_PATH = Path(os.environ.get("CONFIG_PATH", "config.json"))
config: dict = {}
if CONFIG_PATH.exists():
    with open(CONFIG_PATH) as f:
        config = json.load(f)

# Environment variables override config.json — used on Railway/cloud hosts.
for _key, _env in [
    ("app_key",                 "APP_KEY"),
    ("app_secret",              "APP_SECRET"),
    ("redirect_uri",            "REDIRECT_URI"),
    ("leader_account_hash",     "LEADER_ACCOUNT_HASH"),
    ("follower_account_hashes", "FOLLOWER_ACCOUNT_HASHES"),
    ("token_path",              "TOKEN_PATH"),
    ("state_path",              "STATE_PATH"),
]:
    if os.environ.get(_env):
        config[_key] = os.environ[_env]

# Followers used to be a single hash; accept the old key and comma-separated
# env values, and normalize to a list going forward.
_raw_followers = config.get("follower_account_hashes", config.get("follower_account_hash", []))
if isinstance(_raw_followers, str):
    _raw_followers = [h.strip() for h in _raw_followers.split(",") if h.strip()]
config["follower_account_hashes"] = _raw_followers
config.pop("follower_account_hash", None)

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


def get_token_status() -> dict | None:
    """Report how close the saved Schwab token is to needing reauthentication.

    schwab-py auto-refreshes the short-lived access token using the refresh
    token, so this isn't detectable by asking the client — the refresh token
    itself just stops working after 7 days with no advance signal from Schwab.
    """
    token_path = Path(config.get("token_path", "schwab_token.json"))
    if not token_path.exists():
        return None
    try:
        with open(token_path) as f:
            data = json.load(f)
        created = data.get("creation_timestamp")
        if created is None:
            return None
        created_at = datetime.fromtimestamp(created, tz=timezone.utc)
        expires_at = created_at + timedelta(days=REFRESH_TOKEN_LIFETIME_DAYS)
        remaining = (expires_at - datetime.now(timezone.utc)).total_seconds() / 86400
        return {
            "expires_at": expires_at.isoformat(),
            "days_remaining": round(remaining, 2),
            "expired": remaining <= 0,
            "warning": remaining <= REAUTH_WARNING_DAYS,
        }
    except Exception:
        return None


def send_alert_email(subject: str, body: str) -> None:
    if not SMTP_USERNAME or not SMTP_PASSWORD or not ALERT_EMAIL_TO:
        activity_log.append("WARNING", f"Skipped alert email ({subject!r}) — SMTP not configured.")
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = SMTP_USERNAME
    msg["To"] = ALERT_EMAIL_TO
    try:
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=10) as server:
            server.starttls()
            server.login(SMTP_USERNAME, SMTP_PASSWORD)
            server.send_message(msg)
        activity_log.append("INFO", f"Sent alert email: {subject}")
    except Exception as e:
        activity_log.append("ERROR", f"Failed to send alert email ({subject!r}): {e}")


# Tracks which expiry cycle we've already emailed about, keyed by the token's
# expires_at timestamp — that value only changes when you reconnect, so this
# naturally resets after each reauthentication without needing to persist
# anything, and avoids re-sending the same alert on every periodic check.
_token_alert_sent = {"warning": None, "expired": None}


def _token_watcher_loop(stop_event: threading.Event):
    while not stop_event.is_set():
        try:
            status = get_token_status()
            if status:
                expires_at = status["expires_at"]
                if status["expired"] and _token_alert_sent["expired"] != expires_at:
                    send_alert_email(
                        "Schwab Copy Trader — session expired",
                        "Your Schwab session has expired. Trades will fail until you reconnect.\n\n"
                        "Go to your dashboard's Settings page and click 'Connect / Reconnect Schwab'.",
                    )
                    _token_alert_sent["expired"] = expires_at
                elif status["warning"] and _token_alert_sent["warning"] != expires_at:
                    send_alert_email(
                        "Schwab Copy Trader — session expiring soon",
                        f"Your Schwab session expires in ~{status['days_remaining']:.1f} day(s) "
                        f"(around {expires_at}).\n\n"
                        "Reconnect soon via your dashboard's Settings page to avoid interruption.",
                    )
                    _token_alert_sent["warning"] = expires_at
        except Exception as e:
            activity_log.append("ERROR", f"Token watcher error: {e}")
        stop_event.wait(TOKEN_WATCHER_INTERVAL_SECONDS)


_token_watcher_stop = threading.Event()
threading.Thread(
    target=_token_watcher_loop, args=(_token_watcher_stop,), daemon=True, name="token-watcher",
).start()


def save_config():
    with open(CONFIG_PATH, "w") as f:
        json.dump(config, f, indent=2)


def init_trader() -> CopyTrader | None:
    global _trader
    client = get_client()
    if not client or not config.get("leader_account_hash") or not config.get("follower_account_hashes"):
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
        for key in ("app_key", "redirect_uri", "leader_account_hash"):
            value = request.form.get(key, "").strip()
            if value:
                config[key] = value
        followers = [
            h.strip() for h in request.form.get("follower_account_hashes", "").splitlines() if h.strip()
        ]
        if followers:
            config["follower_account_hashes"] = followers
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
        token_status=get_token_status(),
        refresh_token_lifetime_days=REFRESH_TOKEN_LIFETIME_DAYS,
        email_alerts_configured=bool(SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_TO),
        alert_email_to=ALERT_EMAIL_TO,
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
        leader_hash=config.get("leader_account_hash", ""),
        follower_hashes=config.get("follower_account_hashes", []),
        combined_followers_value=COMBINED_FOLLOWERS,
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
        "token_status": get_token_status(),
        "email_alerts_configured": bool(SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_TO),
    })


@app.route("/api/test-alert-email", methods=["POST"])
@login_required
def api_test_alert_email():
    if not (SMTP_USERNAME and SMTP_PASSWORD and ALERT_EMAIL_TO):
        return jsonify({"error": "SMTP_USERNAME, SMTP_PASSWORD, and ALERT_EMAIL_TO must be set first."}), 400
    send_alert_email(
        "Schwab Copy Trader — test alert",
        "This is a test email from your Schwab Copy Trader — if you're reading this, email alerts are working.",
    )
    return jsonify({"sent": True})


@app.route("/api/positions")
@login_required
def api_positions():
    t = get_trader()
    if not t:
        return jsonify({"leader": [], "followers": {}})
    try:
        return jsonify({
            "leader": t.get_positions(config["leader_account_hash"]),
            "followers": {
                h: t.get_positions(h) for h in config["follower_account_hashes"]
            },
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/pnl")
@login_required
def api_pnl():
    t = get_trader()
    if not t:
        return jsonify({"error": "Not connected to Schwab"}), 400

    account = request.args.get("account", "")
    try:
        year = int(request.args.get("year"))
        month = int(request.args.get("month"))
    except (TypeError, ValueError):
        return jsonify({"error": "year and month query params are required"}), 400

    start = datetime(year, month, 1, tzinfo=timezone.utc)
    next_month = datetime(year + 1, 1, 1, tzinfo=timezone.utc) if month == 12 \
        else datetime(year, month + 1, 1, tzinfo=timezone.utc)
    end = next_month - timedelta(seconds=1)

    try:
        if account == COMBINED_FOLLOWERS:
            combined: dict[str, float] = {}
            for h in config["follower_account_hashes"]:
                for date, pnl in t.get_daily_pnl(h, start, end).items():
                    combined[date] = combined.get(date, 0) + pnl
            return jsonify({"pnl": combined})
        return jsonify({"pnl": t.get_daily_pnl(account, start, end)})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/order/<order_id>")
@login_required
def api_order_detail(order_id):
    client = get_client()
    if not client:
        return jsonify({"error": "Not connected to Schwab"}), 400
    default_account = next(iter(config.get("follower_account_hashes", [])), None)
    account_hash = request.args.get("account", default_account)
    try:
        resp = client.get_order(order_id, account_hash)
        return jsonify({"status_code": resp.status_code, "order": resp.json()})
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
