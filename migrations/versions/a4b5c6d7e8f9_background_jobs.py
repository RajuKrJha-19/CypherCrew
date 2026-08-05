"""Background jobs: visible status for user-triggered async actions.

Additive + inspector-guarded + idempotent. Creates the background_jobs table
only if it isn't already there.

Revision ID: a4b5c6d7e8f9
Revises: f3a4b5c6d7e8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "a4b5c6d7e8f9"
down_revision = "f3a4b5c6d7e8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "background_jobs" in set(inspector.get_table_names()):
        return
    op.create_table(
        "background_jobs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(40), nullable=False),
        sa.Column("client_id", sa.Integer(),
                  sa.ForeignKey("clients.id", ondelete="SET NULL"), nullable=True),
        sa.Column("status", sa.String(12), nullable=False,
                  server_default="running"),
        sa.Column("message", sa.String(300), nullable=True),
        sa.Column("result", JSONB(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("started_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
    )
    op.create_index("ix_background_jobs_started_at", "background_jobs",
                    ["started_at"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "background_jobs" in set(inspector.get_table_names()):
        op.drop_index("ix_background_jobs_started_at",
                      table_name="background_jobs")
        op.drop_table("background_jobs")
