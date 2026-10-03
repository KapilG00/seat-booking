# seat-booking

A JSON API that sells assigned seats for a show and stays correct under an
on-sale stampede: no seat is ever sold twice, no user exceeds their limit, and
a retried request never reserves twice.

- **Live URL:** `https://seat-booking-api-2lwj.onrender.com`
- **Design write-up:** [WRITEUP.md](WRITEUP.md)

## Testing the live service

1. You need the **admin token**. It is shared with the submission, not stored in git.
   It is required to create a show and to mint user tokens.
2. Create a fresh show with `POST /shows` (admin).
3. Mint one token per simulated user with `POST /auth/token {"username": "u1"}` (admin).
   Each user token's `sub` is that user's identity.
4. Reserve with the user tokens. Or run the whole stampede plus checks in one command:
   ```bash
   make burst URL=https://seat-booking-api-2lwj.onrender.com ADMIN_TOKEN=<admin token>
   ```

## API

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `POST` | `/auth/token` | admin | Mint a user token: `{"username": "alice"}` → bearer token (identity = token `sub`) |
| `POST` | `/shows` | admin | `{"name", "seats": ["A1",...], "price_paise", "per_user_limit"?}` → show, all seats available |
| `GET` | `/shows/{id}` | none | Per-seat status + counts + `invariant_ok`. `?seats=false` for counts only |
| `POST` | `/shows/{id}/reserve` | user | `{"seats": ["A12"], "idempotency_key": "…"}` (or `Idempotency-Key` header) |
| `POST` | `/reservations/{id}/cancel` | owner | Releases the seats; repeat cancel is a no-op |
| `GET` | `/reservations/{id}` | owner | Fetch own reservation |
| `GET` | `/healthz` | none | Liveness (process up; no dependency checks) |
| `GET` | `/readyz` | none | Readiness: `SELECT 1` within 1s on a dedicated probe pool, else **503** (fails closed) |
| `GET` | `/metrics` | none | Prometheus metrics |
| `GET` | `/docs` | none | Interactive OpenAPI docs |

Admin endpoints take `Authorization: Bearer $ADMIN_TOKEN`.

`/auth/token` stands in for a real identity provider and is **admin-only**. If it
were open, anyone could mint a token for someone else's username and cancel that
user's seats.

### Reserve outcomes

| Status | `error` | Meaning |
|---|---|---|
| 201 | – | Reservation created: `{reservation_id, show_id, user_id, seats, amount_paise, status: "confirmed"}` |
| 200 | – | Idempotent replay: same key + same body → the original reservation (`Idempotent-Replayed: true`) |
| 409 | `seat_taken` | At least one seat is held/confirmed by someone else (lists which) |
| 409 | `per_user_limit` | Would exceed the show's `per_user_limit` (default 4) |
| 409 | `idempotency_key_reused` | Same key used earlier with different seats/show |
| 422 | `invalid_request` | Malformed body, unknown seat, missing idempotency key |
| 401 / 404 | | Missing/invalid token · unknown show, or reservation not yours |
| 503 | `service_unavailable` | DB unreachable / overloaded; safe to retry (`Retry-After: 1`) |

**Partial requests are all-or-nothing**: `["A12","A13"]` with A13 taken confirms
neither and returns 409 listing `A13`. Money is integer paise throughout; floats
and strings are rejected for `price_paise`.

When several declines apply, the first in this order wins (the same with or
without the sold-seat cache): unknown seat (422) → more seats than
`per_user_limit` in one request → `seat_taken` → `per_user_limit` given the
seats the user already holds.

Idempotency keys are scoped per user, across all shows: reusing a key on a
different show is `idempotency_key_reused`. Replaying the key of a reservation
that was since cancelled returns it as it is now (200, `status: "cancelled"`);
it does not re-reserve.

Seats go straight from `available` to `confirmed` (the release model is explicit
cancel, not timed holds), so `counts.held` is always 0. It is kept in the
response and the invariant so time-boxed holds can be added without changing the API.

