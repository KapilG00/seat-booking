import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Index, Integer, Text, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

SEAT_STATUSES = ("available", "held", "confirmed")


class Seat(Base):
    """One row per seat, so available + held + confirmed == total_seats holds
    by construction. The CHECKs make illegal states unrepresentable: a seat
    that is not available always names the reservation (and user) owning it."""

    __tablename__ = "seats"
    __table_args__ = (
        CheckConstraint(f"status IN {SEAT_STATUSES}", name="status_valid"),
        CheckConstraint("(status = 'available') = (reservation_id IS NULL)", name="owned_iff_taken"),
        CheckConstraint("(reservation_id IS NULL) = (user_id IS NULL)", name="owner_consistent"),
        Index(
            "ix_seats_reservation_id",
            "reservation_id",
            postgresql_where=text("reservation_id IS NOT NULL"),
        ),
    )

    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shows.id", ondelete="CASCADE"), primary_key=True
    )
    label: Mapped[str] = mapped_column(Text, primary_key=True)
    # The admin's original order, for display (A2 before A10).
    position: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(Text, server_default=text("'available'"))
    reservation_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[str | None] = mapped_column(Text)
