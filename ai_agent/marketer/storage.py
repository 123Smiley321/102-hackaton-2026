"""Хранилище AI-модуля в общей БД PostgreSQL (SQLAlchemy 2.x async + asyncpg).

Все таблицы модуля начинаются с ai_, чтобы не пересекаться с таблицами бэкенда (company, task,
publication, platform_post ...). Идентификаторы компаний, задач и черновиков — UUID, как у бэкенда.

Изоляция компаний (ТЗ п. 4, критерий 9): к данным можно обратиться только через CompanyRepo,
который создаётся для одного company_id и добавляет фильтр company_id в КАЖДЫЙ запрос.
Чужой черновик/документ для репозитория просто «не существует» (404).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, Uuid, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class CompanyProfile(Base):
    """Профиль компании для AI. company_id — тот же UUID, что в таблице компаний бэкенда."""
    __tablename__ = "ai_company_profile"
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    data: Mapped[dict] = mapped_column(JSON, default=dict)  # отрасль, ЦА, тон, цвет бренда, анализ бизнеса
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class Document(Base):
    __tablename__ = "ai_document"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ai_company_profile.company_id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(500))
    source: Mapped[str] = mapped_column(String(1000))
    # facts — источник фактов для постов; rules — правила, сценарии, методички (в посты не цитируются)
    kind: Mapped[str] = mapped_column(String(16), default="facts")
    content_hash: Mapped[str] = mapped_column(String(64), default="", index=True)  # защита от повторной загрузки
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Chunk(Base):
    __tablename__ = "ai_chunk"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    document_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("ai_document.id", ondelete="CASCADE"))
    text: Mapped[str] = mapped_column(Text)


class Message(Base):
    __tablename__ = "ai_message"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    chat_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Decision(Base):
    """Решения и замечания клиента, которые агент обязан помнить («убрать рекламные обещания»)."""
    __tablename__ = "ai_decision"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    text: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Draft(Base):
    """Черновик агента. Публикацией (publication / platform_post) владеют бэкенд и модуль публикаций."""
    __tablename__ = "ai_draft"
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    task_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    parent_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    topic: Mapped[str] = mapped_column(String(500))
    version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(32), default="draft")
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ActionLog(Base):
    __tablename__ = "ai_action_log"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    company_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    action: Mapped[str] = mapped_column(String(64))
    details: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


DOC_KINDS = {"facts", "rules"}
DRAFT_STATUSES = {"draft", "needs_changes", "superseded", "approved", "rejected", "published"}


class CompanyNotFound(LookupError):
    pass


class NotFound(LookupError):
    pass


def make_engine(database_url: str):
    engine = create_async_engine(database_url, pool_pre_ping=True)
    return engine, async_sessionmaker(engine, expire_on_commit=False)


async def create_tables(engine) -> None:
    """Для хакатона создаём таблицы при старте; в проде — миграции Alembic."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def upsert_company(session: AsyncSession, company_id: uuid.UUID, name: str, data: dict) -> CompanyProfile:
    prof = await session.get(CompanyProfile, company_id)
    if prof is None:
        prof = CompanyProfile(company_id=company_id, name=name, data=data)
        session.add(prof)
    else:
        prof.name, prof.data = name, {**prof.data, **data}
    session.add(ActionLog(company_id=company_id, action="company_upserted", details=name))
    await session.commit()
    return prof


async def list_companies(session: AsyncSession) -> list[uuid.UUID]:
    return list((await session.scalars(select(CompanyProfile.company_id))).all())


