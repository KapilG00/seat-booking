from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "seat-booking"
    environment: str = "local"
    log_level: str = "INFO"

    # Default matches the docker-compose `db` service exposed on localhost.
    database_url: str = "postgresql://app:app@localhost:5432/seats"
    # Kept small: free-tier Postgres caps total connections.
    db_pool_min_size: int = 2
    db_pool_max_size: int = 10
    # How long a request may queue for a free connection before giving up.
    db_pool_acquire_timeout: float = 20.0
    # Readiness probe budget for `SELECT 1`.
    db_ready_timeout: float = 1.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
