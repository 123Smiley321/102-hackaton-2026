from typing import Protocol


class PublishedResult(dict):
    """{'post_id': str, 'url': str}"""


class PlatformAdapter(Protocol):
    async def publish(self, *, target_id: str, text: str, image_url: str | None) -> PublishedResult: ...
    async def fetch_comments(self, *, target_id: str, post_id: str) -> list[dict]: ...
