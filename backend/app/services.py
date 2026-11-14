"""Application services for the Telegram conversation and AI-agent integration."""
from __future__ import annotations

import logging
from typing import Any

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import Company, MessageHistory

logger = logging.getLogger(__name__)

# Number of recent turns sent to the agent. The limit keeps requests bounded while
# preserving enough context for ordinary follow-up questions.
AI_HISTORY_LIMIT = 12
TELEGRAM_HISTORY_LIMIT = 10


def company_context(company: Company) -> dict[str, Any]:
    """Return only the company fields required by the AI-agent contract."""
    return {
        "id": company.id,  # legacy internal numeric id
        "company_id": company.external_id,  # canonical cross-module identifier
        "external_id": company.external_id,
        "name": company.name,
        "description": company.description,
        "tone_of_voice": company.tone_of_voice,
        "products": company.products,
        "target_audience": company.target_audience,
        "advantages": company.advantages,
    }


async def get_history(
    db: AsyncSession,
    company_id: int,
    chat_id: str,
    limit: int = AI_HISTORY_LIMIT,
) -> list[dict[str, str]]:
    """Get the most recent messages for this exact company/chat pair."""
    result = await db.execute(
        select(MessageHistory)
        .where(
            MessageHistory.company_id == company_id,
            MessageHistory.chat_id == chat_id,
        )
        .order_by(MessageHistory.created_at.desc(), MessageHistory.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    return [{"role": row.role, "content": row.content} for row in rows]


async def get_history_for_display(
    db: AsyncSession,
    company_id: int,
    chat_id: str,
    limit: int = TELEGRAM_HISTORY_LIMIT,
) -> list[MessageHistory]:
    """Return recent messages for the /history command, in chronological order."""
    result = await db.execute(
        select(MessageHistory)
        .where(
            MessageHistory.company_id == company_id,
            MessageHistory.chat_id == chat_id,
        )
        .order_by(MessageHistory.created_at.desc(), MessageHistory.id.desc())
        .limit(limit)
    )
    rows = list(result.scalars().all())
    rows.reverse()
    return rows


async def generate_content(
    company: Company,
    prompt: str,
    history: list[dict[str, str]],
) -> dict[str, str | None]:
    """Call the AI-agent using a stable backend-to-agent JSON contract.

    Request: {prompt, company, history}
    Response: {text, image_url?}
    """
    if not settings.ai_agent_url:
        raise RuntimeError("AI-агент пока не подключён: AI_AGENT_URL не задан")

    payload = {
        "prompt": prompt,
        "company": company_context(company),
        "history": history[-AI_HISTORY_LIMIT:],
    }

    timeout = httpx.Timeout(connect=10.0, read=90.0, write=30.0, pool=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(settings.ai_agent_url, json=payload)
        response.raise_for_status()
        data = response.json()

    if not isinstance(data, dict) or not isinstance(data.get("text"), str) or not data["text"].strip():
        raise ValueError("AI-агент вернул ответ без обязательного поля text")

    image_url = data.get("image_url")
    if image_url is not None and not isinstance(image_url, str):
        raise ValueError("Поле image_url в ответе AI-агента должно быть строкой")

    return {"text": data["text"].strip(), "image_url": image_url}
