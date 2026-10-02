# Write-up

## 1. The atomic decision

Postgres decides every seat. No lock exists outside the database. The only in-memory
state is a cache that can **decline** requests and can never sell (see "Fast paths" below).
The schema is SQLAlchemy ORM models plus Alembic migrations. The statements below are
written with SQLAlchemy's Core expression language, and the SQL they emit is shown as
SQL because that's what matters for correctness.
A reserve request is **one transaction** made of three guarded writes, always
taken in the same order. `ReservationService` (`app/services/reservation_service.py`) owns the
order and the transaction, and each step is one `ReservationRepository` method
(`app/repositories/reservation_repository.py`):

1. **Claim the idempotency key**:
   ```sql
   INSERT INTO reservations (...) ON CONFLICT (user_id, idempotency_key) DO NOTHING
   ```
2. **Per-user limit** as a conditional increment on a per-(show, user) counter row:
   ```sql
   INSERT INTO user_show_counts ... ON CONFLICT DO UPDATE SET held = held + n
     WHERE held + n <= per_user_limit RETURNING held
   ```
   No row returned → `per_user_limit`.
3. **Each seat, in sorted label order**:
   ```sql
   UPDATE seats SET status='confirmed', reservation_id=$r, user_id=$u
    WHERE show_id=$s AND label=$l AND status='available'
   ```
   Any `UPDATE 0` → raise → the whole transaction rolls back → `seat_taken`.