class CompanyRepo:
    """Доступ к данным ровно одной компании."""

    def __init__(self, session: AsyncSession, company_id: uuid.UUID | str):
        self.s = session
        self.company_id = uuid.UUID(str(company_id))

    async def load(self) -> "CompanyRepo":
        self.profile_row = await self.s.get(CompanyProfile, self.company_id)
        if self.profile_row is None:
            raise CompanyNotFound(str(self.company_id))
        return self

    @property
    def profile(self) -> dict:
        return {"name": self.profile_row.name, **self.profile_row.data}

    async def update_profile(self, **fields) -> None:
        self.profile_row.data = {**self.profile_row.data, **fields}
        await self.log("profile_updated", ", ".join(fields))

    async def log(self, action: str, details: str = "") -> None:
        self.s.add(ActionLog(company_id=self.company_id, action=action, details=details))
        await self.s.commit()

    # ---------- база знаний ----------
    async def find_document_by_hash(self, content_hash: str) -> Document | None:
        return await self.s.scalar(select(Document).where(
            Document.company_id == self.company_id, Document.content_hash == content_hash))

    async def add_document(self, name: str, source: str, chunks: list[str], kind: str = "facts",
                           content_hash: str = "") -> uuid.UUID:
        if kind not in DOC_KINDS:
            raise ValueError(f"Тип документа: {sorted(DOC_KINDS)}")
        doc = Document(company_id=self.company_id, name=name, source=source, kind=kind, content_hash=content_hash)
        self.s.add(doc)
        await self.s.flush()
        self.s.add_all(Chunk(company_id=self.company_id, document_id=doc.id, text=c) for c in chunks)
        await self.log("document_added", f"{name} [{kind}] ({len(chunks)} фрагм.)")
        return doc.id

    async def all_chunks(self, kinds: tuple[str, ...] = ("facts",)) -> list[dict]:
        rows = await self.s.execute(
            select(Chunk.id, Chunk.text, Document.name)
            .join(Document, Document.id == Chunk.document_id)
            .where(Chunk.company_id == self.company_id, Document.company_id == self.company_id,
                   Document.kind.in_(kinds)))
        return [{"id": i, "text": t, "doc": n} for i, t, n in rows]

    async def documents(self) -> list[dict]:
        rows = await self.s.scalars(select(Document).where(Document.company_id == self.company_id))
        return [{"id": str(d.id), "name": d.name, "source": d.source, "kind": d.kind} for d in rows]

    # ---------- история и решения ----------
    async def add_message(self, chat_id, role: str, text: str) -> None:
        self.s.add(Message(company_id=self.company_id, chat_id=str(chat_id), role=role, text=text))
        await self.s.commit()

    async def history(self, chat_id, limit: int) -> list[dict]:
        rows = await self.s.scalars(
            select(Message).where(Message.company_id == self.company_id, Message.chat_id == str(chat_id))
            .order_by(Message.id.desc()).limit(limit))
        return [{"role": m.role, "text": m.text} for m in reversed(rows.all())]

    async def add_decision(self, text: str) -> None:
        self.s.add(Decision(company_id=self.company_id, text=text))
        await self.log("decision_saved", text)

    async def decisions(self) -> list[str]:
        rows = await self.s.scalars(select(Decision.text).where(
            Decision.company_id == self.company_id, Decision.active.is_(True)).order_by(Decision.id))
        return list(rows.all())

    # ---------- черновики ----------
    async def save_draft(self, topic: str, data: dict, *, version: int = 1, task_id=None, parent_id=None,
                         draft_id: uuid.UUID | None = None) -> Draft:
        d = Draft(id=draft_id or uuid.uuid4(), company_id=self.company_id, topic=topic, data=data,
                  version=version, task_id=uuid.UUID(str(task_id)) if task_id else None, parent_id=parent_id)
        self.s.add(d)
        await self.log("draft_saved", f"{d.id} v{version}: {topic}")
        return d

    async def get_draft(self, draft_id) -> Draft:
        d = await self.s.scalar(select(Draft).where(
            Draft.id == uuid.UUID(str(draft_id)), Draft.company_id == self.company_id))
        if d is None:
            raise NotFound(f"draft {draft_id}")
        return d

    async def last_draft(self) -> Draft | None:
        return await self.s.scalar(select(Draft).where(Draft.company_id == self.company_id)
                                   .order_by(Draft.created_at.desc(), Draft.version.desc()).limit(1))

    async def set_draft_status(self, draft_id, status: str) -> None:
        if status not in DRAFT_STATUSES:
            raise ValueError(f"Неизвестный статус {status}; допустимо: {sorted(DRAFT_STATUSES)}")
        res = await self.s.execute(update(Draft).where(
            Draft.id == uuid.UUID(str(draft_id)), Draft.company_id == self.company_id).values(status=status))
        if res.rowcount == 0:
            raise NotFound(f"draft {draft_id}")
        await self.log("draft_status", f"{draft_id} -> {status}")

    async def count(self, model) -> int:
        return await self.s.scalar(select(func.count()).select_from(model).where(model.company_id == self.company_id))
