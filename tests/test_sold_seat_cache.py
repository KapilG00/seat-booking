"""The sold-seat cache may only ever produce answers the database would give."""

import time
import uuid

import pytest
from prometheus_client import REGISTRY

from app.services.sold_seat_cache import SoldSeatCache
from tests.conftest import auth, make_show, make_user, reserve

SHOW = uuid.uuid4()


def cache_declines() -> float:
    return REGISTRY.get_sample_value("sold_seat_cache_declines_total") or 0.0


# --- unit -------------------------------------------------------------------


def test_taken_returns_sorted_hits_only():
    cache = SoldSeatCache(ttl_seconds=60)
    cache.mark_taken(SHOW, ["B2", "A1"])
    assert cache.taken(SHOW, ["A1", "A2", "B2"]) == ["A1", "B2"]
    assert cache.taken(uuid.uuid4(), ["A1"]) == []  # other show


def test_release_and_expiry():
    cache = SoldSeatCache(ttl_seconds=0.05)
    cache.mark_taken(SHOW, ["A1", "A2"])
    cache.release(SHOW, ["A1"])
    assert cache.taken(SHOW, ["A1", "A2"]) == ["A2"]
    time.sleep(0.06)
    assert cache.taken(SHOW, ["A2"]) == []
    assert len(cache) == 0  # expired entries are dropped on read


def test_key_memory_is_bounded():
    cache = SoldSeatCache(ttl_seconds=60, max_keys=2)
    for k in ("k1", "k2", "k3"):
        cache.remember_key("u", k)
    assert not cache.knows_key("u", "k1")
    assert cache.knows_key("u", "k2") and cache.knows_key("u", "k3")


# --- through the API -----------------------------------------------------------


@pytest.mark.anyio
async def test_sold_seat_is_declined_from_memory(client):
    show = await make_show(client)
    alice, bob = await make_user(client), await make_user(client)
    assert (await reserve(client, show["id"], alice, ["A12"])).status_code == 201
    before = cache_declines()
    resp = await reserve(client, show["id"], bob, ["A12", "A13"])
    assert resp.status_code == 409
    assert resp.json() == {"error": "seat_taken", "message": "seat already taken", "seats": ["A12"]}
    assert cache_declines() == before + 1


@pytest.mark.anyio
async def test_retry_of_successful_request_still_replays(client):
    show = await make_show(client)
    alice = await make_user(client)
    first = await reserve(client, show["id"], alice, ["A12"], key="k-retry")
    again = await reserve(client, show["id"], alice, ["A12"], key="k-retry")
    assert (first.status_code, again.status_code) == (201, 200)
    assert again.json()["reservation_id"] == first.json()["reservation_id"]


@pytest.mark.anyio
async def test_reused_key_on_a_seat_sold_to_someone_else_is_key_reuse(client):
    show = await make_show(client)
    alice, bob = await make_user(client), await make_user(client)
    assert (await reserve(client, show["id"], alice, ["A1"], key="k-alice")).status_code == 201
    assert (await reserve(client, show["id"], bob, ["A2"])).status_code == 201  # A2 now cached
    resp = await reserve(client, show["id"], alice, ["A2"], key="k-alice")
    assert resp.status_code == 409
    assert resp.json()["error"] == "idempotency_key_reused"


@pytest.mark.anyio
async def test_cancelled_seat_is_rebookable_immediately(client):
    show = await make_show(client)
    alice, bob = await make_user(client), await make_user(client)
    booked = (await reserve(client, show["id"], alice, ["A12"])).json()
    assert (await reserve(client, show["id"], bob, ["A12"])).status_code == 409  # cached as taken
    cancel = await client.post(f"/reservations/{booked['reservation_id']}/cancel", headers=auth(alice))
    assert cancel.status_code == 200
    assert (await reserve(client, show["id"], bob, ["A12"])).status_code == 201


@pytest.mark.anyio
async def test_over_limit_request_reports_limit_even_when_seat_is_cached(client):
    show = await make_show(client, per_user_limit=2)
    alice, bob = await make_user(client), await make_user(client)
    assert (await reserve(client, show["id"], alice, ["A1"])).status_code == 201
    resp = await reserve(client, show["id"], bob, ["A1", "A2", "A3"])
    assert resp.status_code == 409
    assert resp.json()["error"] == "per_user_limit"
