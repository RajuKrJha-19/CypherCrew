"""AI settings: guarded question auto-answer switch.

Additive + inspector-guarded + idempotent. Adds one boolean to ai_settings so
comment auto-reply can also answer questions (off by default). Existing rows
keep working (defaulted false).

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
"""

import sqlalchemy as sa
from alembic import op

revision = "e2f3a4b5c6d7"
down_revision = "d1e2f3a4b5c6"
branch_labels = None
depends_on = None


def _cols(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" in set(inspector.get_table_names()):
        if "comment_answer_questions_enabled" not in _cols(inspector, "ai_settings"):
            op.add_column("ai_settings", sa.Column(
                "comment_answer_questions_enabled", sa.Boolean(),
                nullable=False, server_default=sa.text("false")))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" in set(inspector.get_table_names()):
        if "comment_answer_questions_enabled" in _cols(inspector, "ai_settings"):
            op.drop_column("ai_settings", "comment_answer_questions_enabled")
