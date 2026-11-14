"""Настройки ядра AI-маркетолога. Всё берётся из переменных окружения (секреты не в коде)."""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

PROMPT_VERSION = "v3"
PROMPTS_DIR = Path(__file__).parent / "prompts"


def _load_dotenv(path: Path) -> None:
    """Минимальный .env-загрузчик, чтобы не тянуть зависимость python-dotenv."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        value = value.split(" #", 1)[0].split("\t#", 1)[0].strip()  # комментарий в конце строки
        os.environ.setdefault(key.strip(), value.strip('"').strip("'"))


@dataclass
class Settings:
    # общая БД с бэкендом (Р1); для тестов можно sqlite+aiosqlite:///...
    database_url: str = field(default_factory=lambda: os.getenv(
        "DATABASE_URL", "postgresql+asyncpg://postgres:postgres@db:5432/cyber102"))
    # картинки черновиков: файлы в томе + URL, по которому их заберёт модуль публикаций
    media_dir: Path = field(default_factory=lambda: Path(os.getenv("AI_MEDIA_DIR", "media")))
    media_base_url: str = field(default_factory=lambda: os.getenv("AI_MEDIA_BASE_URL", "http://ai-agent:8000/media"))
    # публичный HTTPS-адрес папки /media, если агент доступен из интернета (иначе картинка уходит ссылкой Pollinations)
    public_media_base_url: str = field(default_factory=lambda: os.getenv("AI_PUBLIC_MEDIA_BASE_URL", ""))
    # ключ для внутренних запросов бэкенда к агенту (заголовок X-Internal-Key); пусто = без проверки
    api_key: str = field(default_factory=lambda: os.getenv("AI_AGENT_API_KEY", ""))
    # offline | openai | codex
    llm_provider: str = field(default_factory=lambda: os.getenv("LLM_PROVIDER", "offline"))
    openai_api_key: str = field(default_factory=lambda: os.getenv("OPENAI_API_KEY", ""))
    openai_base_url: str = field(default_factory=lambda: os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
    openai_model: str = field(default_factory=lambda: os.getenv("OPENAI_MODEL", "gpt-4o-mini"))
    openai_auth_scheme: str = field(default_factory=lambda: os.getenv("OPENAI_AUTH_SCHEME", ""))
    llm_retry_wait: float = field(default_factory=lambda: float(os.getenv("LLM_RETRY_WAIT", "16")))
    codex_bin: str = field(default_factory=lambda: os.getenv("CODEX_BIN", "codex"))
    codex_model: str = field(default_factory=lambda: os.getenv("CODEX_MODEL", ""))
    # offline | openai
    image_provider: str = field(default_factory=lambda: os.getenv("IMAGE_PROVIDER", "offline"))
    image_model: str = field(default_factory=lambda: os.getenv("IMAGE_MODEL", "gpt-image-1"))
    # у картинок может быть свой провайдер (например, текст — GitHub Models, картинки — ProxyAPI);
    # пусто = те же ключ и адрес, что у текста
    image_api_key: str = field(default_factory=lambda: os.getenv("IMAGE_API_KEY", "") or os.getenv("OPENAI_API_KEY", ""))
    image_base_url: str = field(default_factory=lambda: os.getenv("IMAGE_BASE_URL", "") or os.getenv(
        "OPENAI_BASE_URL", "https://api.openai.com/v1"))
    image_size: str = field(default_factory=lambda: os.getenv("IMAGE_SIZE", "1536x1024"))
    # Kandinsky (Сбер, FusionBrain): бесплатный API, ключи — в личном кабинете fusionbrain.ai, раздел API
    kandinsky_api_key: str = field(default_factory=lambda: os.getenv("KANDINSKY_API_KEY", ""))
    kandinsky_secret_key: str = field(default_factory=lambda: os.getenv("KANDINSKY_SECRET_KEY", ""))
    kandinsky_base_url: str = field(default_factory=lambda: os.getenv("KANDINSKY_BASE_URL", "https://api-key.fusionbrain.ai/"))
    # Pollinations: бесплатно и без ключа (≈1 запрос в 15 с); бесплатный ключ с enter.pollinations.ai поднимает лимит
    pollinations_timeout: float = field(default_factory=lambda: float(os.getenv("POLLINATIONS_TIMEOUT", "25")))
    pollinations_base_url: str = field(default_factory=lambda: os.getenv("POLLINATIONS_BASE_URL", "https://image.pollinations.ai"))
    pollinations_api_key: str = field(default_factory=lambda: os.getenv("POLLINATIONS_API_KEY", ""))
    pollinations_model: str = field(default_factory=lambda: os.getenv("POLLINATIONS_MODEL", "flux"))
    pollinations_retry_wait: float = field(default_factory=lambda: float(os.getenv("POLLINATIONS_RETRY_WAIT", "16")))
    history_limit: int = field(default_factory=lambda: int(os.getenv("HISTORY_LIMIT", "12")))
    top_k: int = field(default_factory=lambda: int(os.getenv("KB_TOP_K", "5")))

    @classmethod
    def load(cls, env_file: str | os.PathLike = ".env") -> "Settings":
        _load_dotenv(Path(env_file))
        return cls()


# Правила площадок: длина и особенности вёрстки
PLATFORMS = {
    "telegram": {"max_len": 1000, "rules": "подпись к фото в Telegram — до 1024 символов, абзацы короткие, 1–3 эмодзи допустимы"},
    "vk": {"max_len": 2000, "rules": "ВКонтакте: можно длиннее, абзацы, 3–5 хэштегов в конце"},
    "max": {"max_len": 1000, "rules": "MAX: короткий текст, без сложной разметки"},
}
