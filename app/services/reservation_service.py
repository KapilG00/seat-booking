"""The atomic seat decision.

Reserve is one transaction that takes locks in a fixed global order:

    1. reservations (user_id, idempotency_key) unique-index entry
    2. user_show_counts row for (show, user)
    3. seat rows, in sorted label order

Cancel takes the same order (reservation row -> counter -> sorted seats).
Every transaction acquires locks in the same order, so no wait-for cycle,
hence no deadlock, can form. Each step is a *conditional* write guarded on
the current state (see app/repositories/reservation_repository.py), never
read-then-write, so a race has exactly one winner.

ORM note: the tables come from app.models, but contended rows are never
written through ORM objects. Loading a Seat, checking `status` in Python and
saving it back is read-then-write and would double-sell; and the unit of
work's flush order is not ours to control, while the lock order above must be.

The reserve hot path uses Core connections and prebuilt statements: under the
burst the ORM Session layer and per-call statement construction cost ~3x
throughput. Cancel and reads are not hot, so they use ordinary ORM sessions.

Before any of that, requests for seats this process already knows are taken
are declined from memory (app/services/sold_seat_cache.py), keeping the ~95%
of burst traffic that can't win off the database. The cache can only decline;
the database still decides every sale.
"""

import hashlib
import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import Row
from sqlalchemy.exc import DBAPIError

from app.core.errors import (
    IdempotencyKeyReused,
    InvalidRequest,
    NotFound,
    PerUserLimitExceeded,
    SeatTaken,
)
from app.db.database import LOCK_NOT_AVAILABLE, RETRYABLE_SQLSTATES, Database, sqlstate
from app.observability.metrics import RESERVATION_TXN_RETRIES, SOLD_SEAT_CACHE_DECLINES
from app.repositories import ReservationRepository
from app.repositories.reservation_repository import row_to_dict
from app.services.show_service import ShowInfo, ShowService
from app.services.sold_seat_cache import SoldSeatCache

logger = structlog.get_logger(__name__)

# Transient errors worth retrying; with a fixed lock order these should not
# occur, but if one does the client must still get a domain answer, not a 500.
_MAX_ATTEMPTS = 3


@dataclass
class ReserveResult:
    reservation: dict
    created: bool  # False => idempotent replay of an earlier request


class _KeyAlreadyUsed(Exception):
    """Internal: the idempotency key exists; resolve to replay or conflict."""


def request_hash(show_id: uuid.UUID, labels: list[str]) -> str:
    return hashlib.sha256(f"{show_id}|{','.join(sorted(labels))}".encode()).hexdigest()


def _limit_error(show: ShowInfo) -> PerUserLimitExceeded:
    return PerUserLimitExceeded(
        f"at most {show.per_user_limit} seats per user for this show",
        per_user_limit=show.per_user_limit,
    )


def _check_static(show: ShowInfo, labels: list[str]) -> None:
    """Checks that need only the show itself (no seat or user state)."""
    if not show.seat_labels.issuperset(labels):
        raise InvalidRequest("one or more seats do not exist for this show")
    if len(labels) > show.per_user_limit:
        raise _limit_error(show)


def _replay_or_conflict(existing: Row, req_hash: str) -> "ReserveResult":
    if existing.request_hash != req_hash:
        raise IdempotencyKeyReused(
            "idempotency key was already used with a different request",
            reservation_id=str(existing.id),
        )
    return ReserveResult(row_to_dict(existing), created=False)


def _is_retryable(exc: DBAPIError) -> bool:
    return sqlstate(exc) in RETRYABLE_SQLSTATES