Example:

```bash
BASE=http://localhost:8000
SHOW=$(curl -s $BASE/shows -H "Authorization: Bearer dev-admin-token" -H 'content-type: application/json' \
  -d '{"name":"friday-night","seats":["A11","A12","A13"],"price_paise":25000}' | jq -r .id)
TOKEN=$(curl -s $BASE/auth/token -H "Authorization: Bearer dev-admin-token" -H 'content-type: application/json' \
  -d '{"username":"alice"}' | jq -r .access_token)
curl -s $BASE/shows/$SHOW/reserve -H "Authorization: Bearer $TOKEN" -H 'content-type: application/json' \
  -H 'Idempotency-Key: order-1' -d '{"seats":["A12"]}'
curl -s "$BASE/shows/$SHOW?seats=false"
```

## Run locally

Prerequisites: Docker (Compose v2) and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up --build        # app + Postgres 16, exactly as deployed → http://localhost:8000
```

For development with autoreload:

```bash
cp .env.example .env
make db                          # Postgres only
make dev                         # uv run uvicorn app.main:app --reload
make test                        # pytest against the local Postgres (race tests included)
```

## Database and migrations

The schema is defined once, as SQLAlchemy 2.0 models in `app/models/` (one class per file), and
versioned with **Alembic** (`app/db/migrations/versions/`).
- The app runs `alembic upgrade head` on startup, under a Postgres advisory lock, so
  several instances booting at once are safe.
- `/readyz` only turns green once the schema is current.

```bash
# 1. edit a model in app/models/ (new model? also export it from app/models/__init__.py)
make revision m="add venues"    # autogenerate a migration, then REVIEW the file
make migrate                    # apply it (or just restart the app)
make db-check                   # fails if models and DB differ (also a pytest test)
```

How the ORM is used:
- **ORM sessions** handle CRUD and reads: create/get show, get reservation, cancel.
- **The reserve hot path** uses SQLAlchemy **Core** statements over the same model tables.
  - They are built once at import and executed on an `AsyncConnection`.
  - Every contended write is a guarded statement, never "load object → check →
    save", which would double-sell.
  - Moving this path off the ORM session took burst throughput from ~340 to ~570 req/s.
  - See the docstrings of `app/services/reservation_service.py` and
    `app/repositories/reservation_repository.py`.

## Sold-seat cache (performance under a burst)

During an on-sale burst ~95% of reserve requests are for seats that are already sold.
`SoldSeatCache` (`app/services/sold_seat_cache.py`) answers those `seat_taken` declines
from memory, with no database query and no pool connection.

- **It can only decline.** Every sale is still decided by the guarded `UPDATE` in Postgres.
- **It knows each seat's owner and never declines the owner.** A user asking for a seat
  they hold may be retrying, so that request goes to the database and gets its 200 replay.
  Keys this process has seen also go to the database, so a reused key still gets
  `idempotency_key_reused`.
- **Cancelled seats are rebookable immediately.** A cancel removes its seats as soon as
  it commits, and a request that read the seat as taken *before* the cancel can't
  put it back afterwards (release fencing).
- **Same answers as the database path.** It rejects unknown seats and over-limit
  requests exactly like the database precheck does.
- **Entries expire after `SOLD_SEAT_CACHE_TTL_SECONDS` (30 s).** This bounds staleness if
  the service is ever scaled to several instances.
- **It can be switched off** with `SOLD_SEAT_CACHE_ENABLED=false`.

At 0.5 CPU it took the burst from 7–11 5xx (and 668 at 1,000 concurrent requests) to
**zero**, raising throughput from ~210 to ~360 req/s. 96% of `seat_taken` declines
were answered from memory.

## One-command burst

```bash
make burst URL=https://seat-booking-api-2lwj.onrender.com ADMIN_TOKEN=<admin token>
# or
ADMIN_TOKEN=<admin token> ./burst.sh https://seat-booking-api-2lwj.onrender.com
# locally (dev admin token is the default)
make burst
```

`scripts/burst.py` is a standalone uv script (dependencies declared inline), so
it needs nothing but `uv`. Against a **fresh show** it runs:

1. **Hot-seat storm**: 500 users released at the same instant on seat `A12`.
   The script checks for exactly one 201, with every loser getting 409 `seat_taken`.
2. **Stampede**: 20,000 reserve requests at concurrency 400. 80% aim at 20 hot seats,
   and ~5% are concurrent same-key retries. `/shows/{id}` is polled during the run to
   check the invariant *while* it happens.
3. **Functional checks**:
   - 20 concurrent same-key requests: one 201 and 19 replays.
   - Same key with different seats: 409.
   - 10 parallel reserves on a limit-4 show: exactly 4 confirmed.
   - A spoofed body `user_id` is ignored.
   - Another user's cancel gets 404.
   - Cancel → rebook → repeat cancel never resurrects the seat.
4. **Reconciliation**: `GET /shows/{id}` counts vs the 201s actually observed, and
   `/metrics` vs the API.

It prints the outcome distribution (confirmed / declined by reason / 5xx /
transport errors, p50/p95/p99) and PASS/FAIL per check, exiting non-zero on any
failure. Tune with `ARGS="--requests 5000 --concurrency 200"`; see `--help`.

Measured locally: the production image with one uvicorn worker and a fresh Postgres 16
database, with the burst run inside the compose network. The container was limited to
**0.5 CPU / 512 MB** (Render Starter size) with `--cpus=0.5 --memory=512m`.

| Concurrent requests | Throughput | p50 / p95 / p99 | 5xx | Checks |
|---|---|---|---|---|
| 400 (default), run 1 | 361 req/s | 1.0 s / 1.3 s / 5.5 s | 0 | 20/20 |
| 400 (default), run 2 | 363 req/s | 1.0 s / 1.3 s / 3.5 s | 0 | 20/20 |
| 1,000 | 343 req/s | 2.8 s / 3.2 s / 6.6 s | 0 | 20/20 |

Peak memory was ~115 MB. At 0.1 CPU (Render free) the same burst runs at ~43 req/s and
produces 5xx, so use **Starter or larger** for a 20k burst.

> **WSL/Docker Desktop note:** Docker Desktop's localhost port forwarding drops
> connections at a few hundred concurrent sockets. For a local burst against
> the container, run the script inside the compose network:
> `docker run --rm --network seat-booking_default -v "$PWD/scripts:/s:ro" ghcr.io/astral-sh/uv:python3.12-bookworm-slim uv run /s/burst.py http://app:8000`

