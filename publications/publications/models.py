"""Модели модуля публикаций.

Publication и Company — read-only представления таблиц backend (Р1).
Модуль не создаёт и не меняет их, только читает и обновляет статус публикации
после публикации на площадке. Свои таблицы: platform_post, comment_event.
"""
from datetime import datetime, timezone

from sqlalchemy import String, Text, DateTime, Integer, ForeignKey, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from publications.db import Base


def now():
    return datetime.now(timezone.utc)


class Publication(Base):
    __tablename__ = "publications"

    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int]
    task_id: Mapped[int]
    text: Mapped[str]
    image_url: Mapped[str | None]
    platform: Mapped[str] = mapped_column(String(20), default="vk")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), index=True)
    approved_by: Mapped[str | None]
    published_post_id: Mapped[str | None]
    published_url: Mapped[str | None]
    idempotency_key: Mapped[str]
    created_at: Mapped[datetime]


class Company(Base):
    __tablename__ = "companies"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str]
    telegram_chat_id: Mapped[str | None]
    vk_group_id: Mapped[str | None]
    vk_access_token: Mapped[str | None]  # зашифрован Fernet (тот же ENCRYPTION_KEY)
    approval_chat_id: Mapped[str | None]


class PlatformPost(Base):
    __tablename__ = "platform_posts"

    id: Mapped[int] = mapped_column(primary_key=True)
    publication_id: Mapped[int] = mapped_column(ForeignKey("publications.id"), index=True)
    platform: Mapped[str] = mapped_column(String(20))
    adapted_text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="scheduled", index=True)
    external_post_id: Mapped[str | None]
    published_url: Mapped[str | None]
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

    __table_args__ = (UniqueConstraint("publication_id", "platform", name="uq_pub_platform"),)


class CommentEvent(Base):
    __tablename__ = "comment_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    platform: Mapped[str] = mapped_column(String(20))
    platform_post_id: Mapped[int] = mapped_column(ForeignKey("platform_posts.id"), index=True)
    external_comment_id: Mapped[str]
    author_id: Mapped[str | None]
    text: Mapped[str]
    status: Mapped[str] = mapped_column(String(30), default="new", index=True)
    draft_reply: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

    __table_args__ = (UniqueConstraint("platform", "external_comment_id", name="uq_comment"),)
