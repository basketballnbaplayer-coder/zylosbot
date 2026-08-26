"""
Модели базы данных. Работает и с SQLite (для локальной разработки),
и с PostgreSQL (для продакшена на Render) — просто меняется переменная DATABASE_URL.
"""

import os
from datetime import datetime, timezone

from sqlalchemy import (
    create_engine, Column, Integer, String, Boolean, ForeignKey, DateTime, Text, or_
)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///bot.db")

# Render иногда выдаёт URL вида postgres://, а современный SQLAlchemy требует postgresql://
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

engine = create_engine(DATABASE_URL, echo=False, future=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False, future=True)

Base = declarative_base()


class Channel(Base):
    __tablename__ = "channels"

    id = Column(Integer, primary_key=True)
    twitch_login = Column(String(64), unique=True, nullable=False)   # ник канала, нижний регистр
    display_name = Column(String(64))
    owner_twitch_id = Column(String(32))       # twitch user id владельца (надёжный вход через Twitch)
    owner_google_id = Column(String(64))       # google sub владельца (вход через Google, без проверки владения каналом)
    verified = Column(Boolean, default=False)  # True — вход через Twitch; False — канал указан вручную после Google-входа
    is_admin_owner = Column(Boolean, default=False)  # исторический флаг, больше не используется для входа в супер-админку
    active = Column(Boolean, default=True)     # бот подключён к каналу прямо сейчас
    created_at = Column(DateTime, default=lambda: datetime.now(timezone.utc))

    commands = relationship("CustomCommand", back_populates="channel", cascade="all, delete-orphan")
    banned_words = relationship("BannedWord", back_populates="channel", cascade="all, delete-orphan")
    settings = relationship("ChannelSettings", back_populates="channel", uselist=False, cascade="all, delete-orphan")
    logs = relationship("LogEntry", back_populates="channel", cascade="all, delete-orphan")
    timers = relationship("Timer", back_populates="channel", cascade="all, delete-orphan")


class ChannelSettings(Base):
    __tablename__ = "channel_settings"

    channel_id = Column(Integer, ForeignKey("channels.id"), primary_key=True)
    caps_filter_enabled = Column(Boolean, default=True)
    links_filter_enabled = Column(Boolean, default=True)
    words_filter_enabled = Column(Boolean, default=True)
    caps_threshold = Column(Integer, default=70)   # в процентах
    welcome_enabled = Column(Boolean, default=True)
    welcome_message = Column(Text, default="Привет! Я {bot} 🛡 — защищаю чат от спама, ссылок и капса.")

    channel = relationship("Channel", back_populates="settings")


class CustomCommand(Base):
    __tablename__ = "custom_commands"

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    name = Column(String(64), nullable=False)
    response = Column(Text, nullable=False)

    channel = relationship("Channel", back_populates="commands")


class BannedWord(Base):
    __tablename__ = "banned_words"

    id = Column(Integer, primary_key=True)
    # channel_id = NULL значит слово глобальное — действует сразу на все каналы (управляется только супер-админом)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=True)
    word = Column(String(64), nullable=False)

    channel = relationship("Channel", back_populates="banned_words")


class LogEntry(Base):
    __tablename__ = "logs"

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    timestamp = Column(DateTime, default=lambda: datetime.now(timezone.utc))
    author = Column(String(64), nullable=True)
    content = Column(Text)
    action = Column(String(32))  # welcome / warning / timeout / command

    channel = relationship("Channel", back_populates="logs")


MIN_TIMER_MINUTES = 2


class Timer(Base):
    __tablename__ = "timers"

    id = Column(Integer, primary_key=True)
    channel_id = Column(Integer, ForeignKey("channels.id"), nullable=False)
    message = Column(Text, nullable=False)
    interval_minutes = Column(Integer, default=10)   # минимум MIN_TIMER_MINUTES, проверяется в коде
    enabled = Column(Boolean, default=True)
    last_sent = Column(DateTime, nullable=True)

    channel = relationship("Channel", back_populates="timers")


def init_db():
    Base.metadata.create_all(engine)


def get_banned_words_for(session, channel_id):
    """Стоп-слова канала + глобальные стоп-слова (для всех каналов сразу)."""
    rows = session.query(BannedWord).filter(
        or_(BannedWord.channel_id == channel_id, BannedWord.channel_id.is_(None))
    ).all()
    return {r.word for r in rows}
