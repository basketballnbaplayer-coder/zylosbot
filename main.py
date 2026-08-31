import os
import re
import random
import asyncio
from datetime import datetime, timezone, timedelta

from dotenv import load_dotenv
load_dotenv()

from cachetools import TTLCache
from twitchio.ext import commands
from models import (
    init_db, SessionLocal, Channel, CustomCommand, BannedWord, LogEntry, Timer,
    get_banned_words_for, MIN_TIMER_MINUTES,
)

BOT_NICK = os.getenv("BOT_NICK")
TOKEN = os.getenv("TWITCH_OAUTH_TOKEN")

LINK_PATTERN = re.compile(r"(https?://|www\.)\S+", re.IGNORECASE)
CAPS_MIN_LEN = 10
BACKGROUND_LOOP_SECONDS = 30

STRIKE_RESET_HOURS = 24

LEET_MAP = str.maketrans({
    "0": "o", "1": "i", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s",
})

RAID_WINDOW_SECONDS = 10
RAID_MESSAGE_THRESHOLD = 5
RAID_FOLLOWERS_ONLY_MINUTES = 2

seen_users = TTLCache(maxsize=10000, ttl=43200)


def normalize_text(text: str) -> str:
    lowered = text.lower()
    no_separators = re.sub(r"[\s_\-.]+", "", lowered)
    return no_separators.translate(LEET_MAP)


def describe_api_error(error) -> str:
    text = str(error)
    lowered = text.lower()
    if "403" in text or "forbidden" in lowered or "not authorized" in lowered:
        text += (
            f"\n    Подсказка: возможно, у бота ({BOT_NICK}) нет прав модератора на этом "
            f"канале. Стример должен написать в своём чате: /mod {BOT_NICK}"
        )
    return text


