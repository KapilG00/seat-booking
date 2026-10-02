import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

RESERVATION_STATUSES = ("confirmed", "cancelled")


class Reservation(Base):
    """A user's booking of one or more seats. The idempotency key lives here;
    the unique constraint is what makes "same key reserves exactly once" hold
    under concurrency."""

    __tablename__ = "reservations"
    __table_args__ = (
        CheckConstraint("amount_paise >= 0", name="amount_non_negative"),
        CheckConstraint(f"status IN {RESERVATION_STATUSES}", name="status_valid"),
        UniqueConstraint("user_id", "idempotency_key"),
        Index("ix_reservations_show_id", "show_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shows.id", ondelete="CASCADE")
    )
    user_id: Mapped[str] = mapped_column(Text)
    seats: Mapped[list[str]] = mapped_column(ARRAY(Text))
    amount_paise: Mapped[int] = mapped_column(BigInteger)
    status: Mapped[str] = mapped_column(Text)
    idempotency_key: Mapped[str] = mapped_column(Text)
    request_hash: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    cancelled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    def to_dict(self) -> dict:
        """API shape (see app/schemas/reservation.py: ReservationOut)."""
        return {
            "reservation_id": self.id,
            "show_id": self.show_id,
            "user_id": self.user_id,
            "seats": list(self.seats),
            "amount_paise": self.amount_paise,
            "status": self.status,
            "created_at": self.created_at,
            "cancelled_at": self.cancelled_at,
        }
