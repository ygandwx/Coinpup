"""Password hashing and opaque session credentials; never log these inputs."""

import hashlib
import hmac
import re
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

PASSWORD_HASHER = PasswordHasher(time_cost=3, memory_cost=65536, parallelism=4)
# A real Argon2 computation is also performed before an account is initialized.
DUMMY_PASSWORD_HASH = PASSWORD_HASHER.hash(secrets.token_urlsafe(32))


def normalize_username(username: str) -> str:
    normalized = username.strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{3,64}", normalized):
        raise ValueError("Username must contain 3–64 ASCII letters, digits, dots, - or _")
    return normalized


def hash_password(password: str) -> str:
    if not 12 <= len(password) <= 128:
        raise ValueError("Password must contain 12–128 characters")
    return PASSWORD_HASHER.hash(password)


def verify_password(encoded: str, password: str) -> bool:
    try:
        return PASSWORD_HASHER.verify(encoded, password)
    except (VerificationError, InvalidHashError):
        return False


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def csrf_token_for(token: str) -> str:
    # This reveals neither the cookie nor its stored digest, and needs no shared secret.
    return hashlib.sha256(b"coinpup.csrf.v1:" + token.encode("utf-8")).hexdigest()


def valid_csrf_token(token: str, supplied: str | None) -> bool:
    return bool(
        supplied
        and len(supplied) == 64
        and supplied.isascii()
        and hmac.compare_digest(csrf_token_for(token), supplied)
    )
