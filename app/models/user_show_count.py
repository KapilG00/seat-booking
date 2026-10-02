import uuid

from sqlalchemy import CheckConstraint, ForeignKey, Integer, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class UserShowCount(Base):
    """How many seats a user currently holds for a show. The CHECK is a last
    line of defence; the conditional upsert in the reserve path is what
    enforces per_user_limit."""

    __tablename__ = "user_show_counts"
    __table_args__ = (CheckConstraint("held >= 0", name="held_non_negative"),)

    show_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shows.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[str] = mapped_column(Text, primary_key=True)
    held: Mapped[int] = mapped_column(Integer)
