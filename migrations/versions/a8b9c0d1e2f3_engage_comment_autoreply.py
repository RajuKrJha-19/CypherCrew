"""Engage comment auto-reply: guardrails, per-client opt-in, auto-sent flag.

Additive + inspector-guarded. Every column defaults to the prior behaviour
(auto-reply off), so nothing changes until an admin enables it.

Revision ID: a8b9c0d1e2f3
Revises: f7a8b9c0d1e2
"""

import sqlalchemy as sa
from alembic import op

revision = "a8b9c0d1e2f3"
down_revision = "f7a8b9c0d1e2"
branch_labels = None
depends_on = None

# (table, column-name, column)
_ADDS = [
    ("ai_settings", "comment_autoreply_enabled",
     sa.Column("comment_autoreply_enabled", sa.Boolean(), nullable=False,
               server_default=sa.false())),
    ("ai_settings", "comment_max_len",
     sa.Column("comment_max_len", sa.Integer(), nullable=False, server_default="120")),
    ("ai_settings", "comment_max_per_post",
     sa.Column("comment_max_per_post", sa.Integer(), nullable=False, server_default="5")),
    ("clients", "comment_autoreply",
     sa.Column("comment_autoreply", sa.Boolean(), nullable=False,
               server_default=sa.false())),
    ("social_comments", "auto_sent",
     sa.Column("auto_sent", sa.Boolean(), nullable=False, server_default=sa.false())),
]


def _has(inspector, table):
    return table in set(inspector.get_table_names())


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, name, col in _ADDS:
        if not _has(inspector, table):
            continue
        if name not in {c["name"] for c in inspector.get_columns(table)}:
            op.add_column(table, col)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    for table, name, _col in reversed(_ADDS):
        if not _has(inspector, table):
            continue
        if name in {c["name"] for c in inspector.get_columns(table)}:
            op.drop_column(table, name)
