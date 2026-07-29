"""Per-channel posting-schedule slots (the "add to queue" cadence).

A channel's posting schedule is its set of recurring weekly slots. One small
table, unique per (account, weekday, minute), cascading with the account.

Revision ID: e7c2a19b4f80
Revises: d2a6c81f3b90
"""

import sqlalchemy as sa
from alembic import op

revision = "e7c2a19b4f80"
down_revision = "d2a6c81f3b90"
branch_labels = None
depends_on = None

SLOTS = "social_posting_slots"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if SLOTS not in set(inspector.get_table_names()):
        op.create_table(
            SLOTS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("social_account_id", sa.Integer(), nullable=False),
            sa.Column("weekday", sa.Integer(), nullable=False),
            sa.Column("minute", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["social_account_id"],
                                    ["social_accounts.id"],
                                    ondelete="CASCADE"),
            sa.UniqueConstraint("social_account_id", "weekday", "minute",
                                name="uq_posting_slot_account_day_minute"),
        )
        op.create_index("ix_social_posting_slots_account", SLOTS,
                        ["social_account_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if SLOTS in set(inspector.get_table_names()):
        op.drop_table(SLOTS)
