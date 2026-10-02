"""Prometheus metrics.

Counters are in-process (one uvicorn worker), so they reset on restart. Seat
gauges are read from Postgres at scrape time, so they always reconcile with
GET /shows/{id} no matter how many restarts happened.
"""

from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest
from sqlalchemy import func, select

from app.models import SEAT_STATUSES, Seat, Show

DECLINE_REASONS = ("seat_taken", "per_user_limit", "idempotent_replay", "idempotency_key_reused")
# Bound label cardinality: only the most recent shows get seat gauges.
GAUGE_SHOW_LIMIT = 50

RESERVATIONS_CONFIRMED = Counter(
    "reservations_confirmed", "Reservations created (HTTP 201)", ["show_id"]
)
SEATS_CONFIRMED = Counter("seats_confirmed", "Seats confirmed by new reservations", ["show_id"])
RESERVATIONS_DECLINED = Counter(
    "reservations_declined", "Reserve requests that did not create a reservation", ["show_id", "reason"]
)
RESERVATIONS_CANCELLED = Counter(
    "reservations_cancelled", "Reservations cancelled by their owner", ["show_id"]
)
RESERVATION_TXN_RETRIES = Counter(
    "reservation_txn_retries", "Reserve/cancel transactions retried after a transient DB error", ["error"]
)
SOLD_SEAT_CACHE_DECLINES = Counter(
    "sold_seat_cache_declines", "seat_taken declines answered from memory without a DB query"
)
HTTP_REQUESTS = Counter("http_requests", "HTTP requests", ["method", "route", "status"])
HTTP_LATENCY = Histogram(
    "http_request_duration_seconds",
    "HTTP request latency",
    ["method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 20),
)
SHOW_SEATS = Gauge("show_seats", "Seats per show by status (from DB at scrape)", ["show_id", "status"])
SEATS_AVAILABLE = Gauge("seats_available", "Available seats per show (from DB at scrape)", ["show_id"])
SHOW_INVARIANT_OK = Gauge(
    "show_invariant_ok", "1 if available+held+confirmed == total_seats (from DB at scrape)", ["show_id"]
)
DB_POOL_CONNECTIONS = Gauge("db_pool_connections", "DB connection pool (size / checked_out)", ["state"])
METRICS_DB_SCRAPE_OK = Gauge("metrics_db_scrape_ok", "1 if the last scrape read seat gauges from the DB")


def init_show_labels(show_id: str) -> None:
    """Pre-create label sets so a fresh show exports zeros instead of nothing."""
    RESERVATIONS_CONFIRMED.labels(show_id)
    SEATS_CONFIRMED.labels(show_id)
    RESERVATIONS_CANCELLED.labels(show_id)
    for reason in DECLINE_REASONS:
        RESERVATIONS_DECLINED.labels(show_id, reason)


async def refresh_db_gauges(db) -> None:
    """`db` is app.db.database.Database (duck-typed to keep this module light)."""
    if not db.ready:
        METRICS_DB_SCRAPE_OK.set(0)
        return
    pool = db.pool_status()
    DB_POOL_CONNECTIONS.labels("size").set(pool["size"])
    DB_POOL_CONNECTIONS.labels("checked_out").set(pool["checked_out"])

    recent = (
        select(Show.id, Show.total_seats)
        .order_by(Show.created_at.desc())
        .limit(GAUGE_SHOW_LIMIT)
        .cte("recent")
    )
    by_status = {
        status: func.count().filter(Seat.status == status).label(status) for status in SEAT_STATUSES
    }
    # One statement = one snapshot, so the counts are mutually consistent.
    stmt = (
        select(recent.c.id, recent.c.total_seats, *by_status.values())
        .join(Seat, Seat.show_id == recent.c.id)
        .group_by(recent.c.id, recent.c.total_seats)
    )
    try:
        async with db.read_session() as session:
            rows = (await session.execute(stmt)).mappings().all()
    except Exception:
        METRICS_DB_SCRAPE_OK.set(0)
        return
    SHOW_SEATS.clear()
    SEATS_AVAILABLE.clear()
    SHOW_INVARIANT_OK.clear()
    for row in rows:
        show_id = str(row["id"])
        for status in SEAT_STATUSES:
            SHOW_SEATS.labels(show_id, status).set(row[status])
        SEATS_AVAILABLE.labels(show_id).set(row["available"])
        total = row["available"] + row["held"] + row["confirmed"]
        SHOW_INVARIANT_OK.labels(show_id).set(1 if total == row["total_seats"] else 0)
    METRICS_DB_SCRAPE_OK.set(1)


def render_latest() -> tuple[bytes, str]:
    return generate_latest(), CONTENT_TYPE_LATEST
