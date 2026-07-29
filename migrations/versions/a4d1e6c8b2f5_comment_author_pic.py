"""Commenter profile-picture URL on social comments (Engage avatars).

Revision ID: a4d1e6c8b2f5
Revises: f3b6d80c1a92
"""

import sqlalchemy as sa
from alembic import op

revision = "a4d1e6c8b2f5"
down_revision = "f3b6d80c1a92"
branch_labels = None
depends_on = None

TABLE = "social_comments"
COLUMN = "author_pic"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN not in present:
        op.add_column(TABLE, sa.Column(COLUMN, sa.String(length=500),
                                       nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in inspector.get_columns(TABLE)}
    if COLUMN in present:
        op.drop_column(TABLE, COLUMN)
