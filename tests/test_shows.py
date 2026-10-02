import pytest

from tests.conftest import ADMIN, make_show, make_user

pytestmark = pytest.mark.anyio


async def test_create_show_returns_every_seat_available(client):
    show = await make_show(client, seats=["A1", "A2", "A10"], price_paise=25000)
    assert show["total_seats"] == 3
    assert show["per_user_limit"] == 4
    assert show["counts"] == {"available": 3, "held": 0, "confirmed": 0}
    assert show["invariant_ok"] is True
    # Seat order is the admin's order, not lexical.
    assert [s["label"] for s in show["seats"]] == ["A1", "A2", "A10"]
    assert {s["status"] for s in show["seats"]} == {"available"}


async def test_get_show_counts_only(client):
    show = await make_show(client)
    resp = await client.get(f"/shows/{show['id']}", params={"seats": "false"})
    assert resp.status_code == 200
    assert "seats" not in resp.json()
    assert resp.json()["counts"]["available"] == show["total_seats"]


async def test_create_show_requires_admin(client):
    body = {"name": "x", "seats": ["A1"], "price_paise": 100}
    assert (await client.post("/shows", json=body)).status_code == 401
    user = await make_user(client)
    resp = await client.post("/shows", json=body, headers={"Authorization": user["Authorization"]})
    assert resp.status_code == 401


@pytest.mark.parametrize(
    "body",
    [
        {"name": "dup", "seats": ["A1", "A1"], "price_paise": 100},
        {"name": "float", "seats": ["A1"], "price_paise": 250.5},
        {"name": "string-money", "seats": ["A1"], "price_paise": "250"},
        {"name": "zero", "seats": ["A1"], "price_paise": 0},
        {"name": "empty", "seats": [], "price_paise": 100},
        {"name": "bad-label", "seats": ["A 1"], "price_paise": 100},
    ],
)
async def test_create_show_validation(client, body):
    resp = await client.post("/shows", json=body, headers=ADMIN)
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


async def test_unknown_show_is_404_and_bad_id_is_422(client):
    assert (await client.get("/shows/00000000-0000-0000-0000-000000000000")).status_code == 404
    assert (await client.get("/shows/not-a-uuid")).status_code == 422