## Deploy (Render)

1. Push this repo to GitHub.
2. Render → **New → Blueprint** → select the repo. `render.yaml` creates:
   - `seat-booking-db`: Postgres 16.
   - `seat-booking-api`: built from the `Dockerfile`, with health check `/readyz`.
     `DATABASE_URL` is wired from the database, and `JWT_SECRET` is generated.
3. Set `ADMIN_TOKEN` in the service's Environment tab. It is deliberately not in
   git. The app refuses to start in production with the dev secrets.
4. Wait for the deploy to go live, then:
   ```bash
   curl https://seat-booking-api-2lwj.onrender.com/readyz
   make burst URL=https://seat-booking-api-2lwj.onrender.com ADMIN_TOKEN=...
   ```

Every push to `main` redeploys.

**Instance size matters for the burst:**
- **Free (0.1 CPU):** fine for trying the API, but too slow for 20k requests (5xx).
  It also sleeps after ~15 min idle, and the first request then takes 30–60 s.
- **Starter (0.5 CPU):** passes the 20k burst with zero 5xx (measured above) and doesn't sleep.

The burst script waits for `/readyz` before starting.

## Observability

- **Metrics** (`/metrics`):

  | Metric | Type | Notes |
  |---|---|---|
  | `reservations_confirmed_total{show_id}` | counter | |
  | `seats_confirmed_total{show_id}` | counter | |
  | `reservations_declined_total{show_id,reason}` | counter | `seat_taken` / `per_user_limit` / `idempotent_replay` / `idempotency_key_reused` |
  | `reservations_cancelled_total{show_id}` | counter | |
  | `seats_available{show_id}` | gauge | |
  | `show_seats{show_id,status}` | gauge | |
  | `show_invariant_ok{show_id}` | gauge | |
  | `http_requests_total{method,route,status}` | counter | |
  | `http_request_duration_seconds` | histogram | |
  | `db_pool_connections{state}` | gauge | |
  | `reservation_txn_retries_total` | counter | |
  | `sold_seat_cache_declines_total` | counter | `seat_taken` declines answered from memory |

  The seat gauges are read from Postgres at scrape time, so they always match
  `GET /shows/{id}`. That read, like `/readyz`, uses a separate 2-connection probe
  pool, so scrapes neither wait for nor take request connections during a burst.
  Counters are per process and reset on restart.
