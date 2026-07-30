"""Attendance: zoho_connections table (org-level Zoho People connection).

Revision ID: d2b3c4e5f6a7
Revises: c1a2b3d4e5f6
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "d2b3c4e5f6a7"
down_revision = "c1a2b3d4e5f6"
branch_labels = None
depends_on = None

TABLE = "zoho_connections"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("dc", sa.String(length=10), nullable=False,
                  server_default="com"),
        sa.Column("scopes", sa.Text(), nullable=True),
        sa.Column("token_ciphertext", sa.Text(), nullable=True),
        sa.Column("token_key_version", sa.Integer(), nullable=False,
                  server_default="1"),
        sa.Column("refresh_ciphertext", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(), nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False,
                  server_default="active"),
        sa.Column("connected_by_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("meta", JSONB(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False,
                  server_default=sa.func.now()),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        op.drop_table(TABLE)
