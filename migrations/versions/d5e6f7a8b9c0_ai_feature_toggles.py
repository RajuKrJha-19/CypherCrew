"""AI settings: per-feature on/off toggles (captions, media QA, review
replies, comment replies).

Additive + inspector-guarded. Every column defaults TRUE, so existing
behaviour is unchanged until an admin turns a specific feature off.

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
"""

import sqlalchemy as sa
from alembic import op

revision = "d5e6f7a8b9c0"
down_revision = "c4d5e6f7a8b9"
branch_labels = None
depends_on = None

_COLS = ("caption_enabled", "qa_enabled", "reply_enabled", "comment_enabled")


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" not in set(inspector.get_table_names()):
        return
    have = {c["name"] for c in inspector.get_columns("ai_settings")}
    for col in _COLS:
        if col not in have:
            op.add_column("ai_settings", sa.Column(
                col, sa.Boolean(), nullable=False, server_default=sa.true()))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" not in set(inspector.get_table_names()):
        return
    have = {c["name"] for c in inspector.get_columns("ai_settings")}
    for col in reversed(_COLS):
        if col in have:
            op.drop_column("ai_settings", col)
