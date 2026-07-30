"""Attendance: attendance_settings table (admin-tunable idle-alert config).

Revision ID: f4d5e6a7b8c9
Revises: e3c4d5f6a7b8
"""

import sqlalchemy as sa
from alembic import op

revision = "f4d5e6a7b8c9"
down_revision = "e3c4d5f6a7b8"
branch_labels = None
depends_on = None

TABLE = "attendance_settings"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("idle_alerts_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("grace_min", sa.Integer(), nullable=False,
                  server_default="15"),
        sa.Column("repeat_min", sa.Integer(), nullable=False,
                  server_default="10"),
        sa.Column("escalate_enabled", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("escalate_after", sa.Integer(), nullable=False,
                  server_default="3"),
        sa.Column("snooze_min", sa.Integer(), nullable=False,
                  server_default="15"),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_by_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        op.drop_table(TABLE)
