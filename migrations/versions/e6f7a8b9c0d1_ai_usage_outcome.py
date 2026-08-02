"""AI usage: outcome (kept vs discarded) for the keep-rate ROI signal.

Additive + inspector-guarded. Nullable, no backfill - existing rows stay NULL
(no signal), new generations record used/discarded going forward.

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
"""

import sqlalchemy as sa
from alembic import op

revision = "e6f7a8b9c0d1"
down_revision = "d5e6f7a8b9c0"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_usage" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("ai_usage")}
    if "outcome" not in cols:
        op.add_column("ai_usage",
                      sa.Column("outcome", sa.String(length=16), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_usage" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("ai_usage")}
    if "outcome" in cols:
        op.drop_column("ai_usage", "outcome")
