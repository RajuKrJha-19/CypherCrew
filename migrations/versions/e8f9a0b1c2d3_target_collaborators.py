"""SocialPostTarget.collaborators — Instagram co-authors invited on a post.

Additive + nullable: a JSON list of IG usernames to invite as collaborators on
this target's post. Empty/NULL until the composer sets it, so nothing changes
for existing posts.

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
"""

import sqlalchemy as sa
from alembic import op

revision = "e8f9a0b1c2d3"
down_revision = "d7e8f9a0b1c2"
branch_labels = None
depends_on = None

_TABLE = "social_post_targets"
_COL = "collaborators"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COL not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(_COL, sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COL in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COL)
