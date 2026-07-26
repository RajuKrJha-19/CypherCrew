"""Add tasks.scheduled_seconds duration bucket.

Additive only: one new nullable integer column (default 0). Backs the
"Scheduled" task status so time spent waiting for an auto-publish slot is
accumulated instead of being silently dropped by an unmapped status.

Revision ID: e5a3b8c2f1d7
Revises: d4e7c1a9f6b2
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = "e5a3b8c2f1d7"
down_revision = "d4e7c1a9f6b2"
branch_labels = None
depends_on = None


def _has_column(table, column):
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if _has_column("tasks", "scheduled_seconds"):
        return
    op.add_column(
        "tasks",
        sa.Column("scheduled_seconds", sa.Integer(), nullable=True,
                  server_default="0"),
    )


def downgrade():
    if _has_column("tasks", "scheduled_seconds"):
        op.drop_column("tasks", "scheduled_seconds")
