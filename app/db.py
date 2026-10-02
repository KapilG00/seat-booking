import asyncio
import logging

import asyncpg

from app.config import Settings

logger = logging.getLogger(__name__)


class Database:
    """Owns the asyncpg pool.

    The pool is created in the background so the app starts (and /healthz
    answers) even when Postgres is down; /readyz reports 503 until it's up.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._pool: asyncpg.Pool | None = None
        self._connect_task: asyncio.Task | None = None

    @property
    def pool(self) -> asyncpg.Pool | None:
        return self._pool

    def start(self) -> None:
        self._connect_task = asyncio.create_task(self._connect_with_retry())

    async def _connect_with_retry(self) -> None:
        delay = 0.5
        while True:
            try:
                self._pool = await asyncpg.create_pool(
                    dsn=self._settings.database_url,
                    min_size=self._settings.db_pool_min_size,
                    max_size=self._settings.db_pool_max_size,
                )
                logger.info("database pool ready")
                return
            except (OSError, asyncpg.PostgresError) as exc:
                logger.warning("database unavailable, retrying in %.1fs: %r", delay, exc)
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def close(self) -> None:
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    async def ping(self) -> tuple[bool, str | None]:
        """Run `SELECT 1` within the readiness budget. Never raises."""
        if self._pool is None:
            return False, "pool_not_initialized"
        try:
            async with asyncio.timeout(self._settings.db_ready_timeout):
                async with self._pool.acquire() as conn:
                    await conn.fetchval("SELECT 1")
            return True, None
        except TimeoutError:
            return False, "timeout"
        except Exception as exc:  # any failure means "not ready"
            return False, type(exc).__name__
