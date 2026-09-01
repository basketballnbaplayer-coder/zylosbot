"""
Веб-панель бота: вход через Twitch/Google для стримеров, отдельный вход
для супер-админа. Все данные — из общей базы данных (models.py), той же,
которую использует bot.py.
"""

import os
import sys

from flask import Flask, render_template, redirect, url_for, request, session, flash
from flask_wtf import CSRFProtect
from dotenv import load_dotenv
load_dotenv()

sys.path.append(os.path.join(os.path.dirname(__file__), ".."))
from models import init_db, SessionLocal, Channel, ChannelSettings, CustomCommand, BannedWord, LogEntry, Timer, MIN_TIMER_MINUTES  # noqa: E402
from auth import auth_bp, init_oauth, login_required, admin_required  # noqa: E402

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY", "dev-only-change-me")
app.register_blueprint(auth_bp)
init_oauth(app)
init_db()
csrf = CSRFProtect(app)


@app.route("/")
def home():
    return render_template("home.html")


# ==================== ПАНЕЛЬ СТРИМЕРА ====================

@app.route("/dashboard")
@login_required
def dashboard():
    db = SessionLocal()
    channel = db.get(Channel, session["channel_id"])
    if not channel:
        db.close()
        session.clear()
        return redirect(url_for("auth.login"))

    commands = db.query(CustomCommand).filter_by(channel_id=channel.id).all()
    own_words = db.query(BannedWord).filter_by(channel_id=channel.id).all()
    global_words = db.query(BannedWord).filter(BannedWord.channel_id.is_(None)).all()
    timers = db.query(Timer).filter_by(channel_id=channel.id).all()
    logs = (
        db.query(LogEntry)
        .filter_by(channel_id=channel.id)
        .order_by(LogEntry.timestamp.desc())
        .limit(20)
        .all()
    )
    activity = [
        {"time": e.timestamp.strftime("%H:%M"), "kind": e.action, "text": e.content}
        for e in logs
    ]
    blocked_today = sum(1 for e in logs if e.action in ("warning", "timeout"))
    timeouts_today = sum(1 for e in logs if e.action == "timeout")
    welcomes_today = sum(1 for e in logs if e.action == "welcome")

    settings = channel.settings
    toggles = [
        ("words_filter_enabled", "Стоп-слова", "удаляет сообщения со словами из списка выше", settings.words_filter_enabled),
        ("links_filter_enabled", "Ссылки", "блокирует сообщения со ссылками от не-модераторов", settings.links_filter_enabled),
        ("caps_filter_enabled", "Капс", f"порог: {settings.caps_threshold}% заглавных букв в сообщении от 10 символов", settings.caps_filter_enabled),
        ("welcome_enabled", "Приветствие новых зрителей", "бот здоровается при первом сообщении в чате", settings.welcome_enabled),
    ]
    extension_toggles = [
        ("roulette_enabled", "Рулетка", "команда !рулетка — 1 шанс из 6 получить тайм-аут на 1 минуту, для веселья в чате", settings.roulette_enabled),
        ("antiraid_enabled", "Анти-рейд", "если 5+ разных зрителей за 10 сек пишут одно и то же — включает «только для фолловеров» на пару минут", settings.antiraid_enabled),
    ]
    data = dict(
        active="dashboard",
        title="Панель стримера",
        channel=channel,
        commands=commands,
        banned_words=own_words,
        global_words=global_words,
        settings=settings,
        toggles=toggles,
        extension_toggles=extension_toggles,
        timers=timers,
        min_timer_minutes=MIN_TIMER_MINUTES,
        activity=activity,
        blocked_today=blocked_today,
        timeouts_today=timeouts_today,
        new_viewers_today=welcomes_today,
    )
    db.close()
    return render_template("dashboard.html", **data)


@app.route("/dashboard/commands/add", methods=["POST"])
@login_required
def add_command():
    name = request.form.get("name", "").strip().lstrip("!").lower()
    response = request.form.get("response", "").strip()
    if name and response:
        db = SessionLocal()
        existing = db.query(CustomCommand).filter_by(channel_id=session["channel_id"], name=name).first()
        if existing:
            existing.response = response
        else:
            db.add(CustomCommand(channel_id=session["channel_id"], name=name, response=response))
        db.commit()
        db.close()
    return redirect(url_for("dashboard") + "#commands")


@app.route("/dashboard/commands/delete/<int:cmd_id>", methods=["POST"])
@login_required
def delete_command(cmd_id):
    db = SessionLocal()
    cmd = db.query(CustomCommand).filter_by(id=cmd_id, channel_id=session["channel_id"]).first()
    if cmd:
        db.delete(cmd)
        db.commit()
    db.close()
    return redirect(url_for("dashboard") + "#commands")


@app.route("/dashboard/words/add", methods=["POST"])
@login_required
def add_word():
    word = request.form.get("word", "").strip().lower()
    if word:
        db = SessionLocal()
        db.add(BannedWord(channel_id=session["channel_id"], word=word))
        db.commit()
        db.close()
    return redirect(url_for("dashboard") + "#words")


