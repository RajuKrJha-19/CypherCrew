"""AI settings: per-post ceiling for question auto-answers (0 = unlimited).

Additive + inspector-guarded + idempotent. Existing rows default to 0 (no
limit), so questions stay exempt from the acknowledgment flood cap.

Revision ID: f3a4b5c6d7e8
Revises: e2f3a4b5c6d7
"""

import sqlalchemy as sa
from alembic import op

revision = "f3a4b5c6d7e8"
down_revision = "e2f3a4b5c6d7"
branch_labels = None
depends_on = None


def _cols(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" in set(inspector.get_table_names()):
        if "comment_question_max_per_post" not in _cols(inspector, "ai_settings"):
            op.add_column("ai_settings", sa.Column(
                "comment_question_max_per_post", sa.Integer(),
                nullable=False, server_default="0"))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" in set(inspector.get_table_names()):
        if "comment_question_max_per_post" in _cols(inspector, "ai_settings"):
            op.drop_column("ai_settings", "comment_question_max_per_post")
