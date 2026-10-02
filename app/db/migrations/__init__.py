"""Schema migrations (Alembic). The app applies them at startup."""

from pathlib import Path

from alembic import command
from alembic.config import Config

# app/db/migrations/__init__.py -> repo root
ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


def alembic_config(database_url: str) -> Config:
    cfg = Config(str(ALEMBIC_INI)) if ALEMBIC_INI.exists() else Config()
    cfg.set_main_option("script_location", str(Path(__file__).parent))
    cfg.attributes["database_url"] = database_url
    return cfg


def upgrade_to_head(database_url: str) -> None:
    """Blocking: run in a worker thread (env.py starts its own event loop)."""
    command.upgrade(alembic_config(database_url), "head")


def current_revision(database_url: str) -> str | None:
    """Head revision the code expects (for logging)."""
    from alembic.script import ScriptDirectory

    return ScriptDirectory.from_config(alembic_config(database_url)).get_current_head()
