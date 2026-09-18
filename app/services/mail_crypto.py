"""Шифрование IMAP-паролей (Fernet от SECRET_KEY)."""

from __future__ import annotations

import base64
import hashlib
import os

from cryptography.fernet import Fernet, InvalidToken

from app.config_env import get_env


def _fernet() -> Fernet:
    secret = get_env("SECRET_KEY") or os.getenv("SECRET_KEY") or "your-super-secret-key-change-this-1234567890"
    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plain: str) -> str:
    return _fernet().encrypt(plain.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError, TypeError) as exc:
        raise ValueError("Не удалось расшифровать пароль почты. Сохраните настройки заново.") from exc
