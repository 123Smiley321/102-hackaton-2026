"""VK comment monitoring through Backend internal APIs.

No automatic public replies are sent. New comments produce either a draft reply for
approval or a negative-feedback alert. VK and Telegram tokens remain on Backend.
"""
from __future__ import annotations

import asyncio
import logging
import re

import httpx
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from publications.config import settings
from publications.db import SessionLocal
from publications.models import CommentEvent, Company, PlatformPost, Publication
from publications.scheduler import publisher_headers

logger = logging.getLogger(__name__)

NEGATIVE = re.compile(
    r"(жалоба|обманули|верните|вернуть|плохо|ужас|развод|возврат|претензия)", re.I
)


async def draft_reply(company: Company, comment_text: str) -> str:
    """Generate a private draft; failure falls back to a safe generic draft."""
    fallback = "Спасибо за обратную связь! Ответим подробнее в ближайшее время."
    if not settings.ai_agent_url:
        return fallback
    payload = {
        "prompt": (
            "Подготовь вежливый черновик ответа на комментарий к посту нашей компании. "
            f"Комментарий: «{comment_text}». Ответ — от имени компании, коротко."
        ),
        "knowledge_base": [],
        "company": {
            "id": company.id,
            "company_id": company.external_id,
            "external_id": company.external_id,
            "name": company.name,
            "description": company.description,
            "tone_of_voice": company.tone_of_voice,
            "products": company.products,
            "target_audience": company.target_audience,
            "advantages": company.advantages,
        },
        "history": [],
    }
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            response = await client.post(settings.ai_agent_url, json=payload)
            response.raise_for_status()
            data = response.json()
        text = data.get("text")
        return text.strip() if isinstance(text, str) and text.strip() else fallback
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        logger.warning("AI reply draft unavailable: %s", type(exc).__name__)
        return fallback


async def fetch_comments(client: httpx.AsyncClient, publication_id: int) -> list[dict]:
    url = f"{settings.backend_url.rstrip('/')}/internal/publications/{publication_id}/comments"
    response = await client.get(url, headers=publisher_headers())
    if response.status_code != 200:
        raise RuntimeError(f"Backend comments HTTP {response.status_code}")
    payload = response.json()
    comments = payload.get("comments", [])
    if not isinstance(comments, list):
        raise RuntimeError("Backend returned an invalid comments payload")
    return comments


async def notify_via_backend(
    client: httpx.AsyncClient, publication_id: int, text: str
) -> None:
    url = f"{settings.backend_url.rstrip('/')}/internal/publications/{publication_id}/notify"
    response = await client.post(url, headers=publisher_headers(), json={"text": text})
    if response.status_code != 200:
        raise RuntimeError(f"Backend notification HTTP {response.status_code}")


async def monitor_once() -> int:
    handled = 0
    async with SessionLocal() as db:
        rows = await db.execute(
            select(PlatformPost.id).where(
                PlatformPost.platform == "vk",
                PlatformPost.status == "published",
                PlatformPost.external_post_id.isnot(None),
            )
        )
        platform_post_ids = list(rows.scalars().all())

    timeout = httpx.Timeout(settings.backend_timeout_sec, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for platform_post_id in platform_post_ids:
            async with SessionLocal() as db:
                platform_post = await db.get(PlatformPost, platform_post_id)
                publication = (
                    await db.get(Publication, platform_post.publication_id)
                    if platform_post else None
                )
                company = (
                    await db.get(Company, publication.company_id)
                    if publication else None
                )
                if not platform_post or not publication or not company:
                    continue
                try:
                    comments = await fetch_comments(client, publication.id)
                except (httpx.HTTPError, RuntimeError, ValueError):
                    logger.exception("Не удалось получить комментарии публикации %s", publication.id)
                    continue

                for comment in comments:
                    statement = pg_insert(CommentEvent).values(
                        platform="vk",
                        platform_post_id=platform_post.id,
                        external_comment_id=str(comment["external_comment_id"]),
                        author_id=str(comment.get("author_id", "")),
                        text=str(comment.get("text", "")),
                        status="new",
                    ).on_conflict_do_nothing(constraint="uq_comment")
                    await db.execute(statement)
                await db.commit()

                new_comments = (await db.execute(
                    select(CommentEvent).where(
                        CommentEvent.platform_post_id == platform_post.id,
                        CommentEvent.status == "new",
                    ).order_by(CommentEvent.id)
                )).scalars().all()

                for comment_event in new_comments:
                    if NEGATIVE.search(comment_event.text):
                        final_status = "escalated"
                        notification = (
                            f"⚠️ Негативный комментарий к публикации №{publication.id} — "
                            f"обработайте лично:\n{comment_event.text[:500]}"
                        )
                    else:
                        final_status = "draft_reply"
                        comment_event.draft_reply = await draft_reply(company, comment_event.text)
                        notification = (
                            f"💬 Новый комментарий к публикации №{publication.id} "
                            "(черновик ответа на согласование):\n"
                            f"— {comment_event.text[:500]}\n\n"
                            f"Ответ: {comment_event.draft_reply[:1000]}"
                        )
                    if publication.published_url:
                        notification += f"\n{publication.published_url}"
                    # Keep the event `new` until the alert is confirmed by Backend.
                    # If the response is lost, an alert may repeat, but it is not lost.
                    await db.commit()
                    try:
                        await notify_via_backend(client, publication.id, notification)
                    except (httpx.HTTPError, RuntimeError):
                        logger.exception("Не удалось уведомить о комментарии %s", comment_event.id)
                        continue
                    comment_event.status = final_status
                    await db.commit()
                    handled += 1
    return handled


async def monitor_loop() -> None:
    publisher_headers()
    while True:
        try:
            await monitor_once()
        except Exception:
            logger.exception("Ошибка итерации мониторинга")
        await asyncio.sleep(settings.monitor_interval_sec)
