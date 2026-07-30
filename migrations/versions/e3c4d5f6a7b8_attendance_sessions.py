"""Attendance: attendance_sessions table (check-in/out spans + idle state).

Revision ID: e3c4d5f6a7b8
Revises: d2b3c4e5f6a7
"""

import sqlalchemy as sa
from alembic import op

revision = "e3c4d5f6a7b8"
down_revision = "d2b3c4e5f6a7"
branch_labels = None
depends_on = None

TABLE = "attendance_sessions"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"),
                  nullable=False),
        sa.Column("source", sa.String(length=20), nullable=False,
                  server_default="software"),
        sa.Column("check_in_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("check_out_at", sa.DateTime(), nullable=True),
        sa.Column("zoho_entry_id", sa.String(length=64), nullable=True),
        sa.Column("checkout_pending_zoho", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("snooze_until", sa.DateTime(), nullable=True),
        sa.Column("last_idle_alert_at", sa.DateTime(), nullable=True),
        sa.Column("idle_alert_count", sa.Integer(), nullable=False,
                  server_default="0"),
        sa.Column("last_escalated_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )
    op.create_index("ix_attendance_sessions_user_id", TABLE, ["user_id"])
    op.create_index(
        "ix_attendance_sessions_zoho_entry_id", TABLE, ["zoho_entry_id"])
    op.create_index(
        "ix_attendance_user_checkin", TABLE, ["user_id", "check_in_at"])
    # At most one open session per user.
    op.create_index(
        "uq_attendance_open_per_user", TABLE, ["user_id"], unique=True,
        postgresql_where=sa.text("check_out_at IS NULL"))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        op.drop_table(TABLE)
