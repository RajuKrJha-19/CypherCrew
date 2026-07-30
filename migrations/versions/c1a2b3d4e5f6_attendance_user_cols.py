"""Attendance: per-user check-in source + Zoho employee id.

Revision ID: c1a2b3d4e5f6
Revises: b5e2f9a3c1d7
"""

import sqlalchemy as sa
from alembic import op

revision = "c1a2b3d4e5f6"
down_revision = "b5e2f9a3c1d7"
branch_labels = None
depends_on = None

TABLE = "users"
COLUMNS = (
    ("checkin_source", sa.String(length=20)),
    ("zoho_employee_id", sa.String(length=64)),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    for name, type_ in COLUMNS:
        if name not in present:
            op.add_column(TABLE, sa.Column(name, type_, nullable=True))

    # The column is guaranteed present now (added above if it was missing).
    index_names = {ix["name"] for ix in inspector.get_indexes(TABLE)}
    if "ix_users_zoho_employee_id" not in index_names:
        op.create_index(
            "ix_users_zoho_employee_id", TABLE, ["zoho_employee_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    index_names = {ix["name"] for ix in inspector.get_indexes(TABLE)}
    if "ix_users_zoho_employee_id" in index_names:
        op.drop_index("ix_users_zoho_employee_id", table_name=TABLE)
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    for name, _type in reversed(COLUMNS):
        if name in present:
            op.drop_column(TABLE, name)
