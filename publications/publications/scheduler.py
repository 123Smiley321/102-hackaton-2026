"""Планировщик: забирает одобренные публикации из publications (таблица backend).

Интеграция без правок backend: approve-обработчик только ставит status='approved',
модуль подхватывает строку здесь. Идемпотентность: UniqueConstraint на
(publication_id, platform) + статусная машина platform_posts.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from cryptography.fernet import Fernet
from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from publications.adapt import adapt_for_platform
from publications.adapters.telegram import TgAdapter
from publications.adapters.vk import VkAdapter
from publications.config import settings
from publications.db import Base, SessionLocal, engine
from publications.models import Company, PlatformPost, Publication

logger = logging.getLogger(__name__)

BACKOFF = [5, 30, 120]  # секунды между попытками


def decrypt_token(value: str | None) -> str:
    if not value:
        return ""
    if not settings.encryption_key:
        return value  # MVP, как в backend: без ключа хранится открытым
    return Fernet(settings.encryption_key.encode()).decrypt(value.encode()).decode()


async def ensure_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


def make_adapter(platform: str, company: Company):
    if platform == "vk":
        return VkAdapter(decrypt_token(company.vk_access_token)), company.vk_group_id
    if platform == "telegram":
        return TgAdapter(), settings.telegram_target_chat or company.telegram_chat_id
    raise ValueError(f"Неизвестная площадка: {platform}")


async def process_due() -> int:
    now = datetime.now(timezone.utc)
    processed = 0
    async with SessionLocal() as db:
        # SKIP LOCKED: два воркера не возьмут одну строку
        result = await db.execute(
            select(Publication)
            .where(Publication.status == "approved")
            .where(
                (Publication.scheduled_at.is_(None)) | (Publication.scheduled_at <= now)
            )
            .order_by(Publication.id)
            .with_for_update(skip_locked=True)
        )
        publications = result.scalars().all()

        for pub in publications:
            company = await db.get(Company, pub.company_id)
            if company is None:
                continue
            try:
                adapter, target_id = make_adapter(pub.platform, company)
                if not target_id:
                    raise RuntimeError(f"У компании {company.id} не задан target для {pub.platform}")
                adapted_text = adapt_for_platform(pub.text, pub.platform, has_image=bool(pub.image_url))

                # upsert platform_post — повторный прогон не создаст дубль
                stmt = pg_insert(PlatformPost).values(
                    publication_id=pub.id, platform=pub.platform,
                    adapted_text=adapted_text, scheduled_at=pub.scheduled_at,
                    status="publishing",
                ).on_conflict_do_nothing(constraint="uq_pub_platform")
                await db.execute(stmt)
                await db.commit()

                pp = (await db.execute(
                    select(PlatformPost).where(
                        PlatformPost.publication_id == pub.id,
                        PlatformPost.platform == pub.platform,
                    ).with_for_update()
                )).scalar_one()

                if pp.status == "published":
                    continue  # уже опубликовано (защита от дублей при рестарте)

                try:
                    res = await adapter.publish(
                        target_id=str(target_id), text=adapted_text, image_url=pub.image_url
                    )
                except Exception as exc:
                    pp.attempt_count += 1
                    pp.last_error = str(exc)[:500]
                    if pp.attempt_count >= settings.max_attempts:
                        pp.status = "failed"
                        await notify_company(company, f"⚠️ Не удалось опубликовать (попыток: {pp.attempt_count}). Ошибка: {pp.last_error}")
                    else:
                        delay = BACKOFF[min(pp.attempt_count - 1, len(BACKOFF) - 1)]
                        await asyncio.sleep(delay)
                    await db.commit()
                    continue

                pp.status = "published"
                pp.external_post_id = res["post_id"]
                pp.published_url = res["url"]
                await db.execute(
                    update(Publication)
                    .where(Publication.id == pub.id)
                    .values(status="published", published_post_id=res["post_id"],
                            published_url=res["url"])
                )
                await db.commit()
                await notify_company(
                    company,
                    f"✅ Опубликовано на {pp.platform}: {res['url']}"
                    + (" (по расписанию)" if pub.scheduled_at else ""),
                )
                processed += 1
            except Exception:
                logger.exception("Публикация %s провалена", pub.id)
    return processed


async def notify_company(company: Company, text: str) -> None:
    chat_id = company.approval_chat_id or company.telegram_chat_id
    if chat_id:
        await TgAdapter().notify(chat_id=str(chat_id), text=text)


async def scheduler_loop() -> None:
    await ensure_tables()
    while True:
        try:
            await process_due()
        except Exception:
            logger.exception("Ошибка итерации планировщика")
        await asyncio.sleep(settings.scheduler_interval_sec)
