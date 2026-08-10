"""Password hashing, session tokens, CSRF — PRINCIPAL-OWNED.

Rules (docs/planning/07-security-model.md, ratified):
- argon2id for passwords
- session tokens are 256-bit random; the database stores only sha256(token)
- CSRF: double-submit token PLUS an Origin/Referer allowlist check
- all secret comparisons are constant-time
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2id defaults (time_cost=3, memory_cost=64MiB)

SESSION_TOKEN_BYTES = 32
CSRF_TOKEN_BYTES = 32


def hash_password(password: str) -> str:
    return _hasher.hash(password)


def verify_password(password_hash: str, candidate: str) -> bool:
    try:
        return _hasher.verify(password_hash, candidate)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        # a corrupt stored hash is a failed login, never a 500
        return False


def password_needs_rehash(password_hash: str) -> bool:
    return _hasher.check_needs_rehash(password_hash)


def new_session_token() -> str:
    """Opaque token given to the browser. Never stored server-side."""
    return secrets.token_urlsafe(SESSION_TOKEN_BYTES)


def hash_session_token(token: str) -> str:
    """What the database stores: sha256 hex of the opaque token."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(CSRF_TOKEN_BYTES)


def constant_time_equals(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode("utf-8"), b.encode("utf-8"))


def origin_allowed(origin_or_referer: str | None, allowed_origins: list[str]) -> bool:
    """Origin/Referer allowlist check for state-changing requests.

    A missing header fails closed: browsers send Origin on all cross-origin and
    same-origin non-GET requests; its absence on a mutation is suspicious.
    """
    if not origin_or_referer:
        return False
    value = origin_or_referer.rstrip("/")
    for allowed in allowed_origins:
        allowed = allowed.rstrip("/")
        if value == allowed or value.startswith(allowed + "/"):
            return True
    return False
