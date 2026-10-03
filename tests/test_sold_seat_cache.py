"""The sold-seat cache may only ever produce answers the database would give."""

import time
import uuid

import pytest
from prometheus_client import REGISTRY

from app.repositories import ReservationRepository
from app.services.sold_seat_cache import SoldSeatCache
from tests.conftest import auth, make_show, make_user, reserve

SHOW = uuid.uuid4()


def cache_declines() -> float:
    return REGISTRY.get_sample_value("sold_seat_cache_declines_total") or 0.0


# --- unit -------------------------------------------------------------------


def test_taken_returns_sorted_hits_only():
    cache = SoldSeatCache(ttl_seconds=60)
    cache.mark_taken(SHOW, {"B2": "bob", "A1": "bob"})
    assert cache.taken_by_others(SHOW, ["A1", "A2", "B2"], "alice") == ["A1", "B2"]
    assert cache.taken_by_others(uuid.uuid4(), ["A1"], "alice") == []  # other show


def test_owner_is_never_declined_from_memory():
    # Bob's own seat may be a retry of his booking: only the database can tell.
    cache = SoldSeatCache(ttl_seconds=60)
    cache.mark_taken(SHOW, {"A1": "bob", "A2": "carol"})
    assert cache.taken_by_others(SHOW, ["A1"], "bob") == []
    assert cache.taken_by_others(SHOW, ["A1", "A2"], "bob") == []
    assert cache.taken_by_others(SHOW, ["A2"], "bob") == ["A2"]


def test_release_and_expiry():
    cache = SoldSeatCache(ttl_seconds=0.05)
    cache.mark_taken(SHOW, {"A1": "bob", "A2": "bob"})
    cache.release(SHOW, ["A1"])
    assert cache.taken_by_others(SHOW, ["A1", "A2"], "alice") == ["A2"]
    time.sleep(0.06)
    assert cache.taken_by_others(SHOW, ["A2"], "alice") == []
    assert len(cache) == 0  # expired entries are dropped on read


def test_stale_read_cannot_recache_a_released_seat():
    cache = SoldSeatCache(ttl_seconds=60)
    cache.mark_taken(SHOW, {"A1": "bob"})
    epoch = cache.epoch()          # a request starts reading the DB: A1 is taken
    cache.release(SHOW, ["A1"])    # meanwhile Bob's cancel commits
    cache.mark_taken(SHOW, {"A1": "bob", "A2": "carol"}, since=epoch)  # stale writer
    assert cache.taken_by_others(SHOW, ["A1"], "alice") == []
    assert cache.taken_by_others(SHOW, ["A2"], "alice") == ["A2"]  # not released: cached
    # A read that starts after the release may cache it again.
    cache.mark_taken(SHOW, {"A1": "dave"}, since=cache.epoch())
    assert cache.taken_by_others(SHOW, ["A1"], "alice") == ["A1"]


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


@pytest.mark.anyio
async def test_retry_replays_even_if_another_request_cached_the_seat_first(app, client):
    """Race: Alice's booking commits; before her request records its key, Bob's
    precheck sees the seat taken and caches it. Alice's retry must still get
    her 200 replay, not a 409 from memory."""
    show = await make_show(client)
    alice = await make_user(client)
    first = await reserve(client, show["id"], alice, ["A12"], key="k-race")
    assert first.status_code == 201
    cache = app.state.reservation_service._cache
    cache._keys.clear()  # the window: key not yet remembered...
    cache.mark_taken(uuid.UUID(show["id"]), {"A12": alice["_user"]})  # ...seat cached by Bob
    again = await reserve(client, show["id"], alice, ["A12"], key="k-race")
    assert again.status_code == 200
    assert again.json()["reservation_id"] == first.json()["reservation_id"]


@pytest.mark.anyio
async def test_cancel_racing_a_decline_leaves_the_seat_rebookable(app, client, monkeypatch):
    """Race: Bob's precheck reads A12 as taken, then Alice's cancel commits and
    clears the cache, then Bob's request caches A12. The seat must not stay
    declined from memory."""
    show = await make_show(client)
    alice, bob = await make_user(client), await make_user(client)
    booked = (await reserve(client, show["id"], alice, ["A12"])).json()
    service = app.state.reservation_service
    service._cache.release(uuid.UUID(show["id"]), ["A12"])  # cold cache: Bob goes to the DB

    real_find_by_key = ReservationRepository.find_by_key
    cancelled = False

    async def find_by_key_after_cancel(self, user_id, key):
        nonlocal cancelled
        if not cancelled:  # runs between Bob's precheck read and his cache write
            cancelled = True
            await service.cancel(uuid.UUID(booked["reservation_id"]), alice["_user"])
        return await real_find_by_key(self, user_id, key)

    monkeypatch.setattr(ReservationRepository, "find_by_key", find_by_key_after_cancel)
    assert (await reserve(client, show["id"], bob, ["A12"])).status_code == 409  # read before cancel
    assert cancelled
    assert (await reserve(client, show["id"], bob, ["A12"])).status_code == 201


@pytest.mark.anyio
@pytest.mark.parametrize("warm", [False, True])
async def test_decline_reason_does_not_depend_on_the_cache(app, client, warm):
    show = await make_show(client, per_user_limit=1)
    alice, bob = await make_user(client), await make_user(client)
    assert (await reserve(client, show["id"], alice, ["A1"])).status_code == 201
    assert (await reserve(client, show["id"], bob, ["B1"])).status_code == 201  # Bob at his limit
    if not warm:
        app.state.reservation_service._cache.release(uuid.UUID(show["id"]), ["A1"])

    unknown = await reserve(client, show["id"], bob, ["A1", "NOPE"])
    assert (unknown.status_code, unknown.json()["error"]) == (422, "invalid_request")
    if not warm:
        app.state.reservation_service._cache.release(uuid.UUID(show["id"]), ["A1"])
    taken = await reserve(client, show["id"], bob, ["A1"])
    assert (taken.status_code, taken.json()["error"]) == (409, "seat_taken")
