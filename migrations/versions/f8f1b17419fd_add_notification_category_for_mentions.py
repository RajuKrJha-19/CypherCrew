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
    # Idempotent. The baseline migration now builds notifications instead of leaving
    # it to a schema created by hand, so on a database restored from nothing
    # this column already exists by the time we get here and there is nothing
    # to add. On the production database this migration ran long ago. Either
    # way, re-running it must not raise - which is what it did before, because
    # a batch_alter_table block cannot guard its own statements.
    _bind = op.get_bind()
    _inspector = sa.inspect(_bind)
    if "notifications" not in set(_inspector.get_table_names()):
        return
    if "category" in {c["name"] for c in _inspector.get_columns("notifications")}:
        return

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
