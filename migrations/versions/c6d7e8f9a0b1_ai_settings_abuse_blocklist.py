"""AISettings.abuse_blocklist — auto-hide abusive / profane comments.

A second moderation list alongside spam_blocklist: comma-separated abuse /
profanity / hate keywords. A comment matching it is auto-HIDDEN (reversible,
never deleted), on every lane including ads. Additive and nullable, so nothing
changes until an admin fills it in.

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
"""

import sqlalchemy as sa
from alembic import op

revision = "c6d7e8f9a0b1"
down_revision = "b5c6d7e8f9a0"
branch_labels = None
depends_on = None

_TABLE = "ai_settings"
_COL = "abuse_blocklist"


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
