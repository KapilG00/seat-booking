"""FastAPI dependencies: settings, services and identity.

Identity comes only from the bearer token (app/core/security.py), never from
the request body.
"""

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.core.config import Settings
from app.core.errors import Unauthorized
from app.core.security import is_admin_token, user_id_from_token
from app.db.database import Database
from app.services import ReservationService, ShowService

_bearer = HTTPBearer(auto_error=False)


def get_settings(request: Request) -> Settings:
    return request.app.state.settings


def get_db(request: Request) -> Database:
    return request.app.state.db


def get_show_service(request: Request) -> ShowService:
    return request.app.state.show_service


def get_reservation_service(request: Request) -> ReservationService:
    return request.app.state.reservation_service


async def current_user(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> str:
    if creds is None:
        raise Unauthorized("missing bearer token")
    return user_id_from_token(creds.credentials, settings)


async def require_admin(
    creds: HTTPAuthorizationCredentials | None = Depends(_bearer),
    settings: Settings = Depends(get_settings),
) -> None:
    if creds is None or not is_admin_token(creds.credentials, settings):
        raise Unauthorized("admin token required")
