"""
Бот для Twitch-чата: команды, модерация, приветствие новых зрителей.
Многоканальная версия — все данные хранятся в базе данных.
"""

import os
import re
import asyncio
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
from datetime import datetime, timezone

from dotenv import load_dotenv
load_dotenv()

# --- Заглушка портов для Render (Health Check) ---
class HealthCheck(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")
    def log_message(self, format, *args):
        pass

def start_health_check():
    port = int(os.getenv("PORT", 8080))
    server = HTTPServer(("0.0.0.0", port), HealthCheck)
    server.serve_forever()

threading.Thread(target=start_health_check, daemon=True).start()
# --------------------------------------------------

from twitchio.ext import commands
from models import (
    init_db, SessionLocal, Channel, CustomCommand, BannedWord, LogEntry, Timer,
    get_banned_words_for, MIN_TIMER_MINUTES,
)

BOT_NICK = os.getenv("BOT_NICK")
TOKEN = os.getenv("TWITCH_OAUTH_TOKEN")

LINK_PATTERN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
CAPS_MIN_LEN = 10
BACKGROUND_LOOP_SECONDS = 30  # как часто проверяем таймеры и список активных каналов

warnings = {}       # (канал, ник) -> количество предупреждений
seen_users = {}     # канал -> set(ников, кого уже поприветствовали в этой сессии)


def get_active_channel_logins():
    session = SessionLocal()
    logins = [c.twitch_login for c in session.query(Channel).filter_by(active=True).all()]
    session.close()
    return logins


class Bot(commands.Bot):
    def __init__(self):
        init_db()
        self.channel_logins = get_active_channel_logins()
        if not self.channel_logins:
            raise SystemExit("В базе нет ни одного активного канала. Сначала запусти init_db.py.")

        super().__init__(
            token=TOKEN,
            prefix="!",
            initial_channels=self.channel_logins,
        )
        self.start_time = datetime.now(timezone.utc)

    async def event_ready(self):
        print(f"[OK] Бот запущен как {self.nick}. Каналы: {', '.join(self.channel_logins)}")
        for login in self.channel_logins:
            channel_obj, settings = self.get_channel_data(login)
            if not settings or not settings.welcome_enabled:
                continue
            channel = self.get_channel(login)
            if channel:
                text = settings.welcome_message.replace("{bot}", self.nick)
                await channel.send(text)
                self.log(login, "welcome", text)

        self.loop.create_task(self.background_loop())

    async def background_loop(self):
        """Раз в BACKGROUND_LOOP_SECONDS: подхватывает новые/отключённые каналы
        из базы (без перезапуска бота) и рассылает таймеры, которым пора сработать."""
        while True:
            try:
                await self.sync_channels()
                await self.check_timers()
            except Exception as e:
                print(f"[!] Ошибка в фоновом цикле: {e}")
            await asyncio.sleep(BACKGROUND_LOOP_SECONDS)

    async def sync_channels(self):
        active_logins = set(get_active_channel_logins())
        current_logins = {c.name.lower() for c in self.connected_channels}

        to_join = active_logins - current_logins
        to_part = current_logins - active_logins

        if to_join:
            await self.join_channels(list(to_join))
            for login in to_join:
                print(f"[+] Подключился к новому каналу: {login}")

        if to_part:
            await self.part_channels(list(to_part))
            for login in to_part:
                print(f"[-] Отключился от канала: {login}")

        if to_join or to_part:
            self.channel_logins = list(active_logins)

    async def check_timers(self):
        now = datetime.now(timezone.utc)
        session = SessionLocal()
        due_timers = []
        for timer in session.query(Timer).filter_by(enabled=True).all():
            channel_obj = timer.channel
            if not channel_obj or not channel_obj.active:
                continue
            interval = max(timer.interval_minutes, MIN_TIMER_MINUTES)
            if timer.last_sent is None or (now - timer.last_sent).total_seconds() >= interval * 60:
                due_timers.append((timer, channel_obj.twitch_login))

        for timer, login in due_timers:
            channel = self.get_channel(login)
            if channel:
                await channel.send(timer.message)
                timer.last_sent = now
                self.log(login, "timer", timer.message)

        session.commit()
        session.close()

    def get_channel_data(self, login):
        """Возвращает (Channel, ChannelSettings) из базы по логину канала."""
        session = SessionLocal()
        channel_obj = session.query(Channel).filter_by(twitch_login=login).first()
        settings = channel_obj.settings if channel_obj else None
        session.close()
        return channel_obj, settings

    def log(self, channel_login, action, content, author=None):
        session = SessionLocal()
        channel_obj = session.query(Channel).filter_by(twitch_login=channel_login).first()
        if channel_obj:
            session.add(LogEntry(channel_id=channel_obj.id, action=action, content=content, author=author))
            session.commit()
        session.close()

    async def event_message(self, message):
        if message.echo:
            return

        login = message.channel.name.lower()
        author = message.author.name.lower()
        is_privileged = message.author.is_mod or author == login

        channel_obj, settings = self.get_channel_data(login)
        if not channel_obj or not settings:
            return  # канал не настроен в базе — игнорируем

        # приветствие новых зрителей (Twitch помечает первое сообщение тегом first-msg)
        if settings.welcome_enabled and message.tags.get("first-msg") == "1":
            seen = seen_users.setdefault(login, set())
            if author not in seen:
                seen.add(author)
                text = f"Добро пожаловать в чат, {message.author.name}! 👋"
                await message.channel.send(text)
                self.log(login, "welcome", text, author=author)

        # модерация (модов и стримера не трогаем)
        if not is_privileged:
            blocked = await self.check_moderation(message, channel_obj, settings)
            if blocked:
                return

        # встроенные команды twitchio (!hello, !uptime и т.д.)
        await self.handle_commands(message)

        # пользовательские команды из базы данных
        if message.content.startswith("!"):
            parts = message.content[1:].split()
            if parts:
                cmd_name = parts[0].lower()
                session = SessionLocal()
                cmd = session.query(CustomCommand).filter_by(channel_id=channel_obj.id, name=cmd_name).first()
                session.close()
                if cmd:
                    await message.channel.send(cmd.response)
                    self.log(login, "command", cmd.response, author=author)

    async def check_moderation(self, message, channel_obj, settings) -> bool:
        content = message.content
        author = message.author.name
        login = message.channel.name.lower()
        lowered = content.lower()

        if settings.words_filter_enabled:
            words_in_message = set(re.findall(r"\w+", lowered, flags=re.UNICODE))
            session = SessionLocal()
            banned_set = get_banned_words_for(session, channel_obj.id)
            session.close()
            if words_in_message & banned_set:
                await self.punish(message, login, author, base_seconds=10, reason="запрещённое слово")
                return True

        if settings.links_filter_enabled and LINK_PATTERN.search(content):
            await self.punish(message, login, author, base_seconds=10, reason="ссылки запрещены")
            return True

        if settings.caps_filter_enabled:
            letters = [c for c in content if c.isalpha()]
            if len(content) >= CAPS_MIN_LEN and letters:
                caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
                if caps_ratio > settings.caps_threshold / 100:
                    await self.punish(message, login, author, base_seconds=5, reason="слишком много капса")
                    return True

        return False

    async def punish(self, message, login, author, base_seconds, reason):
        try:
            await message.delete()
        except Exception as e:
            print(f"[!] Не удалось удалить сообщение: {e}")

        key = (login, author)
        warnings[key] = warnings.get(key, 0) + 1
        count = warnings[key]

        if count == 1:
            text = f"@{author}, предупреждение: {reason}."
            await message.channel.send(text)
            self.log(login, "warning", text, author=author)
        else:
            await message.channel.send(f"/timeout {author} {base_seconds * count}")
            text = f"@{author} получил тайм-аут: {reason}."
            await message.channel.send(text)
            self.log(login, "timeout", text, author=author)

    # ---------------- встроенные команды ----------------

    @commands.command(name="hello")
    async def hello(self, ctx):
        await ctx.send(f"Привет, {ctx.author.name}! 👋")

    @commands.command(name="uptime")
    async def uptime(self, ctx):
        delta = datetime.now(timezone.utc) - self.start_time
        hours, remainder = divmod(int(delta.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)
        await ctx.send(f"Бот работает уже {hours} ч. {minutes} мин.")

    @commands.command(name="so")
    async def shoutout(self, ctx, user: str = None):
        if not user:
            await ctx.send("Использование: !so <ник>")
            return
        user = user.lstrip("@")
        await ctx.send(f"Загляните на канал {user} — twitch.tv/{user} 🎉")

    @commands.command(name="commands")
    async def list_commands(self, ctx):
        session = SessionLocal()
        channel_obj = session.query(Channel).filter_by(twitch_login=ctx.channel.name.lower()).first()
        cmds = session.query(CustomCommand).filter_by(channel_id=channel_obj.id).all() if channel_obj else []
        session.close()
        if not cmds:
            await ctx.send("Пользовательских команд пока нет.")
            return
        names = ", ".join(f"!{c.name}" for c in cmds)
        await ctx.send(f"Доступные команды: {names}")

    @commands.command(name="addcmd")
    async def add_command(self, ctx, name: str = None, *, response: str = None):
        if not (ctx.author.is_mod or ctx.author.name.lower() == ctx.channel.name.lower()):
            return
        if not name or not response:
            await ctx.send("Использование: !addcmd название текст_ответа")
            return
        name = name.lstrip("!").lower()

        session = SessionLocal()
        channel_obj = session.query(Channel).filter_by(twitch_login=ctx.channel.name.lower()).first()
        existing = session.query(CustomCommand).filter_by(channel_id=channel_obj.id, name=name).first()
        if existing:
            existing.response = response
        else:
            session.add(CustomCommand(channel_id=channel_obj.id, name=name, response=response))
        session.commit()
        session.close()
        await ctx.send(f"Команда !{name} добавлена/обновлена.")

    @commands.command(name="delcmd")
    async def del_command(self, ctx, name: str = None):
        if not (ctx.author.is_mod or ctx.author.name.lower() == ctx.channel.name.lower()):
            return
        if not name:
            return
        name = name.lstrip("!").lower()

        session = SessionLocal()
        channel_obj = session.query(Channel).filter_by(twitch_login=ctx.channel.name.lower()).first()
        existing = session.query(CustomCommand).filter_by(channel_id=channel_obj.id, name=name).first()
        if existing:
            session.delete(existing)
            session.commit()
            await ctx.send(f"Команда !{name} удалена.")
        else:
            await ctx.send("Такой команды нет.")
        session.close()


if __name__ == "__main__":
    if not TOKEN or not BOT_NICK:
        raise SystemExit("Заполни .env файл (см. .env.example) перед запуском.")

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    bot = Bot()
    bot.run()
