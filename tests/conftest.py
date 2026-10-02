"""Shared fixtures. Tests run against the real Postgres from
`docker compose up -d db`; every test creates its own show and users, so
tests are isolated without truncating tables."""

import uuid

import httpx2
import pytest

from app.core.config import DEV_ADMIN_TOKEN, Settings
from app.main import create_app

ADMIN = {"Authorization": f"Bearer {DEV_ADMIN_TOKEN}"}


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def app():
    app = create_app(Settings(environment="test", access_log=False))
    async with app.router.lifespan_context(app):
        assert await app.state.db.wait_ready(15), "Postgres not reachable: docker compose up -d db"
        yield app


@pytest.fixture
async def client(app):
    transport = httpx2.ASGITransport(app=app)
    async with httpx2.AsyncClient(transport=transport, base_url="http://test", timeout=60) as c:
        yield c


async def make_user(client, prefix: str = "user") -> dict:
    name = f"{prefix}-{uuid.uuid4().hex[:10]}"
    resp = await client.post("/auth/token", json={"username": name}, headers=ADMIN)
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}", "_user": name}


def auth(user: dict) -> dict:
    return {"Authorization": user["Authorization"]}


async def make_show(client, seats=None, price_paise=25000, per_user_limit=None) -> dict:
    body = {
        "name": f"show-{uuid.uuid4().hex[:6]}",
        "seats": seats or [f"{row}{n}" for row in "AB" for n in range(1, 21)],
        "price_paise": price_paise,
    }
    if per_user_limit is not None:
        body["per_user_limit"] = per_user_limit
    resp = await client.post("/shows", json=body, headers=ADMIN)
    assert resp.status_code == 201, resp.text
    return resp.json()


async def reserve(client, show_id, user, seats, key=None, extra=None):
    body = {"seats": seats, "idempotency_key": key or uuid.uuid4().hex, **(extra or {})}
    return await client.post(f"/shows/{show_id}/reserve", json=body, headers=auth(user))
