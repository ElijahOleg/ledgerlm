"""create alert_firings

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-04 17:00:22.486223

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Hand-adjusted from autogenerate: plain sa.DateTime() instead of the
    # model-layer UTCDateTime decorator (impl DateTime) — migrations must not
    # import model code. Timestamps are stored naive-UTC as everywhere else.
    op.create_table(
        "alert_firings",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("rule", sa.String(), nullable=False),
        sa.Column("window_start", sa.DateTime(), nullable=False),
        sa.Column("window_end", sa.DateTime(), nullable=False),
        sa.Column("observed", sa.Numeric(precision=18, scale=10), nullable=False),
        sa.Column("threshold", sa.Numeric(precision=18, scale=10), nullable=False),
        sa.Column("fired_at", sa.DateTime(), nullable=False),
        sa.Column("delivered", sa.Boolean(), nullable=False),
        sa.Column("response_status", sa.Integer(), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_alert_firings")),
    )


def downgrade() -> None:
    op.drop_table("alert_firings")
