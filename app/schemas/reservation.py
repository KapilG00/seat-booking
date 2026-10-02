from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.schemas.common import SeatLabel, reject_duplicates


class ReserveRequest(BaseModel):
    # Unknown fields (e.g. a spoofed "user_id") are ignored: identity is the token's.
    seats: list[SeatLabel] = Field(min_length=1, max_length=100)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)

    _unique = field_validator("seats")(reject_duplicates)


class ReservationOut(BaseModel):
    reservation_id: UUID
    show_id: UUID
    user_id: str
    seats: list[str]
    amount_paise: int
    status: Literal["confirmed", "cancelled"]
    created_at: datetime
    cancelled_at: datetime | None = None
