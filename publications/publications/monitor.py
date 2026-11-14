"""Мониторинг комментариев: VK в MVP. Автоответы запрещены — только черновики.

Поток: новый комментарий -> агент готовит черновик ответа -> черновик уходит
на согласование владельцу. Негатив (простая эвристика) — уведомление без черновика.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
from cryptography.fernet import Fernet
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from publications.adapters.telegram import TgAdapter
from publications.adapters.vk import VkAdapter
from publications.config import settings
from publications.db import Base, SessionLocal, engine
from publications.models import CommentEvent, Company, PlatformPost, Publication

logger = logging.getLogger(__name__)

NEGATIVE = re.compile(r"(жалоба|обманули|верните|вернуть|плохо|ужас|развод|возврат|претензия)", re.I)

NEGATIVE_HINT = "⚠️ Негативный комментарий — обработайте лично: "


async def ensure_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def decrypt_token(value: str | None) -> str:
    if not value:
        return ""
    if not settings.encryption_key:
        return value
    return Fernet(settings.encryption_key.encode()).decrypt(value.encode()).decode()


async def draft_reply(company: Company, comment_text: str) -> str:
    """Черновик ответа через AI-агент (тот же контракт, что у backend)."""
    if not settings.ai_agent_url:
        return "Спасибо за обратную связь! Ответим подробнее в ближайшее время."
    payload = {
        "prompt": f"Подготовь вежливый черновик ответа на комментарий к посту нашей компании. "
                  f"Комментарий: «{comment_text}». Ответ — от имени компании, коротко.",
        "company": {"id": company.id, "name": company.name, "tone_of_voice": ""},
        "history": [],
    }
    async with httpx.AsyncClient(timeout=90) as client:
        resp = await client.post(settings.ai_agent_url, json=payload)
        resp.raise_for_status()
        return resp.json().get("text", "").strip() or "Спасибо за обратную связь!"


async def monitor_once() -> int:
    handled = 0
    async with SessionLocal() as db:
        posts = (await db.execute(
            select(PlatformPost).where(
                PlatformPost.platform == "vk",
                PlatformPost.status == "published",
                PlatformPost.external_post_id.isnot(None),
            )
        )).scalars().all()

        for pp in posts:
            pub = await db.get(Publication, pp.publication_id)
            company = await db.get(Company, pub.company_id) if pub else None
            if not company or not company.vk_group_id:
                continue
            adapter = VkAdapter(decrypt_token(company.vk_access_token))
            try:
                comments = await adapter.fetch_comments(
                    target_id=company.vk_group_id, post_id=pp.external_post_id
                )
            except Exception:
                logger.exception("Не удалось получить комментарии поста %s", pp.id)
                continue

            for c in comments:
                stmt = pg_insert(CommentEvent).values(
                    platform="vk", platform_post_id=pp.id,
                    external_comment_id=c["external_comment_id"],
                    author_id=c["author_id"], text=c["text"], status="new",
                ).on_conflict_do_nothing(constraint="uq_comment")
                await db.execute(stmt)
            await db.commit()

            new_comments = (await db.execute(
                select(CommentEvent).where(
                    CommentEvent.platform_post_id == pp.id,
                    CommentEvent.status == "new",
                )
            )).scalars().all()

            chat_id = company.approval_chat_id or company.telegram_chat_id
            for ce in new_comments:
                if NEGATIVE.search(ce.text):
                    ce.status = "escalated"
                    text = NEGATIVE_HINT + ce.text[:500]
                else:
                    ce.draft_reply = await draft_reply(company, ce.text)
                    ce.status = "draft_reply"
                    text = (
                        "💬 Новый комментарий (черновик ответа на согласование):\n"
                        f"— {ce.text[:500]}\n\nОтвет: {ce.draft_reply[:1000]}"
                    )
                await db.commit()
                if chat_id:
                    await TgAdapter().notify(chat_id=str(chat_id), text=text)
                handled += 1
    return handled


async def monitor_loop() -> None:
    await ensure_tables()
    while True:
        try:
            await monitor_once()
        except Exception:
            logger.exception("Ошибка итерации мониторинга")
        await asyncio.sleep(settings.monitor_interval_sec)
