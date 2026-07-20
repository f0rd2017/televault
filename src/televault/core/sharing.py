"""Utilities for share links (increment 8): tokens and passwords.

No new dependencies — passwords are hashed with stdlib PBKDF2-HMAC-SHA256.
DB string format: ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``. An
empty password_hash → a link with no password.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

_PBKDF2_ITERATIONS = 200_000
_ALGO = "pbkdf2_sha256"


def new_share_token(nbytes: int = 18) -> str:
    """A cryptographically strong url-safe token (~24 characters by default)."""
    return secrets.token_urlsafe(max(8, int(nbytes)))


def hash_share_password(password: str) -> str:
    """Hash a password for storage. An empty password → an empty string (link with no password)."""
    pw = str(password or "")
    if not pw:
        return ""
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", pw.encode("utf-8"), salt, _PBKDF2_ITERATIONS)
    return f"{_ALGO}${_PBKDF2_ITERATIONS}${salt.hex()}${digest.hex()}"


def verify_share_password(password: str, stored: str) -> bool:
    """Verify a password against the stored hash.

    An empty ``stored`` → no password required → True. A non-empty ``stored``
    with an empty ``password`` → False. Compared in constant time.
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
