from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/marketer"
    telegram_bot_token: str = ""
    vk_api_version: str = "5.199"
    encryption_key: str = ""  # тот же ключ, что у backend (расшифровка токенов компаний)

    # Куда кросспостить, если у компании нет своего TG-канала (демо-режим)
    telegram_target_chat: str = ""

    ai_agent_url: str = ""  # http://ai-agent:8001/generate — для черновиков ответов

    scheduler_interval_sec: int = 15
    monitor_interval_sec: int = 60
    max_attempts: int = 3


settings = Settings()
