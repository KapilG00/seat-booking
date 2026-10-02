from fastapi import APIRouter

from app.api.routes import auth, health, metrics, reservations, shows

api_router = APIRouter()
for module in (health, metrics, auth, shows, reservations):
    api_router.include_router(module.router)
