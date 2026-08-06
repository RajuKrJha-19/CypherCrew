"""SocialPostTarget.thumbnail_url — the post's own image, for the Engage preview.

The inbox showed a comment with only the post's TITLE above it, which for an
ad post is the literal placeholder "Ad post". Storing the platform's own
caption (an existing column) and thumbnail lets the person answering see what
they are answering about.

Nullable and unused until populated, so nothing changes until a sync fills it.
The URL is Meta's CDN and expires; it is refreshed on every ad sync and the
template hides a broken image, so a stale value degrades to "no picture"
rather than a broken one.

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
"""

import sqlalchemy as sa
from alembic import op

revision = "b5c6d7e8f9a0"
down_revision = "a4b5c6d7e8f9"
branch_labels = None
depends_on = None

_TABLE = "social_post_targets"
_COL = "thumbnail_url"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COL not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COL, sa.String(length=1000),
                                        nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COL in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COL)
