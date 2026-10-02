from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

DEV_JWT_SECRET = "dev-only-jwt-secret-change-me-in-production"
DEV_ADMIN_TOKEN = "dev-admin-token"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "seat-booking"
    environment: str = "local"
    log_level: str = "INFO"
    # One JSON line per request; turn off if logging itself becomes the bottleneck.
    access_log: bool = True

    # Default matches the docker-compose `db` service exposed on localhost.
    database_url: str = "postgresql://app:app@localhost:5432/seats"
    # Kept small: free-tier Postgres caps total connections.
    db_pool_max_size: int = 10
    # How long a request may queue for a free connection before giving up.
    db_pool_acquire_timeout: float = 45.0
    # Readiness probe budget for `SELECT 1`.
    db_ready_timeout: float = 1.0
    db_statement_timeout_ms: int = 15_000
    db_lock_timeout_ms: int = 10_000
    # Log every SQL statement (debugging only; very noisy under load).
    db_echo: bool = False

    # Auth: HS256 JWTs for users, a static bearer token for admin endpoints.
    jwt_secret: str = DEV_JWT_SECRET
    jwt_ttl_seconds: int = 24 * 3600
    admin_token: str = DEV_ADMIN_TOKEN

    # In-memory fast decline for seats already known to be taken
    # (app/services/sold_seat_cache.py). Never decides a sale.
    sold_seat_cache_enabled: bool = True
    sold_seat_cache_ttl_seconds: float = 30.0

    default_per_user_limit: int = 4
    max_seats_per_show: int = 20_000

    @model_validator(mode="after")
    def _no_dev_secrets_in_production(self) -> "Settings":
        if self.environment == "production" and (
            self.jwt_secret == DEV_JWT_SECRET or self.admin_token == DEV_ADMIN_TOKEN
        ):
            raise ValueError("JWT_SECRET and ADMIN_TOKEN must be set in production")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
