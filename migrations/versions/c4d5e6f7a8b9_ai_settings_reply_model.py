"""AI settings: optional per-task provider/model for Google review replies.

Additive + inspector-guarded. Blank -> replies keep riding the caption model,
so existing behaviour is unchanged until an admin sets an override.

Revision ID: c4d5e6f7a8b9
Revises: b3c4d5e6f7a8
"""

import sqlalchemy as sa
from alembic import op

revision = "c4d5e6f7a8b9"
down_revision = "b3c4d5e6f7a8"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("ai_settings")}
    if "reply_provider" not in cols:
        op.add_column("ai_settings",
                      sa.Column("reply_provider", sa.String(length=30), nullable=True))
    if "reply_model" not in cols:
        op.add_column("ai_settings",
                      sa.Column("reply_model", sa.String(length=120), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("ai_settings")}
    if "reply_model" in cols:
        op.drop_column("ai_settings", "reply_model")
    if "reply_provider" in cols:
        op.drop_column("ai_settings", "reply_provider")