- **Logs**: one JSON line per request on stdout, containing:
  - `request_id`: your `X-Request-ID` if sent, otherwise generated, and always echoed back in the response.
  - `route`, `status`, `duration_ms`.
  - For reserves: `show_id`, `user_id`, `seats`, `outcome`, `reason` and `reservation_id`.

  On Render, view them under the service's **Logs** tab.

## Configuration

All settings are environment variables. See [.env.example](.env.example). Key ones:

| Variable | Notes |
|---|---|
| `DATABASE_URL` | |
| `ADMIN_TOKEN` | |
| `JWT_SECRET` | |
| `DB_POOL_MAX_SIZE` | Default 10 |
| `DB_POOL_ACQUIRE_TIMEOUT` | Default 45 s; how long a request waits for a DB connection before a 503 |
| `DB_LOCK_TIMEOUT_MS` | |
| `DB_STATEMENT_TIMEOUT_MS` | |
| `DB_PROBE_STATEMENT_TIMEOUT_MS` | Default 5000; statement timeout for `/readyz` and the metrics seat gauges |
| `ACCESS_LOG` | |
| `SOLD_SEAT_CACHE_ENABLED` | Default `true` |
| `SOLD_SEAT_CACHE_TTL_SECONDS` | Default 30 |

## Layout

```
app/
  main.py                   app factory: settings, logging, lifespan (DB + services), router
  api/                      HTTP layer only (no SQL)
    routes/                 health, metrics, auth, shows, reservations
    deps.py                 dependencies: settings, services, current_user, require_admin
    error_handlers.py       exceptions → 4xx / 503 (never an unhandled 500)
    router.py               mounts all routes
  services/                 business rules + transaction boundaries (classes)
    reservation_service.py  ReservationService: the atomic reserve/cancel  ← start here
    show_service.py         ShowService: create/read shows, price/limit cache
    sold_seat_cache.py      SoldSeatCache: decline-only memory of sold seats
  repositories/             all SQL, one method per statement (classes)
    reservation_repository.py  guarded writes: claim key, take allowance, take seat, …
    show_repository.py      show + seat queries
  models/                   SQLAlchemy ORM models, one class per file
    show.py  seat.py  reservation.py  user_show_count.py
  schemas/                  Pydantic request/response models, per area
  db/                       base.py (declarative Base), database.py (engine, sessions),
                            migrations/ (Alembic env + versions, applied on startup)
  core/                     config.py, logging.py, errors.py, security.py (JWT)
  observability/            metrics.py (Prometheus), middleware.py (request id, access log)
scripts/burst.py            on-sale stampede + checks
tests/                      API tests incl. real concurrency against Postgres

Dependencies point one way: api → services → repositories → models/db.
A request flows route → service (rules, transaction, lock order) → repository
(one guarded statement each) → Postgres.
```
