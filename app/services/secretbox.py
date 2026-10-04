"""Encryption for secrets stored in the database (bank PINs), keyed by SECRET_KEY."""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings


class SecretUnavailable(Exception):
    """No SECRET_KEY is configured, or the stored value was encrypted with a different key."""


def _fernet() -> Fernet:
    secret = get_settings().secret_key
    if not secret:
        raise SecretUnavailable("Set SECRET_KEY in .env to store PINs.")
    key = hashlib.sha256(b"proud-ledger/pin/" + secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


def can_store_secrets() -> bool:
    return bool(get_settings().secret_key)


def encrypt(value: str) -> str:
    return _fernet().encrypt(value.encode()).decode()


def decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode()).decode()
    except InvalidToken as exc:
        raise SecretUnavailable("The stored PIN cannot be decrypted (was SECRET_KEY changed?). Please enter it again.") from exc
