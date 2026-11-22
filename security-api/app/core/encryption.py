from cryptography.fernet import Fernet

from app.core.config import settings


fernet = Fernet(settings.encryption_key.encode())


def encrypt_data(data: str) -> str:
    return fernet.encrypt(data.encode()).decode()


def decrypt_data(encrypted_data: str) -> str:
    return fernet.decrypt(encrypted_data.encode()).decode()