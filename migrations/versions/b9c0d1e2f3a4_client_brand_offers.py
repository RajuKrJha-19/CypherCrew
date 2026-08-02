"""Client Brain: structured time-limited offers (text + valid-until date).

Additive + inspector-guarded. Nullable JSONB; existing clients keep their
free-text offers section untouched.

Revision ID: b9c0d1e2f3a4
Revises: a8b9c0d1e2f3
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "b9c0d1e2f3a4"
down_revision = "a8b9c0d1e2f3"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "clients" not in set(inspector.get_table_names()):
        return
    if "brand_offers" not in {c["name"] for c in inspector.get_columns("clients")}:
        op.add_column("clients",
                      sa.Column("brand_offers", postgresql.JSONB(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "clients" not in set(inspector.get_table_names()):
        return
    if "brand_offers" in {c["name"] for c in inspector.get_columns("clients")}:
        op.drop_column("clients", "brand_offers")
