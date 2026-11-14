from cryptography.fernet import Fernet, InvalidToken
from app.config import settings

def encrypt_secret(value: str | None) -> str | None:
    if not value:
        return value
    if not settings.encryption_key:
        return value  # MVP only; configure ENCRYPTION_KEY before real client credentials
    return Fernet(settings.encryption_key.encode()).encrypt(value.encode()).decode()

def decrypt_secret(value: str | None) -> str | None:
    if not value:
        return value
    if not settings.encryption_key:
        return value
    try:
        return Fernet(settings.encryption_key.encode()).decrypt(value.encode()).decode()
    except InvalidToken:
        raise ValueError("Не удалось расшифровать токен: проверьте ENCRYPTION_KEY")
