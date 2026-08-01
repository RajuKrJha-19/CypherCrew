"""Clients: brand knowledge base (brand_voice + brand_guidelines_notes).

Free-text brand context the AI assist layer reads so captions and media QA
come out on-brand. Additive and nullable - the app behaves exactly as before
until a manager fills them in.

Revision ID: c7a1b2d3e4f5
Revises: d2f5a83c6e17
"""

import sqlalchemy as sa
from alembic import op

revision = "c7a1b2d3e4f5"
down_revision = "d2f5a83c6e17"
branch_labels = None
depends_on = None

TABLE = "clients"
COLUMNS = ("brand_voice", "brand_guidelines_notes")


def _existing(inspector):
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    have = _existing(inspector)
    for col in COLUMNS:
        if col not in have:
            op.add_column(TABLE, sa.Column(col, sa.Text(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    have = _existing(inspector)
    for col in COLUMNS:
        if col in have:
            op.drop_column(TABLE, col)
