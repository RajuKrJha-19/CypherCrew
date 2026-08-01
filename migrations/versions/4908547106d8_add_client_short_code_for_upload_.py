"""add client short_code for upload filenames

Revision ID: 4908547106d8
Revises: f6b4c9d2e8a1
Create Date: 2026-07-27 11:28:38.242646

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4908547106d8'
down_revision = 'f6b4c9d2e8a1'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping several ix_* indexes it does not
# recognise (declared as raw SQL rather than index=True on the model,
# same as prior revisions) - only the clients column this revision
# actually adds is kept below.


def upgrade():
    # Idempotent. The baseline migration now builds clients instead of leaving
    # it to a schema created by hand, so on a database restored from nothing
    # this column already exists by the time we get here and there is nothing
    # to add. On the production database this migration ran long ago. Either
    # way, re-running it must not raise - which is what it did before, because
    # a batch_alter_table block cannot guard its own statements.
    _bind = op.get_bind()
    _inspector = sa.inspect(_bind)
    if "clients" not in set(_inspector.get_table_names()):
        return
    if "short_code" in {c["name"] for c in _inspector.get_columns("clients")}:
        return

    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('short_code', sa.String(length=12), nullable=True))


def downgrade():
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_column('short_code')
