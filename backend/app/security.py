import hashlib
import secrets
from cryptography.fernet import Fernet, InvalidToken
from app.config import settings

def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return value
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY обязателен для сохранения токенов площадок")
    try:
        return Fernet(settings.encryption_key.encode()).encrypt(value.encode()).decode()
    except Exception as exc:
        raise RuntimeError("Некорректный ENCRYPTION_KEY: ожидается ключ Fernet") from exc

def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return value
    if not settings.encryption_key:
        raise RuntimeError("ENCRYPTION_KEY не настроен: невозможно безопасно прочитать токен")
    try:
        return Fernet(settings.encryption_key.encode()).decrypt(value.encode()).decode()
    except InvalidToken as exc:
        raise ValueError("Не удалось расшифровать токен: проверьте ENCRYPTION_KEY") from exc

def hash_api_key(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()

def new_api_key() -> str:
    return "c102_" + secrets.token_urlsafe(36)
