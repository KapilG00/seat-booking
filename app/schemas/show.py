from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, StrictInt, field_validator

from app.schemas.common import SeatLabel, SeatStatus, reject_duplicates


class ShowCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    seats: list[SeatLabel] = Field(min_length=1)
    # StrictInt: money is integer paise; 250.0 or "250" are rejected, not coerced.
    price_paise: StrictInt = Field(gt=0, le=10**12)
    per_user_limit: StrictInt | None = Field(default=None, gt=0, le=100)

    _unique = field_validator("seats")(reject_duplicates)


class SeatState(BaseModel):
    label: str
    status: SeatStatus


class SeatCounts(BaseModel):
    available: int
    held: int
    confirmed: int


class ShowOut(BaseModel):
    id: UUID
    name: str
    price_paise: int
    per_user_limit: int
    total_seats: int
    created_at: datetime
    counts: SeatCounts
    invariant_ok: bool
    seats: list[SeatState] | None = None
