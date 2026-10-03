import asyncio

import structlog
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, OperationalError
from sqlalchemy.ext.asyncio import (
    AsyncConnection,
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings

logger = structlog.get_logger(__name__)

# Postgres SQLSTATEs we treat specially (see app/services/reservations.py, app/main.py).
DEADLOCK_DETECTED = "40P01"
SERIALIZATION_FAILURE = "40001"
LOCK_NOT_AVAILABLE = "55P03"
RETRYABLE_SQLSTATES = {DEADLOCK_DETECTED, SERIALIZATION_FAILURE}


def async_database_url(url: str) -> str:
    """Render/compose give `postgresql://`; SQLAlchemy needs the driver named."""
    for prefix in ("postgresql://", "postgres://"):
        if url.startswith(prefix):
            return "postgresql+asyncpg://" + url[len(prefix):]
    return url


def sqlstate(exc: BaseException) -> str | None:
    """SQLSTATE of a database error, unwrapping SQLAlchemy's DBAPIError."""
    orig = exc.orig if isinstance(exc, DBAPIError) else exc
    return getattr(orig, "sqlstate", None) or getattr(orig, "pgcode", None)


class Database:
    """Owns the SQLAlchemy async engine (and its connection pool).

    Everything shares one pool:
      * `session()`      - ORM session, transactional (CRUD, cancel).
      * `read_session()` - ORM session in AUTOCOMMIT for single-statement reads:
        no BEGIN/ROLLBACK round trips.
      * `connect()` / `read_connect()` - Core connections, the same two modes,
        for the reserve hot path.

    Except the probes: `/readyz` and the `/metrics` seat gauges use their own
    tiny AUTOCOMMIT pool (`probe_session()`). Otherwise a burst that saturates
    the request pool would make `/readyz` time out, and the platform's health
    check would pull (or restart) a perfectly healthy instance mid-burst.

    Connecting and migrating happen in the background so the app starts (and
    /healthz answers) even when Postgres is down; /readyz reports 503 until
    the schema is current.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self.engine: AsyncEngine = create_async_engine(
            async_database_url(settings.database_url),
            pool_size=settings.db_pool_max_size,
            max_overflow=0,
            # How long a request may queue for a free connection.
            pool_timeout=settings.db_pool_acquire_timeout,
            echo=settings.db_echo,
            # AUTOCOMMIT reads have no transaction to roll back on pool return.
            skip_autocommit_rollback=True,
            connect_args={
                "server_settings": {
                    # Nothing on the hot path should run this long; fail the
                    # statement rather than let requests pile up behind it.
                    "statement_timeout": str(settings.db_statement_timeout_ms),
                    "lock_timeout": str(settings.db_lock_timeout_ms),
                    "application_name": settings.app_name,
                }
            },
        )
        self._autocommit_engine = self.engine.execution_options(isolation_level="AUTOCOMMIT")
        self._probe_engine: AsyncEngine = create_async_engine(
            async_database_url(settings.database_url),
            isolation_level="AUTOCOMMIT",
            # One for /readyz, one for a /metrics scrape, so neither waits on the other.
            pool_size=2,
            max_overflow=0,
            pool_timeout=settings.db_ready_timeout,
            # A connection left broken by a DB restart is replaced, not reported as "not ready".
            pool_pre_ping=True,
            connect_args={
                "server_settings": {
                    "statement_timeout": str(settings.db_probe_statement_timeout_ms),
                    "application_name": f"{settings.app_name}-probe",
                }
            },
        )
        self._probe_session = async_sessionmaker(self._probe_engine, expire_on_commit=False)
        self._session = async_sessionmaker(self.engine, expire_on_commit=False)
        self._read_session = async_sessionmaker(self._autocommit_engine, expire_on_commit=False)
        self._ready = False
        self._connect_task: asyncio.Task | None = None

    @property
    def ready(self) -> bool:
        return self._ready

    def start(self) -> None:
        self._connect_task = asyncio.create_task(self._connect_and_migrate())

    async def wait_ready(self, timeout: float) -> bool:
        """Block until connected and migrated (used by tests and scripts)."""
        if self._connect_task is None:
            return False
        try:
            await asyncio.wait_for(asyncio.shield(self._connect_task), timeout)
        except TimeoutError:
            return False
        return self._ready

    async def _connect_and_migrate(self) -> None:
        # Imported lazily: alembic's env.py imports this module.
        from app.db.migrations import current_revision, upgrade_to_head

        delay = 0.5
        while True:
            try:
                async with self.engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))
                # Alembic's env.py runs its own event loop, so give it a thread.
                await asyncio.to_thread(upgrade_to_head, self._settings.database_url)
                self._ready = True
                logger.info("database_ready", schema_revision=current_revision(self._settings.database_url))
                return
            except (OSError, DBAPIError, OperationalError) as exc:
                logger.warning("database_unavailable", retry_in_s=delay, error=repr(exc))
                await asyncio.sleep(delay)
                delay = min(delay * 2, 5.0)

    async def close(self) -> None:
        if self._connect_task and not self._connect_task.done():
            self._connect_task.cancel()
        await self.engine.dispose()
        await self._probe_engine.dispose()

    def session(self) -> AsyncSession:
        if not self._ready:
            raise DatabaseUnavailable("database_not_ready")
        return self._session()

    def read_session(self) -> AsyncSession:
        if not self._ready:
            raise DatabaseUnavailable("database_not_ready")
        return self._read_session()

    def probe_session(self) -> AsyncSession:
        """AUTOCOMMIT session on the probe pool (readiness, metrics gauges)."""
        if not self._ready:
            raise DatabaseUnavailable("database_not_ready")
        return self._probe_session()

    # Core connections for the reserve hot path: same pool and same tables as
    # the ORM, minus the Session layer (identity map, unit of work), whose
    # per-request CPU cost showed up as ~3x lower burst throughput.
    def connect(self) -> AsyncConnection:
        if not self._ready:
            raise DatabaseUnavailable("database_not_ready")
        return self.engine.connect()

    def read_connect(self) -> AsyncConnection:
        if not self._ready:
            raise DatabaseUnavailable("database_not_ready")
        return self._autocommit_engine.connect()

    def pool_status(self) -> dict[str, int]:
        pool = self.engine.pool
        return {"size": pool.size(), "checked_out": pool.checkedout()}

    async def ping(self) -> tuple[bool, str | None]:
        """Run `SELECT 1` within the readiness budget, on the probe pool so a
        busy request pool doesn't read as "not ready". Never raises."""
        if not self._ready:
            return False, "not_initialized"
        try:
            async with asyncio.timeout(self._settings.db_ready_timeout):
                async with self.probe_session() as session:
                    await session.execute(text("SELECT 1"))
            return True, None
        except TimeoutError:
            return False, "timeout"
        except Exception as exc:  # any failure means "not ready"
            return False, type(exc.orig if isinstance(exc, DBAPIError) else exc).__name__


class DatabaseUnavailable(Exception):
    pass
