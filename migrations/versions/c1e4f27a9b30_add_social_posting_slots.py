"""add social_posting_slots

Revision ID: c1e4f27a9b30
Revises: b6f7a8c9d0e1
Create Date: 2026-08-01

`social_posting_slots` was created directly against the production database and
never written as a migration, so it was one of the 37 tables a restore from an
empty database would silently omit.

It does not belong in the baseline with the other 36: it carries a foreign key
to `social_accounts`, which a much later migration creates, so building it that
early fails outright. It genuinely arrived after the social tables did, and
this is where it goes.

Guarded, like every other migration in this chain: on the existing production
database the table is already there and this does nothing.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c1e4f27a9b30'
down_revision = 'b6f7a8c9d0e1'
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "social_posting_slots" in inspector.get_table_names():
        return

    op.create_table(
        "social_posting_slots",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("social_account_id", sa.Integer(), nullable=False),
        sa.Column("weekday", sa.Integer(), nullable=False),
        sa.Column("minute", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["social_account_id"], ["social_accounts.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("social_account_id", "weekday", "minute",
                            name="uq_posting_slot_account_day_minute"),
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "social_posting_slots" in inspector.get_table_names():
        op.drop_table("social_posting_slots")
