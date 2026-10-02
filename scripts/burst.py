# /// script
# requires-python = ">=3.12"
# dependencies = ["aiohttp>=3.9"]
# ///
"""On-sale stampede against a live deployment.

    uv run scripts/burst.py https://<your-service>.onrender.com --admin-token $ADMIN_TOKEN

Phases: hot-seat storm (many users, one seat) -> mixed stampede with
concurrent same-key retries -> idempotency / per-user-limit / identity /
cancel-rebook checks -> final reconciliation against GET /shows and /metrics.
Prints the outcome distribution and exits non-zero if any check fails.
"""

import argparse
import asyncio
import json
import os
import random
import re
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import dataclass, field

import aiohttp

HOT_STORM_SEAT = "A12"
TEST_ZONE = [f"Z{n}" for n in range(1, 31)]  # kept out of the stampede for functional checks


@dataclass
class Outcome:
    phase: str
    status: int  # 0 = transport error / timeout
    reason: str
    latency: float
    body: dict = field(default_factory=dict)
    key: str = ""


class Burst:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.base = args.base_url.rstrip("/")
        self.outcomes: list[Outcome] = []
        self.checks: list[tuple[str, bool, str]] = []
        self.invariant_samples: list[bool] = []
        self.monitor_5xx = 0
        self.cancelled_seats = 0
        self.session: aiohttp.ClientSession | None = None
        self.sem = asyncio.Semaphore(args.concurrency)

    async def http(self, method: str, path: str, *, json=None, headers=None, params=None) -> "Resp":
        """One request; aiohttp because httpx's pool collapses at hundreds of
        concurrent connections and would make the *client* the bottleneck."""
        async with self.session.request(method, self.base + path, json=json, headers=headers, params=params) as r:
            text = await r.text()
            return Resp(r.status, text)

    # ---------------------------------------------------------------- helpers
    def check(self, name: str, ok: bool, detail: str = "") -> None:
        self.checks.append((name, ok, detail))

    async def reserve(self, phase, show_id, token, seats, key=None, extra=None, gate=None) -> Outcome:
        key = key or uuid.uuid4().hex
        body = {"seats": seats, "idempotency_key": key, **(extra or {})}
        headers = {"Authorization": f"Bearer {token}", "X-Request-ID": f"burst-{phase}-{uuid.uuid4().hex[:12]}"}
        async with self.sem:
            if gate is not None:
                await gate.wait()
            start = time.perf_counter()
            try:
                resp = await self.http("POST", f"/shows/{show_id}/reserve", json=body, headers=headers)
                latency = time.perf_counter() - start
                data = resp.json()
                reason = {201: "confirmed", 200: "idempotent_replay"}.get(resp.status_code) or data.get(
                    "error", f"http_{resp.status_code}"
                )
                out = Outcome(phase, resp.status_code, reason, latency, data, key)
            except (aiohttp.ClientError, TimeoutError) as exc:
                out = Outcome(phase, 0, f"transport:{type(exc).__name__}", time.perf_counter() - start, key=key)
        self.outcomes.append(out)
        return out

    async def token(self, username: str) -> str:
        async with self.sem:
            resp = await self.http(
                "POST", "/auth/token", json={"username": username},
                headers={"Authorization": f"Bearer {self.args.admin_token}"},
            )
        if resp.status_code != 200:
            sys.exit(f"token mint failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()["access_token"]

    async def show(self, show_id: str, seats: bool = True) -> dict:
        resp = await self.http("GET", f"/shows/{show_id}", params={"seats": str(seats).lower()})
        if resp.status_code != 200:
            raise RuntimeError(f"GET /shows failed: {resp.status_code} {resp.text[:200]}")
        return resp.json()

    # ----------------------------------------------------------------- phases
    async def wait_ready(self) -> None:
        print(f"Waiting for {self.base}/readyz (cold start can take a minute)...")
        deadline = time.monotonic() + self.args.ready_timeout
        while True:
            try:
                resp = await self.http("GET", "/readyz")
                if resp.status_code == 200:
                    print("  ready")
                    return
            except (aiohttp.ClientError, TimeoutError):
                pass
            if time.monotonic() > deadline:
                sys.exit("service never became ready")
            await asyncio.sleep(2)

    async def setup(self) -> tuple[dict, list[str], list[str], list[str]]:
        rows = [chr(ord("A") + i) for i in range(self.args.rows)]
        seats = [f"{r}{n}" for r in rows for n in range(1, self.args.seats_per_row + 1)] + TEST_ZONE
        resp = await self.http(
            "POST",
            "/shows",
            json={"name": f"burst-{time.strftime('%H%M%S')}", "seats": seats,
                  "price_paise": 25000, "per_user_limit": 4},
            headers={"Authorization": f"Bearer {self.args.admin_token}"},
        )
        if resp.status_code != 201:
            sys.exit(f"create show failed: {resp.status_code} {resp.text[:300]}")
        show = resp.json()
        run = uuid.uuid4().hex[:6]
        n_users = max(self.args.users, self.args.storm)
        print(f"Created show {show['id']} with {show['total_seats']} seats; minting {n_users} user tokens...")
        tokens = await asyncio.gather(*(self.token(f"burst-{run}-{i}") for i in range(n_users)))
        hot = [f"A{n}" for n in range(1, self.args.hot_seats + 1)]
        cold = [s for s in seats if s not in hot and s not in TEST_ZONE]
        return show, list(tokens), hot, cold

    async def hot_seat_storm(self, show_id: str, tokens: list[str]) -> None:
        print(f"\n[storm] {self.args.storm} users -> {HOT_STORM_SEAT} at the same instant")
        gate = asyncio.Event()
        tasks = [
            asyncio.create_task(self.reserve("storm", show_id, t, [HOT_STORM_SEAT], gate=gate))
            for t in tokens[: self.args.storm]
        ]
        await asyncio.sleep(0.2)
        gate.set()
        results = await asyncio.gather(*tasks)
        codes = Counter(r.status for r in results)
        self.check(f"storm: exactly one 201 for {HOT_STORM_SEAT}", codes[201] == 1, f"status codes {dict(codes)}")
        self.check(
            "storm: every loser got 409 seat_taken",
            all(r.reason == "seat_taken" for r in results if r.status != 201),
            str(Counter(r.reason for r in results)),
        )

    async def stampede(self, show_id: str, tokens: list[str], hot: list[str], cold: list[str]) -> None:
        n = self.args.requests
        print(f"\n[stampede] {n} reserve requests, concurrency {self.args.concurrency}, "
              f"{int(self.args.hot_share * 100)}% aimed at {len(hot)} hot seats, ~5% concurrent same-key retries")
        rng = random.Random(self.args.seed)
        specs = []
        while len(specs) < n:
            token = rng.choice(tokens)
            if rng.random() < self.args.hot_share:
                start = rng.randrange(len(hot) - 1)
                seats = hot[start: start + (2 if rng.random() < 0.15 else 1)]
            else:
                seats = rng.sample(cold, rng.choice([1, 1, 2]))
            rng.shuffle(seats)
            key = uuid.uuid4().hex
            copies = 3 if rng.random() < 0.05 else 1  # client retries racing the original
            specs.extend([(token, seats, key)] * copies)
        specs = specs[:n]
        rng.shuffle(specs)

        stop = asyncio.Event()
        monitor = asyncio.create_task(self.monitor(show_id, stop))
        started = time.perf_counter()
        results = await asyncio.gather(*(self.reserve("stampede", show_id, t, s, key=k) for t, s, k in specs))
        elapsed = time.perf_counter() - started
        stop.set()
        await monitor
        print(f"  {len(results)} requests in {elapsed:.1f}s ({len(results) / elapsed:.0f} req/s)")

        # Same key => same reservation, and at most one 201 per key.
        by_key: dict[str, set] = defaultdict(set)
        created_per_key = Counter()
        for r in results:
            if r.status in (200, 201):
                by_key[r.key].add(r.body.get("reservation_id"))
            if r.status == 201:
                created_per_key[r.key] += 1
        self.check("stampede: retries with one key map to one reservation",
                   all(len(ids) == 1 for ids in by_key.values()))
        self.check("stampede: at most one 201 per idempotency key",
                   all(c == 1 for c in created_per_key.values()))
        self.check("stampede: invariant held in every sample taken during the burst",
                   bool(self.invariant_samples) and all(self.invariant_samples),
                   f"{len(self.invariant_samples)} samples")

    async def monitor(self, show_id: str, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                resp = await self.http("GET", f"/shows/{show_id}", params={"seats": "false"})
                if resp.status_code >= 500:
                    self.monitor_5xx += 1
                elif resp.status_code == 200:
                    self.invariant_samples.append(bool(resp.json().get("invariant_ok")))
            except (aiohttp.ClientError, TimeoutError):
                pass
            try:
                await asyncio.wait_for(stop.wait(), timeout=0.5)
            except TimeoutError:
                pass

    async def functional_checks(self, show_id: str, run: str) -> None:
        print("\n[checks] idempotency, per-user limit, identity, cancel/rebook")
        zone = iter(TEST_ZONE)
        alice, bob, carol, dave = await asyncio.gather(
            *(self.token(f"{name}-{run}") for name in ("alice", "bob", "carol", "dave"))
        )

        # Idempotency: 20 concurrent copies of one request.
        key, seat = uuid.uuid4().hex, next(zone)
        results = await asyncio.gather(*(self.reserve("checks", show_id, alice, [seat], key=key) for _ in range(20)))
        codes = Counter(r.status for r in results)
        self.check("idempotency: 20 concurrent same-key requests -> one 201 + 19 replays",
                   codes == Counter({201: 1, 200: 19}), str(dict(codes)))
        self.check("idempotency: all copies return the same reservation_id",
                   len({r.body.get("reservation_id") for r in results}) == 1)
        other = await self.reserve("checks", show_id, alice, [next(zone)], key=key)
        self.check("idempotency: same key + different seats -> 409 idempotency_key_reused",
                   other.status == 409 and other.reason == "idempotency_key_reused", f"{other.status} {other.reason}")

        # Per-user limit: 10 parallel single-seat requests on a limit-4 show.
        seats = [next(zone) for _ in range(10)]
        results = await asyncio.gather(*(self.reserve("checks", show_id, bob, [s]) for s in seats))
        reasons = Counter(r.reason for r in results)
        self.check("per-user limit: 10 parallel reserves -> exactly 4 confirmed",
                   reasons["confirmed"] == 4 and reasons["per_user_limit"] == 6, str(dict(reasons)))

        # Identity is token-derived.
        spoof = await self.reserve("checks", show_id, carol, [next(zone)], extra={"user_id": "bob-" + run})
        self.check("identity: spoofed body user_id is ignored",
                   spoof.status == 201 and spoof.body.get("user_id") == f"carol-{run}",
                   f"user_id={spoof.body.get('user_id')}")
        rid = spoof.body.get("reservation_id")
        steal = await self.http("POST", f"/reservations/{rid}/cancel", headers=_auth(dave))
        self.check("identity: another user cannot cancel the reservation (404)", steal.status_code == 404,
                   str(steal.status_code))

        # Cancel -> rebook -> repeat cancel must not resurrect the seat.
        seat = spoof.body["seats"][0]
        cancel = await self.http("POST", f"/reservations/{rid}/cancel", headers=_auth(carol))
        rebook = await self.reserve("checks", show_id, dave, [seat])
        again = await self.http("POST", f"/reservations/{rid}/cancel", headers=_auth(carol))
        status = {s["label"]: s["status"] for s in (await self.show(show_id))["seats"]}[seat]
        self.check("cancel: owner cancel -> 200, seat rebookable by someone else",
                   cancel.status_code == 200 and rebook.status == 201, f"{cancel.status_code}/{rebook.status}")
        self.check("cancel: repeat cancel leaves the new owner's seat confirmed",
                   again.status_code == 200 and status == "confirmed", f"{again.status_code} seat={status}")
        self.cancelled_seats = len(spoof.body["seats"])

    async def reconcile(self, show_id: str) -> None:
        print("\n[reconcile] GET /shows and /metrics")
        state = await self.show(show_id)
        counts = state["counts"]
        seat_owner: dict[str, set] = defaultdict(set)
        created = [o for o in self.outcomes if o.status == 201]
        for o in created:
            for s in o.body.get("seats", []):
                seat_owner[s].add(o.body.get("reservation_id"))
        # The cancelled-then-rebooked seat legitimately has two reservations over time.
        doubles = {s: ids for s, ids in seat_owner.items() if len(ids) > 1 and not s.startswith("Z")}
        self.check("no seat confirmed to two reservations", not doubles, str(doubles)[:200])
        expected_confirmed = sum(len(o.body.get("seats", [])) for o in created) - self.cancelled_seats
        self.check("final: available + held + confirmed == total_seats",
                   sum(counts.values()) == state["total_seats"] and state["invariant_ok"],
                   f"{counts} total={state['total_seats']}")
        self.check("final: confirmed seats == seats in 201 responses - cancelled",
                   counts["confirmed"] == expected_confirmed, f"api={counts['confirmed']} expected={expected_confirmed}")

        metrics = (await self.http("GET", "/metrics")).text
        m_avail = _metric(metrics, "seats_available", show_id=show_id)
        m_conf = _metric(metrics, "reservations_confirmed_total", show_id=show_id)
        self.check("metrics: seats_available gauge == API available", m_avail == counts["available"],
                   f"metric={m_avail} api={counts['available']}")
        self.check("metrics: reservations_confirmed_total == 201s observed (single instance, no restart)",
                   m_conf == len(created), f"metric={m_conf} observed={len(created)}")
        declined = {
            reason: _metric(metrics, "reservations_declined_total", show_id=show_id, reason=reason)
            for reason in ("seat_taken", "per_user_limit", "idempotent_replay", "idempotency_key_reused")
        }
        print(f"  API counts: {counts}")
        print(f"  metrics: confirmed={m_conf} declined={declined}")

    # ----------------------------------------------------------------- report
    def report(self) -> int:
        print("\n" + "=" * 78)
        print("OUTCOME DISTRIBUTION (all reserve requests)")
        print("=" * 78)
        by_phase: dict[str, list[Outcome]] = defaultdict(list)
        for o in self.outcomes:
            by_phase[o.phase].append(o)
        for phase, items in [("ALL", self.outcomes), *by_phase.items()]:
            reasons = Counter(o.reason for o in items)
            five = sum(1 for o in items if o.status >= 500)
            lat = sorted(o.latency for o in items if o.status)
            pct = (lambda p: lat[min(len(lat) - 1, int(p * len(lat)))] * 1000) if lat else (lambda p: 0)
            print(f"\n{phase:<9} n={len(items):<6} 5xx={five:<4} "
                  f"p50={pct(.5):.0f}ms p95={pct(.95):.0f}ms p99={pct(.99):.0f}ms")
            for reason, count in reasons.most_common():
                print(f"    {reason:<28} {count}")
        total_5xx = sum(1 for o in self.outcomes if o.status >= 500) + self.monitor_5xx
        transport = sum(1 for o in self.outcomes if o.status == 0)
        self.check("zero 5xx across the burst", total_5xx == 0, f"{total_5xx} 5xx")
        self.check("zero transport errors / client timeouts", transport == 0, f"{transport} errors")

        print("\n" + "=" * 78)
        print("CHECKS")
        print("=" * 78)
        for name, ok, detail in self.checks:
            print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail and not ok else ""))
        failed = [c for c in self.checks if not c[1]]
        print(f"\n{len(self.checks) - len(failed)}/{len(self.checks)} checks passed")
        return 1 if failed else 0

    async def run(self) -> int:
        connector = aiohttp.TCPConnector(limit=self.args.concurrency, ttl_dns_cache=300)
        timeout = aiohttp.ClientTimeout(total=self.args.timeout)
        self.session = aiohttp.ClientSession(connector=connector, timeout=timeout)
        try:
            await self.wait_ready()
            show, tokens, hot, cold = await self.setup()
            await self.hot_seat_storm(show["id"], tokens)
            await self.stampede(show["id"], tokens, hot, cold)
            await self.functional_checks(show["id"], uuid.uuid4().hex[:6])
            await self.reconcile(show["id"])
            print(f"\nShow: {self.base}/shows/{show['id']}?seats=false   Metrics: {self.base}/metrics")
            return self.report()
        finally:
            await self.session.close()


