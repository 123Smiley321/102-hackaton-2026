"""Telegram-адаптер: Bot API через HTTPX (без aiogram — не нужен polling)."""
import httpx

from publications.config import settings

TG_API = f"https://api.telegram.org/bot{settings.telegram_bot_token}"


class TgAdapter:
    async def publish(self, *, target_id: str, text: str, image_url: str | None) -> dict:
        """target_id = @channelname или chat_id канала. Бот должен быть админом."""
        async with httpx.AsyncClient(timeout=30) as client:
            if image_url:
                resp = await client.post(
                    f"{TG_API}/sendPhoto",
                    json={"chat_id": target_id, "photo": image_url,
                          "caption": text[:1024], "parse_mode": "Markdown"},
                )
            else:
                resp = await client.post(
                    f"{TG_API}/sendMessage",
                    json={"chat_id": target_id, "text": text[:4096],
                          "parse_mode": "Markdown"},
                )
            resp.raise_for_status()
            data = resp.json()
            if not data.get("ok"):
                raise RuntimeError(f"Telegram error: {data.get('description')}")
            msg = data["result"]
            chat = msg["chat"]
            url = f"https://t.me/{chat.get('username')}/{msg['message_id']}" if chat.get("username") \
                else f"tg://message/{msg['message_id']}"
            return {"post_id": str(msg["message_id"]), "url": url}

    async def fetch_comments(self, *, target_id: str, post_id: str) -> list[dict]:
        # Комментарии TG-канала живут в привязанной группе — в MVP мониторим только VK.
        return []

    async def notify(self, *, chat_id: str, text: str) -> None:
        """Уведомление клиенту/владельцу (результат публикации, ошибка, черновик ответа)."""
        if not settings.telegram_bot_token:
            return
        async with httpx.AsyncClient(timeout=15) as client:
            await client.post(f"{TG_API}/sendMessage",
                              json={"chat_id": chat_id, "text": text[:4096]})
