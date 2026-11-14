from pydantic_settings import BaseSettings, SettingsConfigDict

class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")
    app_name: str = "Cyber102 AI Marketer Backend"
    database_url: str = "postgresql+asyncpg://postgres:postgres@db:5432/marketer"
    telegram_bot_token: str = ""
    admin_api_key: str = "change-me"
    ai_agent_url: str = ""  # e.g. http://ai-agent:8000/v1/generate
    ai_agent_api_key: str = ""  # optional X-Internal-Key for Backend/bot → AI-agent
    vk_api_version: str = "5.199"
    encryption_key: str = ""  # required before saving/decrypting company social tokens
    public_base_url: str = "http://localhost:8000"
    publisher_internal_key: str = ""  # shared only with the publication worker

settings = Settings()
