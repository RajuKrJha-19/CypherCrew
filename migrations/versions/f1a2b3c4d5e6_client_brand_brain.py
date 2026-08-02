"""Clients: brand_brain (structured knowledgebase for the AI fact-checker).

A JSONB dict of {section_key: multiline text} - official phones/emails/
websites, offers, disclaimers, do's/don'ts, etc. Additive and nullable; the
app behaves exactly as before until a manager fills it in.

Revision ID: f1a2b3c4d5e6
Revises: e9c3d4a5b6f7
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "f1a2b3c4d5e6"
down_revision = "e9c3d4a5b6f7"
branch_labels = None
depends_on = None

TABLE = "clients"
COLUMN = "brand_brain"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    if COLUMN not in {c["name"] for c in inspector.get_columns(TABLE)}:
        op.add_column(TABLE, sa.Column(COLUMN, JSONB(), nullable=True))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if TABLE not in set(inspector.get_table_names()):
        return
    if COLUMN in {c["name"] for c in inspector.get_columns(TABLE)}:
        op.drop_column(TABLE, COLUMN)
