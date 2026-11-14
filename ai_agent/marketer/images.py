"""Генерация изображений для поста. IMAGE_PROVIDER = offline | openai."""
from __future__ import annotations

import asyncio
import base64
import io
import random
import textwrap
from pathlib import Path

import httpx

from .config import Settings


def _font(size: int):
    from PIL import ImageFont
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                 "arialbd.ttf", "C:/Windows/Fonts/arialbd.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def offline_illustration(title: str, out: Path, brand_color: str = "#1f5f8b", seed: int = 0) -> Path:
    """Рисует абстрактный стеклянный фасад + заголовок + пометку «AI-иллюстрация». Без сети."""
    from PIL import Image, ImageDraw
    W, H = 1280, 720
    rnd = random.Random(seed or hash(title))
    img = Image.new("RGB", (W, H), "#dfe9f2")
    d = ImageDraw.Draw(img)
    for y in range(H):  # небо
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(170 + 60 * t), int(205 + 30 * t), int(235 + 15 * t)))
    base = tuple(int(brand_color[i:i + 2], 16) for i in (1, 3, 5))
    x = 60
    while x < W - 120:  # здания со стеклянными панелями
        bw, bh = rnd.randint(150, 260), rnd.randint(300, 600)
        top = H - bh
        d.rectangle([x, top, x + bw, H], fill=tuple(max(0, c - 30) for c in base))
        for gx in range(x + 8, x + bw - 30, 28):
            for gy in range(top + 8, H - 36, 34):
                k = rnd.randint(-25, 45)
                d.rectangle([gx, gy, gx + 22, gy + 28],
                            fill=(min(255, 120 + k), min(255, 170 + k), min(255, 210 + k)))
        x += bw + rnd.randint(10, 40)
    d.rectangle([0, 0, W, 150], fill=(255, 255, 255))
    d.text((40, 28), "\n".join(textwrap.wrap(title, 44)[:2]), font=_font(36), fill=base)
    d.text((W - 230, H - 40), "AI-иллюстрация", font=_font(22), fill=(255, 255, 255))
    out.parent.mkdir(parents=True, exist_ok=True)
    img.save(out)
    return out


async def openai_image(prompt: str, out: Path, s: Settings) -> Path:
    body = {"model": s.image_model, "prompt": prompt, "size": s.image_size, "n": 1}
    async with httpx.AsyncClient(timeout=180) as client:
        r = await client.post(s.image_base_url.rstrip("/") + "/images/generations", json=body,
                              headers={"Authorization": f"Bearer {s.image_api_key}"})
        r.raise_for_status()
        item = r.json()["data"][0]
        if item.get("b64_json"):
            out.write_bytes(base64.b64decode(item["b64_json"]))
        else:
            out.write_bytes((await client.get(item["url"])).content)
    return out


async def kandinsky_image(prompt: str, out: Path, s: Settings, width: int = 1024, height: int = 576,
                          attempts: int = 30, delay: float = 3.0) -> Path:
    """Kandinsky через FusionBrain API: получить pipeline -> запустить -> опрашивать статус -> base64."""
    import json as _json
    base = s.kandinsky_base_url.rstrip("/") + "/"
    headers = {"X-Key": f"Key {s.kandinsky_api_key}", "X-Secret": f"Secret {s.kandinsky_secret_key}"}
    async with httpx.AsyncClient(timeout=60, headers=headers) as client:
        r = await client.get(base + "key/api/v1/pipelines")
        r.raise_for_status()
        pipeline_id = r.json()[0]["id"]
        params = {"type": "GENERATE", "style": "DEFAULT", "width": width, "height": height, "numImages": 1,
                  "negativePromptDecoder": "текст, надписи, логотип, водяной знак",
                  "generateParams": {"query": prompt}}
        r = await client.post(base + "key/api/v1/pipeline/run",
                              files={"pipeline_id": (None, pipeline_id),
                                     "params": (None, _json.dumps(params), "application/json")})
        r.raise_for_status()
        uuid_ = r.json()["uuid"]
        for _ in range(attempts):
            r = await client.get(base + f"key/api/v1/pipeline/status/{uuid_}")
            r.raise_for_status()
            data = r.json()
            if data["status"] == "DONE":
                if data.get("result", {}).get("censored"):
                    raise ValueError("Kandinsky: изображение отклонено модерацией")
                out.write_bytes(base64.b64decode(data["result"]["files"][0]))
                return out
            if data["status"] == "FAIL":
                raise ValueError(f"Kandinsky: {data.get('errorDescription', 'генерация не удалась')}")
            await asyncio.sleep(delay)
    raise TimeoutError("Kandinsky: генерация не успела за отведённое время")


