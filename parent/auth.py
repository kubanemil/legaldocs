"""Per-tenant API keys. Only a SHA-256 of each secret is stored.

A fast hash is enough: secrets are 256 random bits, so there is nothing to brute-force.
"""

import hashlib
import hmac
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final

from sqlalchemy.orm import Session

from parent.models import ApiKey

PREFIX: Final = "ldk"


@dataclass(frozen=True)
class IssuedKey:
    key_id: str
    token: str
    secret_hash: bytes


def issue() -> IssuedKey:
    key_id = secrets.token_hex(8)
    secret = secrets.token_urlsafe(32)
    return IssuedKey(key_id, f"{PREFIX}_{key_id}_{secret}", _hash(secret))


def authenticate(session: Session, token: str) -> str | None:
    prefix, _, rest = token.partition("_")
    key_id, _, secret = rest.partition("_")
    if prefix != PREFIX or not key_id or not secret:
        return None
    key = session.get(ApiKey, key_id)
    if key is None or key.revoked_at is not None:
        return None
    if not hmac.compare_digest(key.secret_hash, _hash(secret)):
        return None
    return key.tenant_id


def revoke(session: Session, key_id: str) -> bool:
    key = session.get(ApiKey, key_id)
    if key is None or key.revoked_at is not None:
        return False
    key.revoked_at = datetime.now(UTC)
    session.commit()
    return True


def _hash(secret: str) -> bytes:
    return hashlib.sha256(secret.encode()).digest()