def get_active_channel_logins():
    with SessionLocal() as session:
        return [c.twitch_login for c in session.query(Channel).filter_by(active=True).all()]


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
        self._background_started = False
        self.user_id = None
        self.recent_messages = {}
        self.raid_mode_active = set()

    async def event_ready(self):
        print(f"[OK] Бот запущен как {self.nick}. Каналы: {', '.join(self.channel_logins)}")

        try:
            bot_users = await self.fetch_users(names=[self.nick])
            if bot_users:
                self.user_id = bot_users[0].id
                print(f"[OK] ID бота успешно получен: {self.user_id}")
            else:
                print("[!] fetch_users вернул пустой список — ID бота не получен")
        except Exception as e:
            print(f"[!] Не удалось получить ID бота: {e}")

        with SessionLocal() as session:
            for login in self.channel_logins:
                channel_obj, settings = self.get_channel_data(login)
                if not settings or not settings.welcome_enabled or not channel_obj:
                    continue
                if self.recently_welcomed(session, channel_obj.id):
                    print(f"[=] {login}: приветствие недавно уже отправлялось, пропускаю")
                    continue
                channel = self.get_channel(login)
                if channel:
                    text = settings.welcome_message.replace("{bot}", self.nick)
                    await channel.send(text)
                    self.log(login, "welcome", text)

        if not self._background_started:
            self._background_started = True
            self.loop.create_task(self.background_loop())

    async def event_command_error(self, ctx, error):
        print(f"[!] Ошибка при выполнении команды '{ctx.message.content}' от {ctx.author.name}: {error}")

    def recently_welcomed(self, session, channel_id, cooldown_minutes=360):
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
        recent = (
            session.query(LogEntry)
            .filter_by(channel_id=channel_id, action="welcome")
            .filter(LogEntry.timestamp >= cutoff)
            .first()
        )
        return recent is not None

    async def background_loop(self):
        while True:
            try:
                await self.sync_channels()
                await self.check_timers()
                self._cleanup_raid_buckets()
            except Exception as e:
                print(f"[!] Ошибка в фоновом цикле: {e}")
            await asyncio.sleep(BACKGROUND_LOOP_SECONDS)

    def _cleanup_raid_buckets(self):
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=RAID_WINDOW_SECONDS)
        for login, bucket in list(self.recent_messages.items()):
            bucket[:] = [item for item in bucket if item[0] >= cutoff]
            if not bucket:
                del self.recent_messages[login]

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
        due_timers = []

        with SessionLocal() as session:
            for timer in session.query(Timer).filter_by(enabled=True).all():
                channel_obj = timer.channel
                if not channel_obj or not channel_obj.active:
                    continue
                interval = max(timer.interval_minutes, MIN_TIMER_MINUTES)
                if timer.last_sent is None or (now - timer.last_sent).total_seconds() >= interval * 60:
                    due_timers.append((timer.id, channel_obj.twitch_login, timer.message))

        for timer_id, login, message_text in due_timers:
            channel = self.get_channel(login)
            if not channel:
                continue
            try:
                await channel.send(message_text)
            except Exception as e:
                print(f"[!] Не удалось отправить таймер в {login}: {e}")
                continue
            with SessionLocal() as session:
                timer_obj = session.get(Timer, timer_id)
                if timer_obj:
                    timer_obj.last_sent = now
                    session.commit()
            self.log(login, "timer", message_text)

    def get_channel_data(self, login):
        with SessionLocal() as session:
            channel_obj = session.query(Channel).filter_by(twitch_login=login).first()
            settings = channel_obj.settings if channel_obj else None
            return channel_obj, settings

    def log(self, channel_login, action, content, author=None):
        with SessionLocal() as session:
            channel_obj = session.query(Channel).filter_by(twitch_login=channel_login).first()
            if channel_obj:
                session.add(LogEntry(channel_id=channel_obj.id, action=action, content=content, author=author))
                session.commit()

    def get_recent_strikes(self, channel_id, author):
        cutoff = datetime.now(timezone.utc) - timedelta(hours=STRIKE_RESET_HOURS)
        with SessionLocal() as session:
            return (
                session.query(LogEntry)
                .filter_by(channel_id=channel_id, author=author)
                .filter(LogEntry.action.in_(["warning", "timeout"]))
                .filter(LogEntry.timestamp >= cutoff)
                .count()
            )

    async def event_message(self, message):
        if message.echo:
            return

        login = message.channel.name.lower()
        author = message.author.name.lower()
        is_privileged = message.author.is_mod or author == login

        channel_obj, settings = self.get_channel_data(login)
        if not channel_obj or not settings:
            return

        await self.check_raid(message, login)

        if settings.welcome_enabled and message.tags.get("first-msg") == "1":
            key = f"{login}_{author}"
            if key not in seen_users:
                seen_users[key] = True
                text = f"Добро пожаловать в чат, {message.author.name}! 👋"
                await message.channel.send(text)
                self.log(login, "welcome", text, author=author)

        if not is_privileged:
            blocked = await self.check_moderation(message, channel_obj, settings)
            if blocked:
                return

        await self.handle_commands(message)

        if message.content.startswith("!"):
            parts = message.content[1:].split()
            if parts:
                cmd_name = parts[0].lower()

                if cmd_name == "рулетка":
                    await self.handle_roulette(message, login)
                    return

                response_text = None
                with SessionLocal() as session:
                    cmd = session.query(CustomCommand).filter_by(channel_id=channel_obj.id, name=cmd_name).first()
                    if cmd:
                        response_text = cmd.response
                if response_text:
                    await message.channel.send(response_text)
                    self.log(login, "command", response_text, author=author)

    async def check_moderation(self, message, channel_obj, settings) -> bool:
        content = message.content
        author = message.author.name.lower()
        login = message.channel.name.lower()
        normalized = normalize_text(content)

        if settings.words_filter_enabled:
            with SessionLocal() as session:
                banned_set = get_banned_words_for(session, channel_obj.id)
            if any(word and normalize_text(word) in normalized for word in banned_set):
                await self.punish(message, channel_obj, login, author, reason="запрещённое слово")
                return True

        if settings.links_filter_enabled and LINK_PATTERN.search(content):
            await self.punish(message, channel_obj, login, author, reason="ссылки запрещены")
            return True

        if settings.caps_filter_enabled:
            letters = [c for c in content if c.isalpha()]
            if len(content) >= CAPS_MIN_LEN and letters:
                caps_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
                if caps_ratio > settings.caps_threshold / 100:
                    await self.punish(message, channel_obj, login, author, reason="слишком много капса")
                    return True

        return False

    async def punish(self, message, channel_obj, login, author, reason):
        try:
            await message.delete()
        except Exception as e:
            print(f"[!] Не удалось удалить сообщение у {author} в {login}: {describe_api_error(e)}")

        strikes = self.get_recent_strikes(channel_obj.id, author) + 1

        if strikes == 1:
            text = f"@{author}, предупреждение: {reason}."
            await message.channel.send(text)
            self.log(login, "warning", text, author=author)
            return

        duration = 60 if strikes == 2 else 3600
        human = "1 минуту" if duration == 60 else "1 час"

        if not self.user_id:
            print(f"[!] Не могу выдать тайм-аут {author} в {login}: ID бота ещё не получен")
            text = f"@{author}, предупреждение: {reason}. (тайм-аут временно недоступен)"
            await message.channel.send(text)
            self.log(login, "warning", text, author=author)
            return

        try:
            users = await self.fetch_users(names=[login])
            if users:
                broadcaster = users[0]
                await broadcaster.timeout_user(
                    token=TOKEN,
                    moderator_id=self.user_id,
                    user_id=message.author.id,
                    duration=duration,
                    reason=reason,
                )
        except Exception as e:
            print(f"[!] Не удалось выдать тайм-аут {author} в {login}: {describe_api_error(e)}")

        text = f"@{author} получил тайм-аут на {human}: {reason}."
        await message.channel.send(text)
        self.log(login, "timeout", text, author=author)

    async def handle_roulette(self, message, login):
        if random.randint(1, 6) != 1:
            await message.channel.send(f"@{message.author.name}, в этот раз тебе повезло! 🍀")
            return

        if not self.user_id:
            print(f"[!] Рулетка: ID бота ещё не получен, не могу выдать тайм-аут в {login}")
            await message.channel.send(f"@{message.author.name}, повезло — не смог тебя наказать 😅")
            return

        try:
            users = await self.fetch_users(names=[login])
            if users:
                broadcaster = users[0]
                await broadcaster.timeout_user(
                    token=TOKEN,
                    moderator_id=self.user_id,
                    user_id=message.author.id,
                    duration=60,
                    reason="проиграл в рулетку",
                )
                await message.channel.send(
                    f"🔫 @{message.author.name} испытал судьбу... и проиграл! Тайм-аут на 1 минуту 😂"
                )
                self.log(login, "roulette", "проигрыш в рулетке — тайм-аут 60 сек", author=message.author.name.lower())
        except Exception as e:
            print(f"[!] Рулетка: не удалось выдать тайм-аут {message.author.name}: {describe_api_error(e)}")
            await message.channel.send(f"@{message.author.name}, повезло — не смог тебя наказать 😅")

    async def check_raid(self, message, login):
        if login in self.raid_mode_active:
            return

        normalized = normalize_text(message.content)
        if not normalized:
            return

        now = datetime.now(timezone.utc)
        bucket = self.recent_messages.setdefault(login, [])
        bucket.append((now, message.author.name.lower(), normalized))

        cutoff = now - timedelta(seconds=RAID_WINDOW_SECONDS)
        bucket[:] = [item for item in bucket if item[0] >= cutoff]

        matches = [item for item in bucket if item[2] == normalized]
        distinct_authors = {item[1] for item in matches}

        if len(distinct_authors) >= RAID_MESSAGE_THRESHOLD:
            self.raid_mode_active.add(login)
            self.loop.create_task(self.enable_raid_protection(login))

    async def enable_raid_protection(self, login):
        if not self.user_id:
            print(f"[!] Anti-raid: ID бота ещё не получен, не могу включить follower-only в {login}")
            self.raid_mode_active.discard(login)
            return

        channel = self.get_channel(login)
        try:
            users = await self.fetch_users(names=[login])
            if users and channel:
                broadcaster = users[0]
                await broadcaster.update_chat_settings(
                    token=TOKEN,
                    moderator_id=self.user_id,
                    follower_mode=True,
                    follower_mode_duration=0,
                )
                await channel.send(
                    "🚨 Похоже на спам-рейд! Включаю режим «только для фолловеров» "
                    f"на {RAID_FOLLOWERS_ONLY_MINUTES} мин."
                )
            self.log(login, "raid_protection", "Включен follower-only режим (анти-рейд)")
        except Exception as e:
            print(f"[!] Anti-raid: не удалось включить follower-only в {login}: {describe_api_error(e)}")

        await asyncio.sleep(RAID_FOLLOWERS_ONLY_MINUTES * 60)

        try:
            users = await self.fetch_users(names=[login])
            if users and channel:
                broadcaster = users[0]
                await broadcaster.update_chat_settings(
                    token=TOKEN,
                    moderator_id=self.user_id,
                    follower_mode=False,
                )
                await channel.send("✅ Всё спокойно, режим «только для фолловеров» выключен.")
            self.log(login, "raid_protection", "Follower-only режим выключен")
        except Exception as e:
            print(f"[!] Anti-raid: не удалось выключить follower-only в {login}: {describe_api_error(e)}")
        finally:
            self.raid_mode_active.discard(login)

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
        with SessionLocal() as session:
            channel_obj = session.query(Channel).filter_by(twitch_login=ctx.channel.name.lower()).first()
            cmds = session.query(CustomCommand).filter_by(channel_id=channel_obj.id).all() if channel_obj else []
            names_list = [c.name for c in cmds]
        if not names_list:
            await ctx.send("Пользовательских команд пока нет.")
            return
        names = ", ".join(f"!{n}" for n in names_list)
        await ctx.send(f"Доступные команды: {names}")

    @commands.command(name="addcmd")
    async def add_command(self, ctx, name: str = None, *, response: str = None):
        if not (ctx.author.is_mod or ctx.author.name.lower() == ctx.channel.name.lower()):
            return
        if not name or not response:
            await ctx.send("Использование: !addcmd название текст_ответа")
            return
        name = name.lstrip("!").lower()

        with SessionLocal() as session:
            channel_obj = session.query(Channel).filter_by(twitch_login=ctx.channel.name.lower()).first()
            if not channel_obj:
                await ctx.send("Канал не найден в базе данных.")
                return
            existing = session.query(CustomCommand).filter_by(channel_id=channel_obj.id, name=name).first()
            if existing:
                existing.response = response
            else:
                session.add(CustomCommand(channel_id=channel_obj.id, name=name, response=response))
            session.commit()
        await ctx.send(f"Команда !{name} добавлена/обновлена.")

    @commands.command(name="delcmd")
    async def del_command(self, ctx, name: str = None):
        if not (ctx.author.is_mod or ctx.author.name.lower() == ctx.channel.name.lower()):
            return
        if not name:
            return
        name = name.lstrip("!").lower()

        with SessionLocal() as session:
            channel_obj = session.query(Channel).filter_by(twitch_login=ctx.channel.name.lower()).first()
            if not channel_obj:
                await ctx.send("Канал не найден в базе данных.")
                return
            existing = session.query(CustomCommand).filter_by(channel_id=channel_obj.id, name=name).first()
            if existing:
                session.delete(existing)
                session.commit()
                found = True
            else:
                found = False
        if found:
            await ctx.send(f"Команда !{name} удалена.")
        else:
            await ctx.send("Такой команды нет.")


if __name__ == "__main__":
    if not TOKEN or not BOT_NICK:
        raise SystemExit("Заполни .env файл перед запуском.")

    bot = Bot()
    bot.run()
