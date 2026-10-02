import time

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app


def wait_for_status(client: TestClient, path: str, status: int, timeout: float = 10.0):
    """The pool connects in the background, so poll until the expected status."""
    deadline = time.monotonic() + timeout
    while True:
        response = client.get(path)
        if response.status_code == status or time.monotonic() > deadline:
            return response
        time.sleep(0.2)


def test_readiness_ok_when_database_is_up():
    # Needs Postgres from `docker compose up -d db`.
    with TestClient(create_app(Settings())) as client:
        response = wait_for_status(client, "/readyz", 200)
        assert response.status_code == 200, response.json()
        assert response.json() == {"status": "ready", "database": "ok"}


def test_readiness_fails_closed_when_database_is_unreachable():
    settings = Settings(database_url="postgresql://app:app@127.0.0.1:1/seats")
    with TestClient(create_app(settings)) as client:
        response = client.get("/readyz")
        assert response.status_code == 503
        assert response.json()["status"] == "not_ready"
        # Liveness is independent of the database.
        assert client.get("/healthz").status_code == 200
