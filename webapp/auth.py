"""
Вход на сайт: OAuth через Twitch и Google (для стримеров) + отдельный вход
для супер-админа по логину/паролю.
"""

import os
import sys
import requests
from functools import wraps

from flask import Blueprint, redirect, url_for, session, request, render_template, flash
from authlib.integrations.flask_client import OAuth
from werkzeug.security import generate_password_hash, check_password_hash

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from models import SessionLocal, Channel, ChannelSettings  # noqa: E402

TWITCH_CLIENT_ID = os.getenv("TWITCH_CLIENT_ID")
TWITCH_CLIENT_SECRET = os.getenv("TWITCH_CLIENT_SECRET")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "Admin")
ADMIN_PASSWORD_HASH = generate_password_hash(os.getenv("ADMIN_PASSWORD", "aaaaaaa"))

auth_bp = Blueprint("auth", __name__)
oauth = OAuth()


def init_oauth(app):
    oauth.init_app(app)

    if TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET:
        oauth.register(
            name="twitch",
            client_id=TWITCH_CLIENT_ID,
            client_secret=TWITCH_CLIENT_SECRET,
            access_token_url="https://id.twitch.tv/oauth2/token",
            authorize_url="https://id.twitch.tv/oauth2/authorize",
            client_kwargs={"scope": ""},
        )

    if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
        oauth.register(
            name="google",
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"},
        )


# ---------------- декораторы ----------------

def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("channel_id"):
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return wrapper


def admin_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        if not session.get("is_admin"):
            return redirect(url_for("auth.admin_login"))
        return f(*args, **kwargs)
    return wrapper


# ---------------- страница входа ----------------

@auth_bp.route("/login")
def login():
    return render_template(
        "login.html",
        twitch_ready=bool(TWITCH_CLIENT_ID),
        google_ready=bool(GOOGLE_CLIENT_ID),
    )


@auth_bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("home"))


# ---------------- вход через Twitch ----------------

@auth_bp.route("/login/twitch")
def login_twitch():
    redirect_uri = url_for("auth.twitch_callback", _external=True)
    return oauth.twitch.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/twitch/callback")
def twitch_callback():
    token = oauth.twitch.authorize_access_token()
    resp = requests.get(
        "https://api.twitch.tv/helix/users",
        headers={
            "Authorization": f"Bearer {token['access_token']}",
            "Client-Id": TWITCH_CLIENT_ID,
        },
        timeout=10,
    )
    data = resp.json().get("data", [])
    if not data:
        flash("Не удалось получить данные из Twitch. Попробуй ещё раз.")
        return redirect(url_for("auth.login"))

    user = data[0]
    twitch_id = user["id"]
    login_name = user["login"].lower()
    display_name = user.get("display_name", login_name)

    session_db = SessionLocal()
    channel = session_db.query(Channel).filter(
        (Channel.owner_twitch_id == twitch_id) | (Channel.twitch_login == login_name)
    ).first()

    if not channel:
        channel = Channel(
            twitch_login=login_name,
            display_name=display_name,
            owner_twitch_id=twitch_id,
            verified=True,
            active=True,
        )
        session_db.add(channel)
        session_db.commit()
        session_db.add(ChannelSettings(channel_id=channel.id))
        session_db.commit()
    else:
        channel.owner_twitch_id = twitch_id
        channel.display_name = display_name
        channel.verified = True
        if not channel.settings:
            session_db.add(ChannelSettings(channel_id=channel.id))
        session_db.commit()

    session["channel_id"] = channel.id
    session["channel_login"] = channel.twitch_login
    session_db.close()
    return redirect(url_for("dashboard"))


# ---------------- вход через Google ----------------

@auth_bp.route("/login/google")
def login_google():
    redirect_uri = url_for("auth.google_callback", _external=True)
    return oauth.google.authorize_redirect(redirect_uri)


@auth_bp.route("/auth/google/callback")
def google_callback():
    token = oauth.google.authorize_access_token()
    user_info = token.get("userinfo") or {}
    google_id = user_info.get("sub")
    if not google_id:
        flash("Не удалось получить данные из Google. Попробуй ещё раз.")
        return redirect(url_for("auth.login"))

    session_db = SessionLocal()
    channel = session_db.query(Channel).filter_by(owner_google_id=google_id).first()
    session_db.close()

    if channel:
        session["channel_id"] = channel.id
        session["channel_login"] = channel.twitch_login
        return redirect(url_for("dashboard"))

    # первый вход через Google — ещё не знаем, какой у человека Twitch-канал
    session["pending_google_id"] = google_id
    session["pending_google_name"] = user_info.get("name", "")
    return redirect(url_for("auth.link_twitch"))


@auth_bp.route("/link-twitch", methods=["GET", "POST"])
def link_twitch():
    google_id = session.get("pending_google_id")
    if not google_id:
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        login_name = request.form.get("twitch_login", "").strip().lstrip("@").lower()
        if not login_name:
            flash("Введи название канала.")
            return render_template("link_twitch.html")

        session_db = SessionLocal()
        existing = session_db.query(Channel).filter_by(twitch_login=login_name).first()
        if existing:
            session_db.close()
            flash("Этот канал уже зарегистрирован. Если это твой канал — войди через Twitch.")
            return render_template("link_twitch.html")

        channel = Channel(
            twitch_login=login_name,
            display_name=login_name,
            owner_google_id=google_id,
            verified=False,   # не подтверждено — вход был не через Twitch
            active=True,
        )
        session_db.add(channel)
        session_db.commit()
        session_db.add(ChannelSettings(channel_id=channel.id))
        session_db.commit()

        session.pop("pending_google_id", None)
        session.pop("pending_google_name", None)
        session["channel_id"] = channel.id
        session["channel_login"] = channel.twitch_login
        session_db.close()
        return redirect(url_for("dashboard"))

    return render_template("link_twitch.html")


# ---------------- вход супер-админа ----------------

@auth_bp.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        username = request.form.get("username", "")
        password = request.form.get("password", "")
        if username == ADMIN_USERNAME and check_password_hash(ADMIN_PASSWORD_HASH, password):
            session["is_admin"] = True
            return redirect(url_for("admin"))
        flash("Неверный логин или пароль.")
    return render_template("admin_login.html")
