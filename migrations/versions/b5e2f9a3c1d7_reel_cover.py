"""Reel cover: custom cover image key + frame offset on social posts.

Revision ID: b5e2f9a3c1d7
Revises: a4d1e6c8b2f5
"""

import sqlalchemy as sa
from alembic import op

revision = "b5e2f9a3c1d7"
down_revision = "a4d1e6c8b2f5"
branch_labels = None
depends_on = None

TABLE = "social_posts"
COLUMNS = (
    ("reel_cover_key", sa.String(length=1000)),
    ("reel_thumb_offset", sa.Integer()),
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


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    for name, _type in reversed(COLUMNS):
        if name in present:
            op.drop_column(TABLE, name)
