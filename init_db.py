"""
Разовый скрипт настройки. Делает три вещи:
1. Создаёт таблицы в базе данных.
2. Добавляет твой канал (из переменной CHANNEL в .env) и делает тебя супер-админом.
3. Переносит существующие commands.json и banned_words.txt в базу данных.

Запусти один раз перед первым стартом бота:
    python init_db.py

Дальше все команды/стоп-слова редактируются через сайт (или через !addcmd/!delcmd в чате) —
commands.json и banned_words.txt после этого больше не используются.
"""

import os
import json

from dotenv import load_dotenv
load_dotenv()

from models import init_db, SessionLocal, Channel, ChannelSettings, CustomCommand, BannedWord  # noqa: E402

CHANNEL_LOGIN = os.getenv("CHANNEL", "").lstrip("#").lower()
COMMANDS_FILE = os.path.join(os.path.dirname(__file__), "commands.json")
BANNED_WORDS_FILE = os.path.join(os.path.dirname(__file__), "banned_words.txt")


def main():
    if not CHANNEL_LOGIN:
        raise SystemExit("Заполни переменную CHANNEL в .env перед запуском.")

    init_db()
    session = SessionLocal()

    channel = session.query(Channel).filter_by(twitch_login=CHANNEL_LOGIN).first()
    if not channel:
        channel = Channel(
            twitch_login=CHANNEL_LOGIN,
            display_name=CHANNEL_LOGIN,
            is_admin_owner=True,   # твой собственный канал -> супер-админ
            verified=True,         # добавлен тобой напрямую, считаем подтверждённым
            active=True,
        )
        session.add(channel)
        session.commit()
        print(f"[+] Канал {CHANNEL_LOGIN} добавлен в базу (супер-админ).")
    else:
        print(f"[=] Канал {CHANNEL_LOGIN} уже есть в базе.")

    if not channel.settings:
        session.add(ChannelSettings(channel_id=channel.id))
        session.commit()
        print("[+] Настройки по умолчанию созданы.")

    # перенос команд из commands.json
    if os.path.exists(COMMANDS_FILE):
        with open(COMMANDS_FILE, "r", encoding="utf-8") as f:
            commands_data = json.load(f)
        added = 0
        for name, response in commands_data.items():
            exists = session.query(CustomCommand).filter_by(channel_id=channel.id, name=name).first()
            if not exists:
                session.add(CustomCommand(channel_id=channel.id, name=name, response=response))
                added += 1
        session.commit()
        print(f"[+] Перенесено новых команд: {added}")

    # перенос стоп-слов из banned_words.txt
    if os.path.exists(BANNED_WORDS_FILE):
        with open(BANNED_WORDS_FILE, "r", encoding="utf-8") as f:
            words = [w.strip().lower() for w in f if w.strip() and not w.startswith("#")]
        added = 0
        for word in words:
            exists = session.query(BannedWord).filter_by(channel_id=channel.id, word=word).first()
            if not exists:
                session.add(BannedWord(channel_id=channel.id, word=word))
                added += 1
        session.commit()
        print(f"[+] Перенесено новых стоп-слов: {added}")

    session.close()
    print("Готово! База данных настроена, можно запускать bot.py")


if __name__ == "__main__":
    main()
