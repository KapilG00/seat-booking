import asyncio
import random
import uuid
from collections import Counter

import pytest

from tests.conftest import auth, make_show, make_user, reserve

pytestmark = pytest.mark.anyio


async def show_state(client, show_id) -> dict:
    resp = await client.get(f"/shows/{show_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["invariant_ok"], body["counts"]
    return body


def seat_status(show: dict) -> dict[str, str]:
    return {s["label"]: s["status"] for s in show["seats"]}


async def test_reserve_success_shape(client):
    show = await make_show(client, price_paise=25000)
    user = await make_user(client)
    resp = await reserve(client, show["id"], user, ["A12", "A13"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["show_id"] == show["id"]
    assert body["user_id"] == user["_user"]
    assert body["seats"] == ["A12", "A13"]
    assert body["amount_paise"] == 50000
    assert body["status"] == "confirmed"
    state = await show_state(client, show["id"])
    assert seat_status(state)["A12"] == "confirmed"
    assert state["counts"]["confirmed"] == 2


async def test_hot_seat_race_has_exactly_one_winner(client):
    show = await make_show(client)
    users = await asyncio.gather(*(make_user(client) for _ in range(200)))
    responses = await asyncio.gather(*(reserve(client, show["id"], u, ["A12"]) for u in users))
    codes = Counter(r.status_code for r in responses)
    assert codes == {201: 1, 409: 199}, codes
    assert {r.json()["error"] for r in responses if r.status_code == 409} == {"seat_taken"}
    state = await show_state(client, show["id"])
    assert state["counts"]["confirmed"] == 1


async def test_partial_request_is_all_or_nothing(client):
    show = await make_show(client)
    alice, bob = await make_user(client), await make_user(client)
    assert (await reserve(client, show["id"], alice, ["A13"])).status_code == 201
    resp = await reserve(client, show["id"], bob, ["A12", "A13"])
    assert resp.status_code == 409
    assert resp.json()["seats"] == ["A13"]
    # A12 was free but must not have been taken by the failed request.
    assert seat_status(await show_state(client, show["id"]))["A12"] == "available"


async def test_overlapping_multi_seat_requests_never_deadlock_or_double_sell(client):
    labels = [f"C{n}" for n in range(1, 9)]
    show = await make_show(client, seats=labels, per_user_limit=4)
    users = await asyncio.gather(*(make_user(client) for _ in range(120)))
    rng = random.Random(7)
    requests = []
    for u in users:
        picked = rng.sample(labels, rng.choice([2, 3]))
        rng.shuffle(picked)  # requests arrive in arbitrary orders
        requests.append(reserve(client, show["id"], u, picked))
    responses = await asyncio.gather(*requests)
    assert all(r.status_code in (201, 409) for r in responses), Counter(r.status_code for r in responses)
    owners: dict[str, str] = {}
    for r in responses:
        if r.status_code == 201:
            for seat in r.json()["seats"]:
                assert seat not in owners, f"{seat} sold twice"
                owners[seat] = r.json()["user_id"]
    state = await show_state(client, show["id"])
    assert state["counts"]["confirmed"] == len(owners)


async def test_per_user_limit_holds_under_concurrency(client):
    show = await make_show(client, per_user_limit=4)
    user = await make_user(client)
    seats = [f"A{n}" for n in range(1, 11)]
    responses = await asyncio.gather(*(reserve(client, show["id"], user, [s]) for s in seats))
    codes = Counter(r.status_code for r in responses)
    assert codes == {201: 4, 409: 6}, codes
    assert {r.json()["error"] for r in responses if r.status_code == 409} == {"per_user_limit"}
    assert (await show_state(client, show["id"]))["counts"]["confirmed"] == 4


async def test_single_request_over_limit_is_declined(client):
    show = await make_show(client, per_user_limit=2)
    user = await make_user(client)
    resp = await reserve(client, show["id"], user, ["A1", "A2", "A3"])
    assert resp.status_code == 409
    assert resp.json()["error"] == "per_user_limit"


async def test_concurrent_retries_with_same_key_reserve_once(client):
    show = await make_show(client)
    user = await make_user(client)
    key = uuid.uuid4().hex
    responses = await asyncio.gather(*(reserve(client, show["id"], user, ["B5"], key=key) for _ in range(20)))
    codes = Counter(r.status_code for r in responses)
    assert codes == {201: 1, 200: 19}, codes
    assert len({r.json()["reservation_id"] for r in responses}) == 1
    assert all(r.headers.get("idempotent-replayed") == "true" for r in responses if r.status_code == 200)
    assert (await show_state(client, show["id"]))["counts"]["confirmed"] == 1


async def test_same_key_different_seats_is_rejected(client):
    show = await make_show(client)
    user = await make_user(client)
    key = uuid.uuid4().hex
    assert (await reserve(client, show["id"], user, ["B1"], key=key)).status_code == 201
    resp = await reserve(client, show["id"], user, ["B2"], key=key)
    assert resp.status_code == 409
    assert resp.json()["error"] == "idempotency_key_reused"
    assert seat_status(await show_state(client, show["id"]))["B2"] == "available"


async def test_idempotency_key_from_header(client):
    show = await make_show(client)
    user = await make_user(client)
    headers = {**auth(user), "Idempotency-Key": "hdr-key-1"}
    first = await client.post(f"/shows/{show['id']}/reserve", json={"seats": ["A1"]}, headers=headers)
    again = await client.post(f"/shows/{show['id']}/reserve", json={"seats": ["A1"]}, headers=headers)
    assert (first.status_code, again.status_code) == (201, 200)
    assert first.json()["reservation_id"] == again.json()["reservation_id"]


async def test_missing_idempotency_key_is_rejected(client):
    show = await make_show(client)
    user = await make_user(client)
    resp = await client.post(f"/shows/{show['id']}/reserve", json={"seats": ["A1"]}, headers=auth(user))
    assert resp.status_code == 422


async def test_spoofed_user_id_in_body_is_ignored(client):
    show = await make_show(client)
    alice = await make_user(client, "alice")
    resp = await reserve(client, show["id"], alice, ["A1"], extra={"user_id": "victim"})
    assert resp.status_code == 201
    assert resp.json()["user_id"] == alice["_user"]


async def test_reserve_requires_valid_token(client):
    show = await make_show(client)
    url = f"/shows/{show['id']}/reserve"
    body = {"seats": ["A1"], "idempotency_key": "k"}
    assert (await client.post(url, json=body)).status_code == 401
    bad = {"Authorization": "Bearer not-a-jwt"}
    assert (await client.post(url, json=body, headers=bad)).status_code == 401


async def test_unknown_seat_and_unknown_show(client):
    show = await make_show(client)
    user = await make_user(client)
    assert (await reserve(client, show["id"], user, ["Z99"])).status_code == 422
    missing = "00000000-0000-0000-0000-000000000000"
    assert (await reserve(client, missing, user, ["A1"])).status_code == 404


async def test_cancel_releases_seat_for_rebooking(client):
    show = await make_show(client, per_user_limit=1)
    alice, bob = await make_user(client), await make_user(client)
    booked = (await reserve(client, show["id"], alice, ["A12"])).json()

    resp = await client.post(f"/reservations/{booked['reservation_id']}/cancel", headers=auth(alice))
    assert resp.status_code == 200
    assert resp.json()["status"] == "cancelled"
    assert seat_status(await show_state(client, show["id"]))["A12"] == "available"

    # Bob can now take it; Alice's allowance (limit 1) was given back too.
    assert (await reserve(client, show["id"], bob, ["A12"])).status_code == 201
    assert (await reserve(client, show["id"], alice, ["A13"])).status_code == 201


async def test_repeat_cancel_never_resurrects_someone_elses_seat(client):
    show = await make_show(client)
    alice, bob = await make_user(client), await make_user(client)
    booked = (await reserve(client, show["id"], alice, ["A12"])).json()
    cancel_url = f"/reservations/{booked['reservation_id']}/cancel"
    assert (await client.post(cancel_url, headers=auth(alice))).status_code == 200
    assert (await reserve(client, show["id"], bob, ["A12"])).status_code == 201

    again = await client.post(cancel_url, headers=auth(alice))
    assert again.status_code == 200 and again.json()["status"] == "cancelled"
    assert seat_status(await show_state(client, show["id"]))["A12"] == "confirmed"


async def test_only_owner_can_cancel_or_view(client):
    show = await make_show(client)
    alice, mallory = await make_user(client), await make_user(client)
    booked = (await reserve(client, show["id"], alice, ["A1"])).json()
    rid = booked["reservation_id"]
    assert (await client.post(f"/reservations/{rid}/cancel", headers=auth(mallory))).status_code == 404
    assert (await client.get(f"/reservations/{rid}", headers=auth(mallory))).status_code == 404
    assert (await client.get(f"/reservations/{rid}", headers=auth(alice))).status_code == 200
    assert seat_status(await show_state(client, show["id"]))["A1"] == "confirmed"


async def test_concurrent_cancel_and_rebook_keep_invariant(client):
    labels = [f"D{n}" for n in range(1, 6)]
    show = await make_show(client, seats=labels)
    owners = await asyncio.gather(*(make_user(client) for _ in labels))
    booked = await asyncio.gather(*(reserve(client, show["id"], u, [s]) for u, s in zip(owners, labels)))
    challengers = await asyncio.gather(*(make_user(client) for _ in range(50)))
    tasks = [
        client.post(f"/reservations/{b.json()['reservation_id']}/cancel", headers=auth(u))
        for b, u in zip(booked, owners)
    ]
    tasks += [reserve(client, show["id"], c, [random.choice(labels)]) for c in challengers]
    responses = await asyncio.gather(*tasks)
    assert all(r.status_code < 500 for r in responses)
    rebooked = [r for r in responses[len(labels):] if r.status_code == 201]
    assert len(rebooked) == len({r.json()["seats"][0] for r in rebooked})
    state = await show_state(client, show["id"])
    assert state["counts"]["confirmed"] == len(rebooked)


async def test_only_admin_can_mint_user_tokens(client):
    # Otherwise anyone could mint a token for someone else's username.
    assert (await client.post("/auth/token", json={"username": "alice"})).status_code == 401
    mallory = await make_user(client, "mallory")
    resp = await client.post("/auth/token", json={"username": "alice"}, headers=auth(mallory))
    assert resp.status_code == 401