class ReservationService:
    """Reserve, cancel and read reservations. One instance per app."""

    def __init__(self, db: Database, shows: ShowService, cache: SoldSeatCache | None = None) -> None:
        self._db = db
        self._shows = shows
        self._cache = cache

    async def reserve(
        self, show_id: uuid.UUID, user_id: str, seats: list[str], key: str
    ) -> ReserveResult:
        """All-or-nothing reservation of `seats` for `user_id`, idempotent on `key`."""
        labels = sorted(seats)  # sorted = the global seat lock order
        req_hash = request_hash(show_id, labels)

        # Fast decline from memory: no connection, no query. Skipped when this
        # key is known to exist, so replays/key-reuse are resolved by the DB.
        # Applies the same checks, in the same order, as _precheck, so the
        # answer never depends on whether the cache was warm.
        if self._cache is not None and not self._cache.knows_key(user_id, key):
            show = self._shows.cached_info(show_id)
            if show is not None:
                _check_static(show, labels)
                taken = self._cache.taken_by_others(show_id, labels, user_id)
                if taken:
                    SOLD_SEAT_CACHE_DECLINES.inc()
                    raise SeatTaken("seat already taken", seats=taken)

        async with self._db.read_connect() as conn:
            show = await self._shows.info(conn, show_id)
            _check_static(show, labels)
            repo = ReservationRepository(conn)
            # Fence for the cache: a cancel committing after this point must
            # not be undone by us caching what we are about to read.
            epoch = self._cache.epoch() if self._cache is not None else 0
            row = await repo.precheck(show.id, user_id, labels)
            try:
                self._precheck(row, show, labels)
            except (SeatTaken, PerUserLimitExceeded):
                # A retry of an already-successful request looks "taken"/"over
                # limit" to the precheck (it took the seats itself), so check
                # the key before declining.
                existing = await repo.find_by_key(user_id, key)
                if existing is not None:
                    self._remember_key(user_id, key)
                    return _replay_or_conflict(existing, req_hash)
                if row.taken:
                    self._mark_taken(show_id, dict(zip(row.taken, row.taken_owners)), since=epoch)
                raise

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._db.connect() as conn, conn.begin():
                    reservation = await self._reserve_txn(
                        ReservationRepository(conn), show, user_id, labels, key, req_hash
                    )
                # Committed: remember the key and cache the seats as ours.
                self._remember_key(user_id, key)
                self._mark_taken(show_id, dict.fromkeys(labels, user_id))
                return ReserveResult(reservation, created=True)
            except _KeyAlreadyUsed:
                async with self._db.read_connect() as conn:
                    existing = await ReservationRepository(conn).find_by_key(user_id, key)
                if existing is None:  # the other request rolled back after all; try again
                    continue
                self._remember_key(user_id, key)
                return _replay_or_conflict(existing, req_hash)
            except DBAPIError as exc:
                if not _is_retryable(exc):
                    raise
                RESERVATION_TXN_RETRIES.labels(sqlstate(exc)).inc()
                logger.warning("reserve_txn_retry", attempt=attempt, sqlstate=sqlstate(exc))
                if attempt == _MAX_ATTEMPTS:
                    raise
        raise SeatTaken("seat is being booked by someone else", seats=labels)

    @staticmethod
    def _precheck(row: Row, show: ShowInfo, labels: list[str]) -> None:
        """Cheap, lock-free early decline from `repo.precheck`. NOT the
        decision: the transaction re-checks everything with guarded writes, so
        a stale read here can only cause a decline that would have happened
        anyway, never a double-sell.

        Order (shared with the in-memory path): unknown seat (422) -> request
        bigger than the limit -> seat taken -> limit given what's already held."""
        if row.found != len(labels):
            raise InvalidRequest("one or more seats do not exist for this show")
        if row.taken:
            raise SeatTaken("seat already taken", seats=list(row.taken))
        if (row.held or 0) + len(labels) > show.per_user_limit:
            raise _limit_error(show)

    @staticmethod
    async def _reserve_txn(
        repo: ReservationRepository,
        show: ShowInfo,
        user_id: str,
        labels: list[str],
        key: str,
        req_hash: str,
    ) -> dict:
        """Runs inside a transaction: any exception rolls everything back."""
        reservation_id = uuid.uuid4()

        # (1) Claim the idempotency key. A concurrent request with the same key
        #     blocks on the unique index until we commit or roll back, so only
        #     one of them can ever proceed past this line.
        reservation = await repo.claim_key(
            reservation_id=reservation_id,
            show_id=show.id,
            user_id=user_id,
            seats=labels,
            amount_paise=show.price_paise * len(labels),
            key=key,
            request_hash=req_hash,
        )
        if reservation is None:
            raise _KeyAlreadyUsed

        # (2) Per-user limit as a conditional increment. The row lock on
        #     (show, user) serialises one user's parallel requests.
        if await repo.take_allowance(show.id, user_id, len(labels), show.per_user_limit) is None:
            raise _limit_error(show)

        # (3) Seats, in sorted order, each a guarded state transition. If a
        #     competitor holds the row lock we wait, then Postgres re-evaluates
        #     the WHERE against the committed row: the loser updates 0 rows.
        #     Any miss rolls back the whole reservation (all-or-nothing).
        for label in labels:
            try:
                taken = await repo.take_seat(show.id, label, reservation_id, user_id)
            except DBAPIError as exc:
                if sqlstate(exc) == LOCK_NOT_AVAILABLE:
                    # Held by an in-flight booking for longer than lock_timeout.
                    raise SeatTaken("seat is being booked by someone else", seats=[label]) from None
                raise
            if not taken:
                raise SeatTaken("seat already taken", seats=[label])
        return row_to_dict(reservation)

    async def cancel(self, reservation_id: uuid.UUID, user_id: str) -> tuple[dict, bool]:
        """Owner-only release. Returns (reservation, changed). Cancelling an
        already-cancelled reservation is a no-op that returns it unchanged."""
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                async with self._db.session() as session, session.begin():
                    repo = ReservationRepository(session)
                    # (1) Lock the reservation, only if this user owns it and
                    #     it is still live.
                    reservation = await repo.mark_cancelled(reservation_id, user_id)
                    if reservation is None:
                        existing = await repo.get_owned(reservation_id, user_id)
                        if existing is None:
                            raise NotFound("reservation not found")
                        return existing.to_dict(), False
                    # (2) Give the seats back to the user's allowance.
                    await repo.release_allowance(reservation.show_id, user_id, len(reservation.seats))
                    # (3) Free only seats still owned by THIS reservation, sorted.
                    for label in sorted(reservation.seats):
                        await repo.release_seat(reservation.show_id, label, reservation_id)
                    released = reservation.to_dict()
                # Committed: the seats are free again, forget them immediately.
                if self._cache is not None:
                    self._cache.release(released["show_id"], released["seats"])
                return released, True
            except DBAPIError as exc:
                if not _is_retryable(exc) or attempt == _MAX_ATTEMPTS:
                    raise
                RESERVATION_TXN_RETRIES.labels(sqlstate(exc)).inc()
        raise AssertionError("unreachable")

    def _remember_key(self, user_id: str, key: str) -> None:
        if self._cache is not None:
            self._cache.remember_key(user_id, key)

    def _mark_taken(self, show_id: uuid.UUID, owners: dict[str, str], since: int | None = None) -> None:
        if self._cache is not None:
            self._cache.mark_taken(show_id, owners, since=since)

    async def get(self, reservation_id: uuid.UUID, user_id: str) -> dict:
        async with self._db.read_session() as session:
            reservation = await ReservationRepository(session).get_owned(reservation_id, user_id)
        if reservation is None:
            raise NotFound("reservation not found")
        return reservation.to_dict()
