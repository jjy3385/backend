from cryptography.fernet import Fernet
from app.config.env import settings

class TokenCipher:
    def __init__(self, key: str | None = None):
        key_bytes = (key or settings.ENCRYPTION_KEY).encode()
        self._fernet = Fernet(key_bytes)

    def encrypt(self, value: str) -> str:
        return self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        return self._fernet.decrypt(value.encode()).decode()

cipher = TokenCipher()