**Why it's race-free.** Step 3 is a compare-and-set on the row. When 500
transactions hit `A12`, Postgres row-locks it for the first `UPDATE`. The others
block on that lock. Once the winner commits, each waiter **re-evaluates the
`WHERE` against the committed row** (READ COMMITTED's EvalPlanQual re-check),
sees `status='confirmed'` and updates 0 rows. Nothing is decided by an earlier
read, so a stale read can't cause a double-sell.

There is a second line of defence. The seat row has `CHECK ((status='available') = (reservation_id IS NULL))`,
and one row exists per `(show_id,label)` primary key. So a seat physically cannot belong to two
reservations at once.

**Fast paths (optimisations, not the decision).** Two checks run before the transaction:
1. **Sold-seat cache** (`app/services/sold_seat_cache.py`). If this process already
   knows a requested seat is taken, the request is declined from memory, with no query
   and no pool connection. About 95% of burst traffic is this case.
2. **Lock-free precheck.** Otherwise one `SELECT` declines requests whose seats are
   taken or whose user is at the limit, keeping the hot-seat losers off the row lock.

Neither can cause a double-sell:
- Both can only **decline**. A request they let through still has to win the guarded
  `UPDATE` in the transaction.
- A stale "free" just sends the request on to the transaction, which decides correctly.
- A stale "taken" is bounded:
  - the cache drops a seat the moment its cancel commits;
  - entries expire after a TTL.
- A request whose idempotency key is known always skips the cache, so replays are unaffected.

**Multi-seat and deadlock avoidance.** Partial requests are **all-or-nothing**:
one transaction, so any taken seat rolls back the others. Every transaction locks
in one global order:
- idempotency-key index entry, then
- the user's counter row, then
- seat rows in sorted label order.

Cancel uses the same order: reservation row → counter → sorted seats. With
locks always taken in one order, a wait-for cycle can't form, so deadlocks
can't happen. As belt and braces, `DeadlockDetected` / `SerializationFailure` are
retried up to three times instead of surfacing as errors. The burst's
overlapping-pair requests arrive in shuffled orders and exercise this.

**Never a 5xx for a domain outcome.**
- Declines are typed exceptions mapped to 409/422.
- Infrastructure trouble is a retryable 503: no pool connection within 45 s, DB down,
  or a statement hitting `statement_timeout`.
- A `lock_timeout` while waiting on a seat is reported as `seat_taken`. Another
  in-flight booking owns that seat.

## 2. Idempotency

- **Where it lives.** The key is a column on the reservation itself, with
  `UNIQUE (user_id, idempotency_key)`.
  - Scoping per user means one user's key can never collide with, or replay,
    another user's.
  - The key and the reservation are written in the same transaction, so they
    can't diverge.
- **Exactly once.** Two concurrent requests with one key both run the `INSERT`.
  The second **blocks on the unique index** until the first commits or aborts:
  - If the first committed, the second gets `DO NOTHING` and reads the existing row.
  - If the first aborted (for example, seat taken), the second proceeds as a fresh attempt.
  
  So at most one reservation per key can ever exist.
- **Replay vs conflict.** We store `request_hash = sha256(show_id | sorted seats)`.
  - Same key and same hash: we return the original reservation with **200** and
    `Idempotent-Replayed: true`. 201 stays reserved for "created now", so "exactly
    one 201 per seat" stays true even with retries.
  - Same key and a different hash: **409 `idempotency_key_reused`**.
- **Declined requests don't consume their key.** A decline rolls back, key included,
  so a retry of a declined request is evaluated fresh. Declines aren't money
  movements, so there is nothing to protect. The trade-off: a retry can succeed
  where the original failed, if the seat was freed in between.
- **Replaying after a cancel** returns the original reservation in its current
  state (`cancelled`). It does not re-reserve.
- **Interaction with the sold-seat cache.** The cache remembers which
  `(user_id, key)` pairs it has seen exist, and those requests always go to the
  database.
  - The one gap: a key used *before a process restart* and then reused for a seat sold
    *after* it is declined as `seat_taken` instead of `idempotency_key_reused`.
  - That is still a 409, and never a sale.

## 3. Holds and expiry

The model is **reserve = confirmed immediately, with an owner-only
`POST /reservations/{id}/cancel`**.

Cancel is one transaction:
1. `UPDATE reservations … WHERE id=$r AND user_id=<token sub> AND status='confirmed'`.
   - Ownership is part of the predicate, so a non-owner gets the same 404 as a
     missing reservation.
   - A second cancel is a no-op that returns the cancelled reservation.
2. Decrement the user's counter.
3. Free each seat with `… WHERE reservation_id=$r`.

That last predicate is what makes a release unable to **resurrect a seat
confirmed to someone else**. Once a seat has been rebooked it carries a different
`reservation_id`, so a late or repeated cancel matches 0 rows. Tests and the
burst cover cancel → rebook → repeat cancel.

The schema already has a `held` status and the invariant counts it. Time-boxed
holds would add these pieces:
- `hold_expires_at`.
- A `confirm` endpoint.
- Expiry, applied in two places:
  - lazily, in the guarded `UPDATE` (`status='available' OR (status='held' AND hold_expires_at < now())`);
  - by a periodic sweeper that releases expired holds back to `available`.

## 4. Consistency vs availability under a partition

We choose **consistency**. Postgres is the single source of truth. If the app
can't reach it:
- `/readyz` returns 503, so the platform stops routing traffic to that instance.
- Reserves return 503 `service_unavailable` with `Retry-After`.

We never guess, never sell from a cache (the sold-seat cache can only decline), and
never queue bookings to apply later. A seat sale that might be wrong is worse than a few seconds of "try again".
Liveness (`/healthz`) stays green during a DB outage, so the platform doesn't
restart-loop a healthy process. The pool reconnects by itself when the DB returns.

Scaling out (more app instances) keeps this property, because no instance holds
authoritative state. The limit is one Postgres primary. If it fails over, in-flight
transactions abort (clients see a 503 and retry with the same key, which is safe).
Replicas would only serve reads such as `GET /shows`.

## 5. Observability: what would page me at 2am

- **Any 5xx on `/shows/{id}/reserve`**: `http_requests_total{status=~"5.."}`. Declines are designed to be 4xx.
- **`show_invariant_ok == 0`** for any show. This "should never happen" is the
  double-sell or leaked-seat alarm.
- **`/readyz` failing** (from the platform health check), or `metrics_db_scrape_ok == 0`.
- **Reserve p99 latency** above ~2s sustained: pool saturation, lock queues, or a slow DB.
- **`reservation_txn_retries_total` increasing**: deadlocks shouldn't happen given
  the lock order, so a rise means someone broke the ordering.
- **Dashboards, not pages**:
  - `reservations_declined_total` by reason (a spike in `per_user_limit` means bots);
  - `seats_available` draining;
  - pool `idle` → 0.

Logs carry one JSON line per request with `request_id`, which is echoed in
`X-Request-ID`, so a customer complaint maps to exactly one line.

## 6. AI usage (directed vs decided)
Built with Claude Code (Claude Opus 5.5) in an interactive session.
- **I directed**:
  - the stack (FastAPI, Postgres, uv) and the platform (Render);
  - the reserve-then-cancel model instead of TTL holds;
  - that declined requests must not consume their idempotency key;
  - building in small, reviewable steps, with every commit made by me;
  - when to stop and question things.
- **The AI proposed, and I reviewed**:
  - the schema;
  - the three-step guarded transaction and the global lock order;
  - unique-index-based idempotency;
  - the code, tests, burst script, Dockerfile, compose file and Render blueprint.
- **Found during testing and review**:
  - **Small instances:** at 0.5 CPU (Render Starter) the burst produced 7–11 5xx, and 668
    at 1,000 concurrent requests. Each was a request waiting more than 20 s for a DB
    connection.
    - The fix was the decline-only sold-seat cache plus a 45 s pool wait. 96% of
      `seat_taken` declines are now answered from memory.
    - Result: zero 5xx at 400 and 1,000 concurrent requests, and throughput ~210 → ~360 req/s.
  - **Identity:** `/auth/token` was originally open, so anyone could mint a token for
    another user's name and cancel their seats. It is now admin-only.
  - Introducing the ORM: the first version used ORM sessions everywhere. It
    passed every correctness check but fell from ~985 to ~340 req/s. Profiling
    showed ~29% of request CPU inside SQLAlchemy:
    - the session layer;
    - per-call statement construction;
    - entity loading from `RETURNING`.

    Moving only the reserve hot path to prebuilt Core statements on an
    `AsyncConnection` recovered it to ~570 req/s, with SQLAlchemy at ~14% of
    CPU. The ORM stays for models, migrations, CRUD and cancel.
  - The first burst showed ~75 req/s and multi-second latency. Profiling showed
    the server handling requests in ~26ms on average, so the bottleneck was the
    load-test client (httpx's pool at 400 connections). Switching to aiohttp
    gave ~13× throughput.
  - Docker Desktop's port forwarding on WSL drops connections under load, which
    is why the README says to run the local burst inside the compose network.

## 7. What I'd do next

- **Time-boxed holds** (`held` → `confirmed` / expired), with a sweeper and lazy expiry.
- **Share the sold-seat cache across instances** (Redis, or Postgres `LISTEN/NOTIFY`
  for cancel invalidation). Today each instance has its own cache, and a cancel on one
  instance is only seen by the others when the TTL expires.
- **Payments**: a two-phase `held → paid` flow with the payment-provider
  idempotency key derived from ours.
- **Real auth** (OIDC) instead of the demo token endpoint.
- **Per-user rate limiting** at the edge.
- **PgBouncer** in transaction mode so several app instances can share the
  database's limited connection slots.
- **Prometheus/Grafana**: scrape `/metrics` and alert on the conditions in §5.
- **Durable audit trail**: append-only `reservation_events` for disputes.
