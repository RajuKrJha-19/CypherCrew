"""Campaign label on social posts (grouping + utm_campaign).

Revision ID: f3b6d80c1a92
Revises: e7c2a19b4f80
"""

import sqlalchemy as sa
from alembic import op

revision = "f3b6d80c1a92"
down_revision = "e7c2a19b4f80"
branch_labels = None
depends_on = None

POSTS = "social_posts"
COLUMN = "campaign"
INDEX = "ix_social_posts_campaign"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if POSTS not in set(inspector.get_table_names()):
        return
    present = {c["name"] for c in inspector.get_columns(POSTS)}
    if COLUMN not in present:
        op.add_column(POSTS, sa.Column(COLUMN, sa.String(length=120),
                                       nullable=True))
    if not any(i["name"] == INDEX for i in inspector.get_indexes(POSTS)):
        op.create_index(INDEX, POSTS, [COLUMN])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if POSTS not in set(inspector.get_table_names()):
        return
    if any(i["name"] == INDEX for i in inspector.get_indexes(POSTS)):
        op.drop_index(INDEX, table_name=POSTS)
    present = {c["name"] for c in inspector.get_columns(POSTS)}
    if COLUMN in present:
        op.drop_column(POSTS, COLUMN)
