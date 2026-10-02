"""Token handling, free of FastAPI so it can be unit-tested and reused.

Identity comes only from a verified token's `sub` claim, never from the
request body. The HTTP dependencies that use this live in app/api/deps.py.
"""

import secrets
import time

import jwt

from app.core.config import Settings
from app.core.errors import Unauthorized

ALGORITHM = "HS256"


def issue_token(user_id: str, settings: Settings) -> tuple[str, int]:
    """Return (signed JWT, lifetime in seconds) for `user_id`."""
    now = int(time.time())
    claims = {"sub": user_id, "iat": now, "exp": now + settings.jwt_ttl_seconds}
    return jwt.encode(claims, settings.jwt_secret, algorithm=ALGORITHM), settings.jwt_ttl_seconds


def user_id_from_token(token: str, settings: Settings) -> str:
    try:
        claims = jwt.decode(
            token,
            settings.jwt_secret,
            algorithms=[ALGORITHM],
            options={"require": ["sub", "exp"]},
        )
    except jwt.PyJWTError:
        raise Unauthorized("invalid or expired token") from None
    return claims["sub"]


def is_admin_token(token: str, settings: Settings) -> bool:
    # Constant-time comparison so the token can't be guessed byte by byte.
    return secrets.compare_digest(token.encode(), settings.admin_token.encode())
