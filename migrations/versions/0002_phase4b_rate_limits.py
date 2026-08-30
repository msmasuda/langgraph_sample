"""フェーズ4-Bの共有レート制限テーブルを追加する。

Revision ID: 0002_phase4b
Revises: 0001_phase3
Create Date: 2026-08-30
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0002_phase4b"
down_revision: str | None = "0001_phase3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "rate_limit_buckets",
        sa.Column("scope", sa.String(length=20), nullable=False),
        sa.Column("subject_hash", sa.String(length=64), nullable=False),
        sa.Column("window_started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("request_count", sa.Integer(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("scope", "subject_hash", "window_started_at"),
    )
    op.create_index(
        "ix_rate_limit_buckets_expires_at",
        "rate_limit_buckets",
        ["expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_rate_limit_buckets_expires_at",
        table_name="rate_limit_buckets",
    )
    op.drop_table("rate_limit_buckets")
