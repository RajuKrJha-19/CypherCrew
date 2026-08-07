"""SocialAccount.auto_include — a channel that rides along on every post.

A personal-brand page that is meant to carry everything the institution
publishes had to be ticked by hand on every single post, and forgetting it was
silent. With this on, the composer pre-selects the channel whenever the post is
for its client group; it stays an ordinary checkbox, so any one post can still
opt out.

Additive and off by default (server_default false), so nothing changes until a
channel is explicitly opted in.

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
"""

import sqlalchemy as sa
from alembic import op

revision = "d7e8f9a0b1c2"
down_revision = "c6d7e8f9a0b1"
branch_labels = None
depends_on = None

_TABLE = "social_accounts"
_COL = "auto_include"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COL not in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.add_column(_TABLE, sa.Column(
            _COL, sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _COL in {c["name"] for c in inspector.get_columns(_TABLE)}:
        op.drop_column(_TABLE, _COL)
