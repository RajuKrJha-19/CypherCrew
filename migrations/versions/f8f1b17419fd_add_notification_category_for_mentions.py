"""add notification category for mentions

Revision ID: f8f1b17419fd
Revises: e5dda5583e93
Create Date: 2026-07-25 18:17:39.313960

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f8f1b17419fd'
down_revision = 'e5dda5583e93'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping several ix_* indexes it doesn't
# recognise (declared as raw SQL rather than index=True on the model,
# same as prior revisions) - only the notifications column this
# revision actually adds is kept below.


def upgrade():
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'category',
                sa.String(length=20),
                nullable=False,
                server_default='activity',
            )
        )
        batch_op.alter_column('category', server_default=None)

    # Backfill: existing "You were mentioned" rows predate this column
    # and would otherwise silently vanish from the new mentions panel
    # (they'd default to "activity" and show up nowhere relevant).
    op.execute(
        "UPDATE notifications SET category = 'mention' "
        "WHERE title = 'You were mentioned'"
    )


def downgrade():
    with op.batch_alter_table('notifications', schema=None) as batch_op:
        batch_op.drop_column('category')
