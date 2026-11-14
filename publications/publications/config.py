from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/marketer"
    backend_url: str = "http://backend:8000"
    publisher_internal_key: str = ""

    # Optional: drafts of replies to VK comments. Platform credentials are never configured here.
    ai_agent_url: str = ""

    scheduler_interval_sec: int = 15
    monitor_interval_sec: int = 60
    publication_batch_size: int = 50
    backend_timeout_sec: int = 100


settings = Settings()