@app.route("/dashboard/words/delete/<int:word_id>", methods=["POST"])
@login_required
def delete_word(word_id):
    db = SessionLocal()
    w = db.query(BannedWord).filter_by(id=word_id, channel_id=session["channel_id"]).first()
    if w:
        db.delete(w)
        db.commit()
    db.close()
    return redirect(url_for("dashboard") + "#words")


@app.route("/dashboard/settings/toggle/<field>", methods=["POST"])
@login_required
def toggle_setting(field):
    allowed = {
        "caps_filter_enabled", "links_filter_enabled", "words_filter_enabled", "welcome_enabled",
        "roulette_enabled", "antiraid_enabled",
    }
    if field in allowed:
        db = SessionLocal()
        settings = db.query(ChannelSettings).filter_by(channel_id=session["channel_id"]).first()
        if settings:
            setattr(settings, field, not getattr(settings, field))
            db.commit()
        db.close()
    anchor = "extensions" if field in ("roulette_enabled", "antiraid_enabled") else "filters"
    return redirect(url_for("dashboard") + f"#{anchor}")


@app.route("/dashboard/timers/add", methods=["POST"])
@login_required
def add_timer():
    message = request.form.get("message", "").strip()
    try:
        interval = int(request.form.get("interval_minutes", MIN_TIMER_MINUTES))
    except ValueError:
        interval = MIN_TIMER_MINUTES
    interval = max(interval, MIN_TIMER_MINUTES)   # нельзя поставить чаще минимума

    if message:
        db = SessionLocal()
        db.add(Timer(channel_id=session["channel_id"], message=message, interval_minutes=interval, enabled=True))
        db.commit()
        db.close()
    return redirect(url_for("dashboard") + "#timers")


@app.route("/dashboard/timers/toggle/<int:timer_id>", methods=["POST"])
@login_required
def toggle_timer(timer_id):
    db = SessionLocal()
    timer = db.query(Timer).filter_by(id=timer_id, channel_id=session["channel_id"]).first()
    if timer:
        timer.enabled = not timer.enabled
        db.commit()
    db.close()
    return redirect(url_for("dashboard") + "#timers")


@app.route("/dashboard/timers/delete/<int:timer_id>", methods=["POST"])
@login_required
def delete_timer(timer_id):
    db = SessionLocal()
    timer = db.query(Timer).filter_by(id=timer_id, channel_id=session["channel_id"]).first()
    if timer:
        db.delete(timer)
        db.commit()
    db.close()
    return redirect(url_for("dashboard") + "#timers")


# ==================== СУПЕР-АДМИНКА ====================

@app.route("/admin")
@admin_required
def admin():
    db = SessionLocal()
    channels = db.query(Channel).all()
    command_counts = {c.id: db.query(CustomCommand).filter_by(channel_id=c.id).count() for c in channels}
    global_words = db.query(BannedWord).filter(BannedWord.channel_id.is_(None)).all()
    logs = db.query(LogEntry).order_by(LogEntry.timestamp.desc()).limit(30).all()
    blocked_total = sum(1 for e in logs if e.action in ("warning", "timeout"))

    global_activity = [
        {
            "time": e.timestamp.strftime("%H:%M"),
            "channel": e.channel.twitch_login if e.channel else "?",
            "kind": e.action,
            "text": e.content,
        }
        for e in logs
    ]
    data = dict(
        active="admin",
        title="Супер-админка",
        channels=channels,
        command_counts=command_counts,
        global_words=global_words,
        global_activity=global_activity,
        blocked_total=blocked_total,
    )
    db.close()
    return render_template("admin.html", **data)


@app.route("/admin/channels/toggle/<int:channel_id>", methods=["POST"])
@admin_required
def toggle_channel(channel_id):
    db = SessionLocal()
    channel = db.get(Channel, channel_id)
    if channel:
        channel.active = not channel.active
        db.commit()
    db.close()
    return redirect(url_for("admin"))


@app.route("/admin/words/add", methods=["POST"])
@admin_required
def admin_add_word():
    word = request.form.get("word", "").strip().lower()
    if word:
        db = SessionLocal()
        db.add(BannedWord(channel_id=None, word=word))
        db.commit()
        db.close()
    return redirect(url_for("admin"))


@app.route("/admin/words/delete/<int:word_id>", methods=["POST"])
@admin_required
def admin_delete_word(word_id):
    db = SessionLocal()
    w = db.query(BannedWord).filter_by(id=word_id, channel_id=None).first()
    if w:
        db.delete(w)
        db.commit()
    db.close()
    return redirect(url_for("admin"))


@app.route("/admin/logout")
def admin_logout():
    session.pop("is_admin", None)
    return redirect(url_for("home"))


if __name__ == "__main__":
    port = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug, host="0.0.0.0", port=port)
