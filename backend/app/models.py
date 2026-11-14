from datetime import datetime, timezone
from sqlalchemy import String, Text, DateTime, ForeignKey, Integer, Boolean, UniqueConstraint, JSON
from sqlalchemy.orm import Mapped, mapped_column, relationship
from app.db import Base

def now():
    return datetime.now(timezone.utc)

class Company(Base):
    __tablename__ = "companies"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(200))
    # Stable external identifier shared with AI/content packages (e.g. salavatsteklo_demo).
    # Internal integer id remains the FK used by this backend and the publications worker.
    external_id: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    description: Mapped[str] = mapped_column(Text, default="")
    tone_of_voice: Mapped[str] = mapped_column(String(500), default="Деловой, дружелюбный")
    products: Mapped[str] = mapped_column(Text, default="")
    target_audience: Mapped[str] = mapped_column(Text, default="")
    advantages: Mapped[str] = mapped_column(Text, default="")
    brand_guidelines: Mapped[str] = mapped_column(Text, default="")
    enabled_platforms: Mapped[list] = mapped_column(JSON, default=list)
    approval_required: Mapped[bool] = mapped_column(Boolean, default=True)
    approval_mode: Mapped[str] = mapped_column(String(30), default="per_platform")
    telegram_chat_id: Mapped[str | None] = mapped_column(String(80), nullable=True, index=True)
    vk_group_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    vk_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)

    @property
    def company_id(self) -> str:
        """Alias for external_id: keeps CompanyOut.company_id serialization working."""
        return self.external_id
    approval_chat_id: Mapped[str | None] = mapped_column(String(80), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    source: Mapped[str] = mapped_column(String(30), default="telegram")
    prompt: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="received", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now, onupdate=now)

class Publication(Base):
    __tablename__ = "publications"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("tasks.id"), index=True)
    text: Mapped[str] = mapped_column(Text)
    image_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    platform: Mapped[str] = mapped_column(String(20), default="vk")
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending_approval", index=True)
    approved_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revision_comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    approval_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    published_post_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    published_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(100), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class MessageHistory(Base):
    __tablename__ = "message_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    chat_id: Mapped[str] = mapped_column(String(80), index=True)
    role: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id: Mapped[int] = mapped_column(primary_key=True)
    company_id: Mapped[int] = mapped_column(ForeignKey("companies.id"), index=True)
    actor: Mapped[str] = mapped_column(String(100))
    action: Mapped[str] = mapped_column(String(100))
    object_type: Mapped[str] = mapped_column(String(50))
    object_id: Mapped[str] = mapped_column(String(100))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
