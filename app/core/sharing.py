"""Утилиты для шар-ссылок (инкремент 8): токены и пароли.

Без новых зависимостей — пароль хэшируется stdlib PBKDF2-HMAC-SHA256. Формат
строки в БД: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``. Пустой
password_hash → ссылка без пароля.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_PBKDF2_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"


def new_share_token(nbytes: int = 18) -> str:
    """Криптостойкий url-safe токен (по умолчанию ~24 символа)."""
    return secrets.token_urlsafe(max(8, int(nbytes)))


def hash_share_password(password: str) -> str:
    """Хэш пароля для хранения. Пустой пароль → пустая строка (ссылка без пароля)."""
    pw = str(password or "")
    if not pw:
        return ""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_share_password(password: str, stored: str) -> bool:
    """Проверить пароль против сохранённого хэша.

    Пустой ``stored`` → пароль не требуется → True. Непустой ``stored`` с пустым
    ``password`` → False. Сравнение в постоянном времени.
    """
    stored = str(stored or "")
    if not stored:
        return True
    pw = str(password or "")
    if not pw:
        return False
    try:
        algo, iters_s, salt_hex, hash_hex = stored.split("$", 3)
        if algo != _ALGO:
            return False
        iterations = int(iters_s)
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except (ValueError, TypeError):
        return False
    candidate = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(candidate, expected)
