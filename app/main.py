from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI

from app.api.error_handlers import install_error_handlers
from app.api.router import api_router
from app.core.config import Settings, get_settings
from app.core.logging import configure_logging
from app.db.database import Database
from app.observability.middleware import RequestContextMiddleware
from app.services import ReservationService, ShowService
from app.services.sold_seat_cache import SoldSeatCache

logger = structlog.get_logger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    configure_logging(settings.log_level)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        logger.info("startup", environment=settings.environment)
        db = Database(settings)
        db.start()  # connects and migrates in the background; /readyz reports progress
        app.state.db = db
        app.state.show_service = ShowService(db, settings)
        cache = None
        if settings.sold_seat_cache_enabled:
            cache = SoldSeatCache(settings.sold_seat_cache_ttl_seconds)
        app.state.reservation_service = ReservationService(db, app.state.show_service, cache)
        yield
        await db.close()

    app = FastAPI(
        title=settings.app_name,
        lifespan=lifespan,
        description="Assigned-seat reservations that stay correct under on-sale contention.",
    )
    app.state.settings = settings
    install_error_handlers(app)
    app.add_middleware(RequestContextMiddleware, access_log=settings.access_log)
    app.include_router(api_router)

    @app.get("/", include_in_schema=False)
    async def root() -> dict:
        return {
            "service": settings.app_name,
            "docs": "/docs",
            "health": {"liveness": "/healthz", "readiness": "/readyz"},
            "metrics": "/metrics",
        }

    return app


app = create_app()
