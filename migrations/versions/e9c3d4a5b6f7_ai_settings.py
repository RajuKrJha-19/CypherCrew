"""AI: ai_settings table (runtime provider/model config, single row).

Additive - with no row the app uses the AI_* env defaults, so behaviour is
unchanged until an admin saves settings.

Revision ID: e9c3d4a5b6f7
Revises: d8b2c3f4a5e6
"""

import sqlalchemy as sa
from alembic import op

revision = "e9c3d4a5b6f7"
down_revision = "d8b2c3f4a5e6"
branch_labels = None
depends_on = None

TABLE = "ai_settings"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        "ai_settings",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False,
                  server_default=sa.true()),
        sa.Column("caption_provider", sa.String(length=30), nullable=True),
        sa.Column("caption_model", sa.String(length=120), nullable=True),
        sa.Column("qa_provider", sa.String(length=30), nullable=True),
        sa.Column("qa_model", sa.String(length=120), nullable=True),
        sa.Column("updated_by_id", sa.Integer(),
                  sa.ForeignKey("users.id"), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=True),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE in set(inspector.get_table_names()):
        op.drop_table(TABLE)
