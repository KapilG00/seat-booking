import uuid

from sqlalchemy import Row, bindparam, func, insert, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.models import Seat, Show

# Hot path (every reserve looks up price/limit): built once, bound per call.
_SHOW_INFO = select(Show.id, Show.price_paise, Show.per_user_limit).where(
    Show.id == bindparam("show_id")
)


class ShowRepository:
    """Data access for shows and their seats. One method = one statement.

    Bound to an executor: an ORM `AsyncSession` (CRUD) or, for the hot-path
    lookup, a Core `AsyncConnection`.
    """

    def __init__(self, executor: AsyncSession | AsyncConnection) -> None:
        self._db = executor

    async def get_info(self, show_id: uuid.UUID) -> Row | None:
        """(id, price_paise, per_user_limit). Works on a connection or session."""
        return (await self._db.execute(_SHOW_INFO, {"show_id": show_id})).one_or_none()

    async def add(self, show: Show, seat_labels: list[str]) -> None:
        """Insert the show and all its seats (requires a session in a transaction)."""
        self._db.add(show)
        await self._db.flush()  # the show row must exist before its seats (FK)
        # ORM bulk insert: batched multi-row INSERTs instead of one object per seat.
        # position keeps the admin's seat order for display (A2 before A10).
        await self._db.execute(
            insert(Seat),
            [{"show_id": show.id, "label": label, "position": i} for i, label in enumerate(seat_labels, 1)],
        )

    async def get(self, show_id: uuid.UUID) -> Show | None:
        return await self._db.get(Show, show_id)

    async def seat_counts(self, show_id: uuid.UUID) -> dict[str, int]:
        counts = {"available": 0, "held": 0, "confirmed": 0}
        rows = await self._db.execute(
            select(Seat.status, func.count()).where(Seat.show_id == show_id).group_by(Seat.status)
        )
        for status, n in rows:
            counts[status] = n
        return counts

    async def seat_states(self, show_id: uuid.UUID) -> list[dict]:
        rows = await self._db.execute(
            select(Seat.label, Seat.status).where(Seat.show_id == show_id).order_by(Seat.position)
        )
        return [{"label": label, "status": status} for label, status in rows]