@dataclass
class Resp:
    status_code: int
    text: str

    def json(self) -> dict:
        try:
            data = json.loads(self.text)
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _metric(text: str, name: str, **labels) -> float | None:
    for line in text.splitlines():
        if line.startswith(name + "{"):
            got = dict(re.findall(r'(\w+)="([^"]*)"', line[line.index("{"): line.index("}")]))
            if got == labels:
                return float(line.rsplit(" ", 1)[1])
    return None


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("base_url", nargs="?", default="http://localhost:8000")
    p.add_argument("--admin-token", default=os.environ.get("ADMIN_TOKEN", "dev-admin-token"))
    p.add_argument("--requests", type=int, default=20_000, help="stampede size")
    p.add_argument("--concurrency", type=int, default=400, help="max in-flight requests")
    p.add_argument("--users", type=int, default=3_000)
    p.add_argument("--storm", type=int, default=500, help="users racing for the single hot seat")
    p.add_argument("--hot-seats", type=int, default=20)
    p.add_argument("--hot-share", type=float, default=0.8)
    p.add_argument("--rows", type=int, default=20)
    p.add_argument("--seats-per-row", type=int, default=50)
    p.add_argument("--timeout", type=float, default=60.0)
    p.add_argument("--ready-timeout", type=float, default=180.0)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()
    if args.hot_seats < 2 or args.seats_per_row < args.hot_seats:
        p.error("need 2 <= --hot-seats <= --seats-per-row")
    sys.exit(asyncio.run(Burst(args).run()))


if __name__ == "__main__":
    main()
