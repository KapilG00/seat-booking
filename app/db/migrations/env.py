"""Alembic environment: runs migrations over the app's async engine.

Used both by the CLI (`uv run alembic ...`) and at app startup
(app.db.migrations.upgrade_to_head). A transaction-scoped advisory lock means
that when several instances boot at once, exactly one migrates and the rest
wait, then find nothing to do.
"""

import asyncio

from alembic import context
from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import get_settings
from app.db.database import async_database_url
from app.models import Base  # importing the package registers every model

MIGRATION_LOCK_ID = 727_001

config = context.config
target_metadata = Base.metadata


def _url() -> str:
    # Startup passes the URL explicitly; the CLI falls back to app settings.
    return async_database_url(config.attributes.get("database_url") or get_settings().database_url)


def run_migrations_offline() -> None:
    """`alembic upgrade head --sql`: print the SQL instead of running it."""
    context.configure(url=_url(), target_metadata=target_metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        connection.execute(text("SELECT pg_advisory_xact_lock(:id)"), {"id": MIGRATION_LOCK_ID})
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(_run)
    finally:
        await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
