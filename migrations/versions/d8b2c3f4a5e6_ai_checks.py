"""AI media-QA: ai_checks table (advisory findings per submitted file).

One row per AI review of a task_files deliverable. Additive - nothing about
the app changes until the media-QA feature is used.

Revision ID: d8b2c3f4a5e6
Revises: c7a1b2d3e4f5
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d8b2c3f4a5e6"
down_revision = "c7a1b2d3e4f5"
branch_labels = None
depends_on = None

TABLE = "ai_checks"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        "ai_checks",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("task_file_id", sa.Integer(),
                  sa.ForeignKey("task_files.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="clean"),
        sa.Column("model", sa.String(length=120), nullable=True),
        sa.Column("findings", JSONB(), nullable=False,
                  server_default=sa.text("'[]'::jsonb")),
        sa.Column("created_by_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_ai_checks_task_file_id", TABLE, ["task_file_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        op.drop_table(TABLE)
