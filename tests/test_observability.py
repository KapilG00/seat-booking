import re

import pytest

from tests.conftest import make_show, make_user, reserve

pytestmark = pytest.mark.anyio


def metric(text: str, name: str, **labels) -> float:
    label_re = ",".join(f'{k}="{re.escape(v)}"' for k, v in sorted(labels.items()))
    for line in text.splitlines():
        if line.startswith(name + "{"):
            got = dict(re.findall(r'(\w+)="([^"]*)"', line[line.index("{"): line.index("}")]))
            if got == labels:
                return float(line.rsplit(" ", 1)[1])
    raise AssertionError(f"{name}{{{label_re}}} not found")


async def test_metrics_reconcile_with_api(client):
    show = await make_show(client, seats=["A1", "A2", "A3"])
    sid = show["id"]
    alice, bob = await make_user(client), await make_user(client)
    first = await reserve(client, sid, alice, ["A1"], key="k1")
    await reserve(client, sid, alice, ["A1"], key="k1")      # replay
    await reserve(client, sid, bob, ["A1"])                  # seat taken

    text = (await client.get("/metrics")).text
    assert metric(text, "reservations_confirmed_total", show_id=sid) == 1
    assert metric(text, "reservations_declined_total", show_id=sid, reason="seat_taken") == 1
    assert metric(text, "reservations_declined_total", show_id=sid, reason="idempotent_replay") == 1
    assert metric(text, "reservations_declined_total", show_id=sid, reason="per_user_limit") == 0

    api = (await client.get(f"/shows/{sid}")).json()["counts"]
    assert metric(text, "seats_available", show_id=sid) == api["available"] == 2
    assert metric(text, "show_seats", show_id=sid, status="confirmed") == api["confirmed"] == 1
    assert metric(text, "show_invariant_ok", show_id=sid) == 1
    assert first.status_code == 201


async def test_request_id_is_echoed_or_generated(client):
    resp = await client.get("/healthz", headers={"X-Request-ID": "trace-123"})
    assert resp.headers["x-request-id"] == "trace-123"
    generated = (await client.get("/healthz")).headers["x-request-id"]
    assert re.fullmatch(r"[0-9a-f]{32}", generated)
    # Malformed ids are replaced rather than echoed into logs.
    assert (await client.get("/healthz", headers={"X-Request-ID": "bad id\n"})).headers["x-request-id"] != "bad id\n"
