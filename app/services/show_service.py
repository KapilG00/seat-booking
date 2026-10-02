import uuid
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncConnection

from app.core.config import Settings
from app.core.errors import InvalidRequest, NotFound
from app.db.database import Database
from app.models import Show
from app.repositories import ShowRepository


@dataclass(frozen=True)
class ShowInfo:
    id: uuid.UUID
    price_paise: int
    per_user_limit: int


class ShowService:
    """Create and read shows. One instance per app (see app/main.py)."""

    def __init__(self, db: Database, settings: Settings) -> None:
        self._db = db
        self._settings = settings
        # Shows are immutable once created, so caching price/limit is safe and
        # saves a round trip on every reserve during the burst.
        self._info_cache: dict[uuid.UUID, ShowInfo] = {}

    async def create(
        self, *, name: str, seats: list[str], price_paise: int, per_user_limit: int | None
    ) -> dict:
        if len(seats) > self._settings.max_seats_per_show:
            raise InvalidRequest(f"a show may have at most {self._settings.max_seats_per_show} seats")
        show = Show(
            id=uuid.uuid4(),
            name=name,
            price_paise=price_paise,
            per_user_limit=per_user_limit or self._settings.default_per_user_limit,
            total_seats=len(seats),
        )
        async with self._db.session() as session, session.begin():
            await ShowRepository(session).add(show, seats)
        return await self.get(show.id, include_seats=True)

    async def get(self, show_id: uuid.UUID, include_seats: bool) -> dict:
        async with self._db.session() as session:
            # REPEATABLE READ: the show row, seat list and counts all come from
            # one snapshot, so the counts we return always add up to what we list.
            await session.connection(
                execution_options={"isolation_level": "REPEATABLE READ", "postgresql_readonly": True}
            )
            repo = ShowRepository(session)
            show = await repo.get(show_id)
            if show is None:
                raise NotFound("show not found", show_id=str(show_id))
            counts = await repo.seat_counts(show_id)
            seats = await repo.seat_states(show_id) if include_seats else None
        return {
            "id": show.id,
            "name": show.name,
            "price_paise": show.price_paise,
            "per_user_limit": show.per_user_limit,
            "total_seats": show.total_seats,
            "created_at": show.created_at,
            "counts": counts,
            "invariant_ok": sum(counts.values()) == show.total_seats,
            "seats": seats,
        }

    def cached_info(self, show_id: uuid.UUID) -> ShowInfo | None:
        """Price/limit if already cached; never touches the database."""
        return self._info_cache.get(show_id)

    async def info(self, conn: AsyncConnection, show_id: uuid.UUID) -> ShowInfo:
        """Price and limit for the reserve hot path (cached; uses the caller's connection)."""
        info = self._info_cache.get(show_id)
        if info is None:
            row = await ShowRepository(conn).get_info(show_id)
            if row is None:
                raise NotFound("show not found", show_id=str(show_id))
            info = ShowInfo(row.id, row.price_paise, row.per_user_limit)
            self._info_cache[show_id] = info
        return info
