"""Map exceptions to HTTP responses.

Domain outcomes are clean 4xx. Infrastructure trouble (DB down, pool
exhausted, statement timeout) is a retryable 503, never an unhandled 500.
"""

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.exc import DBAPIError
from sqlalchemy.exc import TimeoutError as PoolTimeoutError

from app.core.errors import DomainError
from app.db.database import DatabaseUnavailable, sqlstate
from app.observability.middleware import annotate

logger = structlog.get_logger(__name__)

_UNAVAILABLE_ERRORS = (DatabaseUnavailable, TimeoutError, PoolTimeoutError, OSError)
# SQLSTATE classes that mean "the database is unavailable/overloaded right now":
# 08 connection exception, 53 insufficient resources, 57 operator intervention
# (includes 57014 statement timeout and server shutdown).
_TRANSIENT_SQLSTATE_CLASSES = ("08", "53", "57")


def _is_transient_db_error(exc: DBAPIError) -> bool:
    code = sqlstate(exc) or ""
    return exc.connection_invalidated or code[:2] in _TRANSIENT_SQLSTATE_CLASSES or code == ""


async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(exc.body(), status_code=exc.status_code)


async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
    annotate(request.scope, outcome="invalid_request")
    return JSONResponse(
        {
            "error": "invalid_request",
            "message": "request validation failed",
            "details": [
                {"loc": list(e.get("loc", ())), "msg": e.get("msg"), "type": e.get("type")}
                for e in exc.errors()
            ],
        },
        status_code=422,
    )


async def _unavailable(request: Request, exc: Exception) -> JSONResponse:
    logger.warning("dependency_unavailable", error=repr(exc))
    annotate(request.scope, outcome="unavailable", error=type(exc).__name__)
    return JSONResponse(
        {"error": "service_unavailable", "message": "temporarily unavailable, retry"},
        status_code=503,
        headers={"Retry-After": "1"},
    )


async def _database_error(request: Request, exc: DBAPIError) -> JSONResponse:
    if _is_transient_db_error(exc):
        return await _unavailable(request, exc)
    # A real bug (bad SQL, violated constraint we didn't anticipate): make it loud.
    logger.error("database_error", sqlstate=sqlstate(exc), error=repr(exc.orig))
    annotate(request.scope, outcome="error", error="database_error")
    return JSONResponse(
        {"error": "internal_error", "message": "unexpected database error"}, status_code=500
    )


def install_error_handlers(app: FastAPI) -> None:
    app.add_exception_handler(DomainError, _domain_error)
    app.add_exception_handler(RequestValidationError, _validation_error)
    for exc_type in _UNAVAILABLE_ERRORS:
        app.add_exception_handler(exc_type, _unavailable)
    app.add_exception_handler(DBAPIError, _database_error)
