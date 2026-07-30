"""Attendance: distinct inactivity buzzer settings.

Revision ID: a5e6f7b8c9d0
Revises: f4d5e6a7b8c9
"""

import sqlalchemy as sa
from alembic import op

revision = "a5e6f7b8c9d0"
down_revision = "f4d5e6a7b8c9"
branch_labels = None
depends_on = None

TABLE = "attendance_settings"
COLUMNS = (
    ("buzzer_enabled", sa.Boolean(), sa.true()),
    ("buzzer_volume", sa.Integer(), "70"),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    for name, type_, default in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(
                name, type_, nullable=False, server_default=default))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    for name, _type, _default in reversed(COLUMNS):
        if name in present:
            op.drop_column(TABLE, name)