async def pollinations_image(prompt: str, out: Path, s: Settings, width: int = 1024, height: int = 576) -> Path:
    """Pollinations: GET /prompt/{текст} -> готовая картинка (JPEG/PNG) в ответе."""
    from urllib.parse import quote
    url = f"{s.pollinations_base_url.rstrip('/')}/prompt/{quote(prompt[:900])}"
    import random
    # фиксированный seed: по той же ссылке Pollinations отдаёт ту же картинку (а не новую при каждом открытии)
    params = {"width": width, "height": height, "model": s.pollinations_model, "nologo": "true", "safe": "true",
              "seed": random.randint(1, 2_000_000_000)}
    headers = {"Authorization": f"Bearer {s.pollinations_api_key}"} if s.pollinations_api_key else {}
    # укладываемся в таймаут бэкенда (90 с на весь ответ): не дождались — пост уходит без картинки
    async with httpx.AsyncClient(timeout=s.pollinations_timeout, follow_redirects=True) as client:
        for attempt in range(2):  # без ключа ≈1 запрос в 15 с: при 429 ждём и повторяем один раз
            r = await client.get(url, params=params, headers=headers)
            if r.status_code != 429 or attempt == 1:
                break
            await asyncio.sleep(min(s.pollinations_retry_wait, 12))
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image/"):
            raise ValueError(f"Pollinations вернул не картинку: {r.headers.get('content-type')}")
    from PIL import Image
    Image.open(io.BytesIO(r.content)).convert("RGB").save(out, "PNG")  # приводим к PNG, как у остальных
    # публичная HTTPS-ссылка на ту же картинку (без ключа): её бэкенд передаёт в Telegram/VK
    out.with_suffix(".url").write_text(str(httpx.URL(url, params=params)), encoding="utf-8")
    return out


def public_image_url(path: Path, rel: str, s: Settings) -> str | None:
    """Ссылка, которую можно отдать во внешний мир (Telegram/VK качают картинку сами).
    1) AI_PUBLIC_MEDIA_BASE_URL (https), если агент опубликован наружу; 2) ссылка Pollinations; иначе None."""
    base = (s.public_media_base_url or "").rstrip("/")
    if base.startswith("https://"):
        return f"{base}/{rel}"
    side = Path(path).with_suffix(".url")
    if side.exists():
        url = side.read_text(encoding="utf-8").strip()
        if url.startswith("https://"):
            return url
    return None


async def generate_image(prompt: str, title: str, out: Path, s: Settings,
                         brand_color: str = "#1f5f8b") -> tuple[Path, str]:
    """Возвращает (путь, движок). При сбое внешнего генератора — офлайн-иллюстрация, а не падение."""
    out.parent.mkdir(parents=True, exist_ok=True)
    if s.image_provider == "pollinations":
        try:
            return await pollinations_image(prompt, out, s), f"pollinations ({s.pollinations_model})"
        except (httpx.HTTPError, ValueError, OSError) as e:
            path = await asyncio.to_thread(offline_illustration, title, out, brand_color)
            return path, f"offline (Pillow) — генератор недоступен: {type(e).__name__}"
    if s.image_provider == "kandinsky" and s.kandinsky_api_key and s.kandinsky_secret_key:
        try:
            return await kandinsky_image(prompt, out, s), "kandinsky (FusionBrain)"
        except (httpx.HTTPError, KeyError, IndexError, ValueError, TimeoutError) as e:
            path = await asyncio.to_thread(offline_illustration, title, out, brand_color)
            return path, f"offline (Pillow) — генератор недоступен: {type(e).__name__}"
    if s.image_provider == "openai" and s.image_api_key:
        try:
            return await openai_image(prompt, out, s), f"{s.image_model} @ {s.image_base_url}"
        except (httpx.HTTPError, KeyError, ValueError) as e:
            # не падаем, но и не скрываем: движок картинки честно помечается как запасной
            path = await asyncio.to_thread(offline_illustration, title, out, brand_color)
            return path, f"offline (Pillow) — генератор недоступен: {type(e).__name__}"
    path = await asyncio.to_thread(offline_illustration, title, out, brand_color)
    return path, "offline (Pillow)"
