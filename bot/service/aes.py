import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from service.config import settings


def get_master_key() -> bytes:
    """Получает мастер-ключ для шифрования из настроек."""
    return bytes.fromhex(settings.master_key)


def encrypt(plaintext: str) -> tuple[bytes, bytes]:
    """Encrypt plaintext with AES-GCM. Returns (ciphertext, nonce)."""
    key = get_master_key()
    aesgcm = AESGCM(key)
    nonce = os.urandom(12)
    ciphertext = aesgcm.encrypt(nonce, plaintext.encode("utf-8"), None)
    return ciphertext, nonce


def decrypt(ciphertext: bytes, nonce: bytes) -> str:
    """Decrypt ciphertext with AES-GCM."""
    key = get_master_key()
    aesgcm = AESGCM(key)
    plaintext = aesgcm.decrypt(nonce, ciphertext, None)
    return plaintext.decode("utf-8")
