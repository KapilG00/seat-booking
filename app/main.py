from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.config import Settings, get_settings
from app.db import Database
from app.routes import health


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.db = Database(settings)
        app.state.db.start()
        yield
        await app.state.db.close()

    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.include_router(health.router)
    return app


app = create_app()
