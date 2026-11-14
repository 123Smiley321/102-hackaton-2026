"""Backend-owned VK/Telegram/MAX API calls and operator notifications.

The publication worker never receives platform tokens. These platforms do not provide
an idempotency key for sending posts, so ambiguous publishing outcomes are handled by
the API endpoint as ``publish_unknown`` and require manual reconciliation.
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

from app.config import settings
from app.models import Company, Publication
from app.security import decrypt_secret

VK_API = "https://api.vk.com/method"
MAX_API = "https://platform-api2.max.ru"


class PublicationNotConfiguredError(RuntimeError):
    """A deterministic local configuration error; no external post was attempted."""


class PublicationRejectedError(RuntimeError):
    """The platform definitively rejected the request with an HTTP 4xx response."""


async def _validate_image_url(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise RuntimeError("Изображение должно иметь безопасный HTTPS URL")
    host = parsed.hostname.lower()
    if host in {"localhost", "localhost.localdomain"} or host.endswith((".local", ".internal")):
        raise RuntimeError("Недопустимый адрес изображения")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(
                host, parsed.port or 443, type=socket.SOCK_STREAM
            )
            addresses = [ipaddress.ip_address(info[4][0]) for info in infos]
        except Exception as exc:
            raise RuntimeError("Не удалось проверить адрес изображения") from exc
    if not addresses or any(not address.is_global for address in addresses):
        raise RuntimeError("Изображение указывает на непубличный адрес")


async def _vk_call(client: httpx.AsyncClient, method: str, params: dict):
    response = await client.post(f"{VK_API}/{method}", data=params)
    response.raise_for_status()
    data = response.json()
    if "error" in data:
        err = data["error"]
        raise RuntimeError(
            f"VK API error {err.get('error_code')}: {err.get('error_msg', 'unknown')}"
        )
    return data.get("response")


async def _vk_upload_photo(
    client: httpx.AsyncClient, token: str, group_id: str, image_url: str
) -> str:
    upload_info = await _vk_call(
        client,
        "photos.getWallUploadServer",
        {"group_id": group_id, "access_token": token, "v": settings.vk_api_version},
    )
    await _validate_image_url(image_url)
    image_response = await client.get(image_url, follow_redirects=False)
    image_response.raise_for_status()
    if len(image_response.content) > 10 * 1024 * 1024:
        raise RuntimeError("Изображение превышает лимит 10 МБ")
    content_type = image_response.headers.get("content-type", "image/jpeg")
    if not content_type.startswith("image/"):
        raise RuntimeError("Ссылка изображения не ведёт к файлу изображения")
    uploaded = await client.post(
        upload_info["upload_url"],
        files={"photo": ("publication-image", image_response.content, content_type)},
    )
    uploaded.raise_for_status()
    upload_data = uploaded.json()
    saved = await _vk_call(
        client,
        "photos.saveWallPhoto",
        {
            "group_id": group_id,
            "photo": upload_data["photo"],
            "server": upload_data["server"],
            "hash": upload_data["hash"],
            "access_token": token,
            "v": settings.vk_api_version,
        },
    )
    photo = saved[0]
    return f"photo{photo['owner_id']}_{photo['id']}"


async def publish_vk(company: Company, publication: Publication) -> tuple[str, str | None]:
    token = decrypt_secret(company.vk_access_token)
    if not token or not company.vk_group_id:
        raise RuntimeError("Для компании не настроены VK community token и vk_group_id")
    group_id = str(company.vk_group_id).lstrip("-")
    timeout = httpx.Timeout(40.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        attachments = None
        if publication.image_url:
            attachments = await _vk_upload_photo(
                client, token, group_id, publication.image_url
            )
        params = {
            "owner_id": f"-{group_id}",
            "from_group": 1,
            "message": publication.text,
            "access_token": token,
            "v": settings.vk_api_version,
        }
        if attachments:
            params["attachments"] = attachments
        result = await _vk_call(client, "wall.post", params)
    post_id = str(result["post_id"])
    return post_id, f"https://vk.com/wall-{group_id}_{post_id}"


async def publish_telegram(
    company: Company, publication: Publication
) -> tuple[str, str | None]:
    token = settings.telegram_bot_token
    chat_id = company.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError(
            "Для компании не настроены TELEGRAM_BOT_TOKEN и telegram_chat_id"
        )
    timeout = httpx.Timeout(40.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        if publication.image_url:
            await _validate_image_url(publication.image_url)
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendPhoto",
                data={
                    "chat_id": chat_id,
                    "caption": publication.text[:1024],
                    "photo": publication.image_url,
                },
            )
        else:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={
                    "chat_id": chat_id,
                    "text": publication.text,
                    "disable_web_page_preview": False,
                },
            )
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            raise RuntimeError(f"Telegram API error: {data.get('description', 'unknown')}")
        message = data["result"]
        if publication.image_url and len(publication.text) > 1024:
            followup = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": publication.text[1024:]},
            )
            followup.raise_for_status()
            followup_data = followup.json()
            if not followup_data.get("ok"):
                raise RuntimeError(
                    f"Telegram API error: {followup_data.get('description', 'unknown')}"
                )
    message_id = str(message["message_id"])
    post_url = (
        f"https://t.me/c/{str(chat_id).replace('-100', '')}/{message_id}"
        if str(chat_id).startswith("-100")
        else None
    )
    return message_id, post_url


def _max_rejection(response: httpx.Response, token: str) -> PublicationRejectedError:
    """Build a bounded error without leaking the access token or response headers."""
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    code = str(payload.get("code") or "unknown")[:120]
    message = str(payload.get("message") or "request rejected")[:300]
    message = message.replace(token, "[redacted]")
    return PublicationRejectedError(
        f"MAX API HTTP {response.status_code} ({code}): {message}"
    )


async def _publish_max_with_client(
    client: httpx.AsyncClient,
    token: str,
    chat_id: int,
    publication: Publication,
) -> tuple[str, str | None]:
    body: dict = {"text": publication.text}
    if publication.image_url:
        # MAX accepts a direct image URL; no local file upload or fake media ID is needed.
        await _validate_image_url(publication.image_url)
        body["attachments"] = [
            {"type": "image", "payload": {"url": publication.image_url}}
        ]
    response = await client.post(
        f"{MAX_API}/messages",
        params={"chat_id": chat_id},
        headers={"Authorization": token},
        json=body,
    )
    if 400 <= response.status_code < 500:
        raise _max_rejection(response, token)
    if response.status_code >= 500:
        response.raise_for_status()
    if response.status_code != 200:
        raise RuntimeError(f"MAX API returned unexpected HTTP {response.status_code}")
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError("MAX API returned HTTP 200 with invalid JSON") from exc
    if not isinstance(data, dict):
        raise RuntimeError("MAX API returned an unexpected response schema")
    if data.get("success") is False:
        code = str(data.get("code") or "unknown")[:120]
        message = str(data.get("message") or "request rejected")[:300]
        message = message.replace(token, "[redacted]")
        raise PublicationRejectedError(f"MAX API rejected request ({code}): {message}")

    # Official schema: {"message": {"message": {"mid": "..."}, "url": "..."}}.
    message = data.get("message")
    if not isinstance(message, dict):
        raise RuntimeError("MAX API HTTP 200 did not contain a message object")
    message_payload = message.get("message")
    if not isinstance(message_payload, dict) or not message_payload.get("mid"):
        raise RuntimeError("MAX API HTTP 200 did not contain message.message.mid")
    post_url = message.get("url")
    return (
        str(message_payload["mid"]),
        str(post_url) if isinstance(post_url, str) and post_url else None,
    )


async def publish_max(
    company: Company,
    publication: Publication,
    client: httpx.AsyncClient | None = None,
) -> tuple[str, str | None]:
    """Publish through the official MAX Bot API, never through a simulated success path."""
    if not company.max_access_token or not company.max_chat_id:
        raise PublicationNotConfiguredError(
            "MAX not_configured: задайте max_access_token и max_chat_id для компании"
        )
    try:
        chat_id = int(str(company.max_chat_id).strip())
    except (TypeError, ValueError) as exc:
        raise PublicationNotConfiguredError(
            "MAX not_configured: max_chat_id должен быть целым идентификатором чата или канала"
        ) from exc
    if not -(2**63) <= chat_id < 2**63:
        raise PublicationNotConfiguredError(
            "MAX not_configured: max_chat_id выходит за пределы int64"
        )
    try:
        token = (decrypt_secret(company.max_access_token) or "").strip()
    except (RuntimeError, ValueError) as exc:
        raise PublicationNotConfiguredError(
            "MAX not_configured: не удалось расшифровать max_access_token; проверьте ENCRYPTION_KEY"
        ) from exc
    if not token:
        raise PublicationNotConfiguredError(
            "MAX not_configured: max_access_token пуст"
        )

    if client is not None:
        return await _publish_max_with_client(client, token, chat_id, publication)
    timeout = httpx.Timeout(40.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as owned_client:
        return await _publish_max_with_client(owned_client, token, chat_id, publication)


async def publish_to_platform(
    company: Company, publication: Publication
) -> tuple[str, str | None]:
    if publication.platform == "vk":
        return await publish_vk(company, publication)
    if publication.platform == "telegram":
        return await publish_telegram(company, publication)
    if publication.platform == "max":
        return await publish_max(company, publication)
    raise RuntimeError(f"Неизвестная площадка: {publication.platform}")


async def fetch_vk_comments(company: Company, publication: Publication) -> list[dict]:
    """Read comments through the backend without exposing the VK token to workers."""
    token = decrypt_secret(company.vk_access_token)
    if not token or not company.vk_group_id or not publication.published_post_id:
        raise RuntimeError("Для чтения комментариев не настроены VK token, group или post ID")
    group_id = str(company.vk_group_id).lstrip("-")
    post_id = int(str(publication.published_post_id).rsplit("_", 1)[-1])
    timeout = httpx.Timeout(30.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await _vk_call(
            client,
            "wall.getComments",
            {
                "owner_id": f"-{group_id}",
                "post_id": post_id,
                "count": 50,
                "sort": "desc",
                "access_token": token,
                "v": settings.vk_api_version,
            },
        )
    return [
        {
            "external_comment_id": str(item["id"]),
            "author_id": str(item.get("from_id", "")),
            "text": str(item.get("text", "")),
        }
        for item in (response or {}).get("items", [])
    ]


async def send_telegram_notification(company: Company, text: str) -> None:
    """Send an operator notification while keeping the bot token on the backend."""
    token = settings.telegram_bot_token
    chat_id = company.approval_chat_id or company.telegram_chat_id
    if not token or not chat_id:
        raise RuntimeError("Не настроены Telegram bot token или чат уведомлений")
    chunks = [text[i : i + 3900] for i in range(0, len(text), 3900)] or [" "]
    timeout = httpx.Timeout(20.0, connect=10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        for chunk in chunks:
            response = await client.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": chunk},
            )
            response.raise_for_status()
            data = response.json()
            if not data.get("ok"):
                raise RuntimeError(
                    f"Telegram API error: {data.get('description', 'unknown')}"
                )
