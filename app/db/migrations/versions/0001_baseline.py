"""Baseline schema: shows, seats, reservations, user_show_counts.

On a fresh database this creates the schema. On a database created by the
pre-Alembic SQL runner (it has `shows` and a `schema_migrations` table), it
adopts the existing tables: renames indexes/constraints to the models' naming
convention and drops the old bookkeeping table. No data is touched.

Revision ID: 0001
Revises:
Create Date: 2026-10-02
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Old auto-generated names -> names the models' naming convention produces.
_LEGACY_RENAMES = [
    "ALTER INDEX IF EXISTS seats_reservation_idx RENAME TO ix_seats_reservation_id",
    "ALTER INDEX IF EXISTS reservations_show_idx RENAME TO ix_reservations_show_id",
    "ALTER TABLE reservations RENAME CONSTRAINT reservations_user_id_idempotency_key_key "
    "TO uq_reservations_user_id_idempotency_key",
]


def upgrade() -> None:
    bind = op.get_bind()
    if sa.inspect(bind).has_table("shows"):
        for stmt in _LEGACY_RENAMES:
            op.execute(stmt)
        op.execute("DROP TABLE IF EXISTS schema_migrations")
        return

    op.create_table(
        "shows",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("price_paise", sa.BigInteger(), nullable=False),
        sa.Column("per_user_limit", sa.Integer(), server_default=sa.text("4"), nullable=False),
        sa.Column("total_seats", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("price_paise > 0", name=op.f("ck_shows_price_positive")),
        sa.CheckConstraint("per_user_limit > 0", name=op.f("ck_shows_limit_positive")),
        sa.CheckConstraint("total_seats > 0", name=op.f("ck_shows_total_positive")),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_shows")),
    )
    op.create_table(
        "seats",
        sa.Column("show_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("label", sa.Text(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("status", sa.Text(), server_default=sa.text("'available'"), nullable=False),
        sa.Column("reservation_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("user_id", sa.Text(), nullable=True),
        sa.CheckConstraint(
            "status IN ('available', 'held', 'confirmed')", name=op.f("ck_seats_status_valid")
        ),
        sa.CheckConstraint(
            "(status = 'available') = (reservation_id IS NULL)", name=op.f("ck_seats_owned_iff_taken")
        ),
        sa.CheckConstraint(
            "(reservation_id IS NULL) = (user_id IS NULL)", name=op.f("ck_seats_owner_consistent")
        ),
        sa.ForeignKeyConstraint(
            ["show_id"], ["shows.id"], name=op.f("fk_seats_show_id_shows"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("show_id", "label", name=op.f("pk_seats")),
    )
    op.create_index(
        "ix_seats_reservation_id",
        "seats",
        ["reservation_id"],
        postgresql_where=sa.text("reservation_id IS NOT NULL"),
    )
    op.create_table(
        "reservations",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("show_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("seats", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("amount_paise", sa.BigInteger(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("amount_paise >= 0", name=op.f("ck_reservations_amount_non_negative")),
        sa.CheckConstraint(
            "status IN ('confirmed', 'cancelled')", name=op.f("ck_reservations_status_valid")
        ),
        sa.ForeignKeyConstraint(
            ["show_id"], ["shows.id"], name=op.f("fk_reservations_show_id_shows"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_reservations")),
        sa.UniqueConstraint(
            "user_id", "idempotency_key", name=op.f("uq_reservations_user_id_idempotency_key")
        ),
    )
    op.create_index("ix_reservations_show_id", "reservations", ["show_id"])
    op.create_table(
        "user_show_counts",
        sa.Column("show_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("user_id", sa.Text(), nullable=False),
        sa.Column("held", sa.Integer(), nullable=False),
        sa.CheckConstraint("held >= 0", name=op.f("ck_user_show_counts_held_non_negative")),
        sa.ForeignKeyConstraint(
            ["show_id"], ["shows.id"], name=op.f("fk_user_show_counts_show_id_shows"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("show_id", "user_id", name=op.f("pk_user_show_counts")),
    )


def downgrade() -> None:
    op.drop_table("user_show_counts")
    op.drop_index("ix_reservations_show_id", table_name="reservations")
    op.drop_table("reservations")
    op.drop_index("ix_seats_reservation_id", table_name="seats")
    op.drop_table("seats")
    op.drop_table("shows")
