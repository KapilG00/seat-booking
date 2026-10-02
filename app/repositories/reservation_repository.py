"""Data access for reservations, seat ownership and per-user counters.

Every write here is a *guarded* statement: its WHERE (or ON CONFLICT) clause
encodes the precondition, so Postgres decides atomically and a loser simply
affects 0 rows. None of these methods load a row, check it in Python and write
it back: that read-then-write pattern double-sells under concurrency.

The order these are called in (the lock order that rules out deadlocks) is
the service's job: see app/services/reservation_service.py.

Hot-path statements are built once at import (bind names prefixed b_ because
INSERT/UPDATE reserve column names) and run on a Core connection; the
cancel/read methods use ORM entities and need an AsyncSession.
"""

import uuid

from sqlalchemy import Row, bindparam, func, literal_column, select, update
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models import Reservation, Seat, UserShowCount

_reservations = Reservation.__table__
_seats = Seat.__table__
_counts = UserShowCount.__table__

_FIND_BY_KEY = select(_reservations).where(
    _reservations.c.user_id == bindparam("b_user_id"),
    _reservations.c.idempotency_key == bindparam("b_key"),
)

_PRECHECK = select(
    func.count().label("found"),
    func.array_agg(aggregate_order_by(_seats.c.label, _seats.c.label))
    .filter(_seats.c.status != "available")
    .label("taken"),
    select(_counts.c.held)
    .where(_counts.c.show_id == bindparam("b_show_id"), _counts.c.user_id == bindparam("b_user_id"))
    .scalar_subquery()
    .label("held"),
).where(
    _seats.c.show_id == bindparam("b_show_id"),
    _seats.c.label.in_(bindparam("b_labels", expanding=True)),
)

_CLAIM_KEY = (
    pg_insert(_reservations)
    .values(
        id=bindparam("b_id"),
        show_id=bindparam("b_show_id"),
        user_id=bindparam("b_user_id"),
        seats=bindparam("b_seats"),
        amount_paise=bindparam("b_amount"),
        status="confirmed",
        idempotency_key=bindparam("b_key"),
        request_hash=bindparam("b_hash"),
    )
    .on_conflict_do_nothing(index_elements=["user_id", "idempotency_key"])
    .returning(*_reservations.c)
)

_count_insert = pg_insert(_counts).values(
    show_id=bindparam("b_show_id"), user_id=bindparam("b_user_id"), held=bindparam("b_n")
)
_TAKE_ALLOWANCE = _count_insert.on_conflict_do_update(
    index_elements=["show_id", "user_id"],
    set_={"held": _counts.c.held + _count_insert.excluded.held},
    where=(_counts.c.held + _count_insert.excluded.held) <= bindparam("b_limit"),
).returning(_counts.c.held)

_TAKE_SEAT = (
    update(_seats)
    .where(
        _seats.c.show_id == bindparam("b_show_id"),
        _seats.c.label == bindparam("b_label"),
        _seats.c.status == literal_column("'available'"),
    )
    .values(status="confirmed", reservation_id=bindparam("b_id"), user_id=bindparam("b_user_id"))
)


def row_to_dict(row: Row) -> dict:
    """API shape of a reservation row (same as Reservation.to_dict)."""
    return {
        "reservation_id": row.id,
        "show_id": row.show_id,
        "user_id": row.user_id,
        "seats": list(row.seats),
        "amount_paise": row.amount_paise,
        "status": row.status,
        "created_at": row.created_at,
        "cancelled_at": row.cancelled_at,
    }


class ReservationRepository:
    """Bound to an executor: a Core `AsyncConnection` for the reserve hot path,
    an ORM `AsyncSession` for cancel and reads."""

    def __init__(self, executor: AsyncConnection | AsyncSession) -> None:
        self._db = executor

    # --- reserve hot path (Core) ---------------------------------------------

    async def find_by_key(self, user_id: str, key: str) -> Row | None:
        return (await self._db.execute(_FIND_BY_KEY, {"b_user_id": user_id, "b_key": key})).one_or_none()

    async def precheck(self, show_id: uuid.UUID, user_id: str, labels: list[str]) -> Row:
        """Lock-free read: (found, taken, held) for an early decline."""
        params = {"b_show_id": show_id, "b_user_id": user_id, "b_labels": labels}
        return (await self._db.execute(_PRECHECK, params)).one()

    async def claim_key(
        self,
        *,
        reservation_id: uuid.UUID,
        show_id: uuid.UUID,
        user_id: str,
        seats: list[str],
        amount_paise: int,
        key: str,
        request_hash: str,
    ) -> Row | None:
        """INSERT … ON CONFLICT (user_id, idempotency_key) DO NOTHING RETURNING *.
        None means the key already exists. A concurrent insert of the same key
        blocks on the unique index until the other transaction finishes."""
        params = {
            "b_id": reservation_id,
            "b_show_id": show_id,
            "b_user_id": user_id,
            "b_seats": seats,
            "b_amount": amount_paise,
            "b_key": key,
            "b_hash": request_hash,
        }
        return (await self._db.execute(_CLAIM_KEY, params)).one_or_none()

    async def take_allowance(self, show_id: uuid.UUID, user_id: str, n: int, limit: int) -> int | None:
        """Conditional upsert held += n WHERE held + n <= limit. None = over limit."""
        params = {"b_show_id": show_id, "b_user_id": user_id, "b_n": n, "b_limit": limit}
        return (await self._db.execute(_TAKE_ALLOWANCE, params)).scalar_one_or_none()

    async def take_seat(self, show_id: uuid.UUID, label: str, reservation_id: uuid.UUID, user_id: str) -> bool:
        """Guarded available -> confirmed transition. False = someone else has it."""
        params = {"b_show_id": show_id, "b_label": label, "b_id": reservation_id, "b_user_id": user_id}
        return (await self._db.execute(_TAKE_SEAT, params)).rowcount == 1

    # --- cancel and reads (ORM) ----------------------------------------------

    async def get_owned(self, reservation_id: uuid.UUID, user_id: str) -> Reservation | None:
        """Ownership is part of the WHERE: a non-owner sees "not found"."""
        return await self._db.scalar(
            select(Reservation).where(Reservation.id == reservation_id, Reservation.user_id == user_id)
        )

    async def mark_cancelled(self, reservation_id: uuid.UUID, user_id: str) -> Reservation | None:
        """confirmed -> cancelled, only for the owner. None = not owned / not live."""
        return await self._db.scalar(
            update(Reservation)
            .where(
                Reservation.id == reservation_id,
                Reservation.user_id == user_id,
                Reservation.status == "confirmed",
            )
            .values(status="cancelled", cancelled_at=func.now())
            .returning(Reservation)
        )

    async def release_allowance(self, show_id: uuid.UUID, user_id: str, n: int) -> None:
        await self._db.execute(
            update(UserShowCount)
            .where(UserShowCount.show_id == show_id, UserShowCount.user_id == user_id)
            .values(held=UserShowCount.held - n)
            .execution_options(synchronize_session=False)
        )

    async def release_seat(self, show_id: uuid.UUID, label: str, reservation_id: uuid.UUID) -> None:
        """Frees the seat only if THIS reservation still owns it, so a release
        can never touch a seat that has since been sold to someone else."""
        await self._db.execute(
            update(Seat)
            .where(Seat.show_id == show_id, Seat.label == label, Seat.reservation_id == reservation_id)
            .values(status="available", reservation_id=None, user_id=None)
            .execution_options(synchronize_session=False)
        )
