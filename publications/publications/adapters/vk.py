"""VK-адаптер: токен сообщества, HTTPX, без сторонних библиотек.

Согласовано с backend (app/vk.py Р1): тот же порядок вызовов и версия API.
"""
import httpx

from publications.config import settings

VK_API = "https://api.vk.com/method"


class VkError(RuntimeError):
    def __init__(self, payload: dict):
        self.code = payload.get("error_code")
        super().__init__(f"VK error {self.code}: {payload.get('error_msg')}")


class VkAdapter:
    def __init__(self, token: str):
        self.token = token
        self.v = settings.vk_api_version

    async def _call(self, client: httpx.AsyncClient, method: str, **params) -> dict:
        params.setdefault("access_token", self.token)
        params.setdefault("v", self.v)
        resp = await client.post(f"{VK_API}/{method}", data=params)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise VkError(data["error"])
        return data["response"]

    async def publish(self, *, target_id: str, text: str, image_url: str | None) -> dict:
        """target_id = vk_group_id компании. Пост от имени сообщества."""
        group_id = abs(int(target_id))
        owner_id = -group_id
        attachments = ""
        async with httpx.AsyncClient(timeout=60) as client:
            if image_url:
                img = await client.get(image_url)
                img.raise_for_status()
                upload = await self._call(client, "photos.getWallUploadServer", group_id=group_id)
                uploaded = await client.post(
                    upload["upload_url"], files={"photo": ("image.jpg", img.content, "image/jpeg")}
                )
                uploaded.raise_for_status()
                photo_data = uploaded.json()
                saved = await self._call(
                    client, "photos.saveWallPhoto", group_id=group_id,
                    photo=photo_data["photo"], server=photo_data["server"], hash=photo_data["hash"],
                )
                photo = saved[0]
                attachments = f"photo{photo['owner_id']}_{photo['id']}"
            response = await self._call(
                client, "wall.post", owner_id=owner_id, from_group=1,
                message=text, attachments=attachments,
            )
        post_id = str(response["post_id"])
        return {"post_id": post_id, "url": f"https://vk.com/wall{owner_id}_{post_id}"}

    async def fetch_comments(self, *, target_id: str, post_id: str) -> list[dict]:
        group_id = abs(int(target_id))
        owner_id = -group_id
        async with httpx.AsyncClient(timeout=30) as client:
            response = await self._call(
                client, "wall.getComments", owner_id=owner_id, post_id=int(post_id),
                count=50, sort="desc",
            )
        return [
            {
                "external_comment_id": str(item["id"]),
                "author_id": str(item.get("from_id", "")),
                "text": item.get("text", ""),
            }
            for item in response.get("items", [])
        ]
