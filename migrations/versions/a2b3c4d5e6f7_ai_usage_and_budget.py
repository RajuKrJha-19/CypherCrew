"""AI: ai_usage log table + ai_settings.monthly_budget_usd.

The per-call spend/activity log behind the AI Usage screen, and the soft
monthly budget cap. Additive - no behaviour changes until AI runs.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""

import sqlalchemy as sa
from alembic import op

revision = "a2b3c4d5e6f7"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None

USAGE = "ai_usage"
SETTINGS = "ai_settings"
BUDGET = "monthly_budget_usd"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if USAGE not in tables:
        op.create_table(
            "ai_usage",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.func.now()),
            sa.Column("user_id", sa.Integer(),
                      sa.ForeignKey("users.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("client_id", sa.Integer(),
                      sa.ForeignKey("clients.id", ondelete="SET NULL"),
                      nullable=True),
            sa.Column("feature", sa.String(length=30), nullable=False),
            sa.Column("provider", sa.String(length=30), nullable=True),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("output_tokens", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("est_cost_usd", sa.Float(), nullable=False,
                      server_default="0"),
            sa.Column("status", sa.String(length=20), nullable=False,
                      server_default="ok"),
        )
        op.create_index("ix_ai_usage_created_at", "ai_usage", ["created_at"])

    if SETTINGS in tables:
        cols = {c["name"] for c in inspector.get_columns(SETTINGS)}
        if BUDGET not in cols:
            op.add_column(SETTINGS, sa.Column(
                BUDGET, sa.Float(), nullable=False, server_default="0"))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if SETTINGS in tables:
        cols = {c["name"] for c in inspector.get_columns(SETTINGS)}
        if BUDGET in cols:
            op.drop_column(SETTINGS, BUDGET)
    if USAGE in tables:
        op.drop_table("ai_usage")
