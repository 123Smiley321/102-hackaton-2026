"""Due-publication scheduler backed exclusively by the internal Backend API.

The worker selects approved rows, releases its short queue locks, and asks the
backend to publish each ID. Only the backend calls VK/Telegram and changes the
canonical ``publications`` status. ``platform_posts`` is a worker-owned mirror used
by the VK comment monitor.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import httpx
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from publications.config import settings
from publications.db import SessionLocal
from publications.models import PlatformPost, Publication, now

logger = logging.getLogger(__name__)


@dataclass
class DispatchResult:
    status: str
    http_status: int | None
    payload: dict[str, Any]
    detail: str = ""


def publisher_headers() -> dict[str, str]:
    if not settings.publisher_internal_key:
        raise RuntimeError("PUBLISHER_INTERNAL_KEY не задан для publication worker")
    return {"X-Publisher-Key": settings.publisher_internal_key}


def _response_payload(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError:
        return {}
    return payload if isinstance(payload, dict) else {}


async def request_publish(client: httpx.AsyncClient, publication_id: int) -> DispatchResult:
    """Make exactly one backend request; there is no hidden retry loop here."""
    url = f"{settings.backend_url.rstrip('/')}/internal/publications/{publication_id}/publish"
    try:
        response = await client.post(url, headers=publisher_headers())
    except httpx.HTTPError as exc:
        # A retry is safe only through the backend status machine on a later poll:
        # `publishing`/`published` rows cannot be sent twice by this worker.
        return DispatchResult("dispatch_unknown", None, {}, type(exc).__name__)

    payload = _response_payload(response)
    detail = str(payload.get("detail", ""))[:500]
    if response.status_code == 200 and payload.get("status") in {"published", "already_published"}:
        return DispatchResult(payload["status"], response.status_code, payload)
    if response.status_code == 502:
        return DispatchResult("publish_unknown", response.status_code, payload, detail)
    if response.status_code == 409:
        return DispatchResult("conflict", response.status_code, payload, detail)
    if response.status_code in {401, 403}:
        return DispatchResult("blocked", response.status_code, payload, detail)
    return DispatchResult(
        "dispatch_unknown", response.status_code, payload,
        detail or f"Unexpected backend HTTP {response.status_code}",
    )


async def sync_mirror(publication_id: int, result: DispatchResult) -> None:
    """Synchronize the monitoring mirror without modifying backend-owned rows."""
    async with SessionLocal() as db:
        publication = await db.get(Publication, publication_id)
        if publication is None:
            return

        payload = result.payload
        if publication.status == "published" or result.status in {"published", "already_published"}:
            mirror_status = "published"
        elif publication.status == "publish_unknown" or result.status == "publish_unknown":
            mirror_status = "publish_unknown"
        elif result.status == "blocked":
            mirror_status = "blocked"
        elif result.status == "dispatch_unknown":
            mirror_status = "dispatch_unknown"
        else:
            mirror_status = publication.status

        external_post_id = payload.get("published_post_id") or publication.published_post_id
        published_url = payload.get("published_url") or publication.published_url
        error = None if mirror_status == "published" else (
            result.detail or publication.publish_error or result.status
        )
        values = {
            "publication_id": publication.id,
            "platform": publication.platform,
            "adapted_text": publication.text,
            "scheduled_at": publication.scheduled_at,
            "status": mirror_status,
            "external_post_id": external_post_id,
            "published_url": published_url,
            "attempt_count": publication.publish_attempts or 0,
            "last_error": str(error)[:500] if error else None,
            "updated_at": now(),
        }
        statement = pg_insert(PlatformPost).values(**values).on_conflict_do_update(
            constraint="uq_pub_platform",
            set_={
                "adapted_text": values["adapted_text"],
                "scheduled_at": values["scheduled_at"],
                "status": values["status"],
                "external_post_id": values["external_post_id"],
                "published_url": values["published_url"],
                "attempt_count": values["attempt_count"],
                "last_error": values["last_error"],
                "updated_at": values["updated_at"],
            },
        )
        await db.execute(statement)
        await db.commit()


async def reconcile_published() -> None:
    """Pick up manual ``mark_published`` resolutions for the comment monitor."""
    async with SessionLocal() as db:
        rows = await db.execute(
            select(Publication.id)
            .outerjoin(
                PlatformPost,
                and_(
                    PlatformPost.publication_id == Publication.id,
                    PlatformPost.platform == Publication.platform,
                ),
            )
            .where(Publication.status == "published")
            .where(or_(PlatformPost.id.is_(None), PlatformPost.status != "published"))
            .order_by(Publication.id)
            .limit(settings.publication_batch_size)
        )
        publication_ids = list(rows.scalars().all())
    for publication_id in publication_ids:
        await sync_mirror(
            publication_id,
            DispatchResult("reconciled", None, {}),
        )


async def process_due() -> int:
    await reconcile_published()
    current_time = datetime.now(timezone.utc)
    async with SessionLocal() as db:
        rows = await db.execute(
            select(Publication.id)
            .where(Publication.status == "approved")
            .where(
                (Publication.scheduled_at.is_(None))
                | (Publication.scheduled_at <= current_time)
            )
            .order_by(Publication.id)
            .limit(settings.publication_batch_size)
            .with_for_update(skip_locked=True)
        )
        publication_ids = list(rows.scalars().all())
        # Never hold these locks while Backend waits for a VK/Telegram response.
        await db.commit()

    if not publication_ids:
        return 0

    processed = 0
    timeout = httpx.Timeout(settings.backend_timeout_sec, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for publication_id in publication_ids:
            result = await request_publish(client, publication_id)
            await sync_mirror(publication_id, result)
            if result.status in {"published", "already_published"}:
                processed += 1
                logger.info("Publication %s: %s", publication_id, result.status)
            elif result.status == "publish_unknown":
                logger.warning("Publication %s requires manual reconciliation", publication_id)
            elif result.status == "conflict":
                logger.warning(
                    "Publication %s rejected by Backend state check: %s",
                    publication_id, result.detail,
                )
            else:
                logger.error(
                    "Publication %s was not dispatched (%s, HTTP %s): %s",
                    publication_id, result.status, result.http_status, result.detail,
                )
    return processed


async def scheduler_loop() -> None:
    publisher_headers()  # fail fast instead of polling forever with invalid configuration
    while True:
        try:
            await process_due()
        except Exception:
            logger.exception("Ошибка итерации планировщика")
        await asyncio.sleep(settings.scheduler_interval_sec)
