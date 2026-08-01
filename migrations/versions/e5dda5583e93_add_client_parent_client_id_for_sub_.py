"""add client parent_client_id for sub-clients

Revision ID: e5dda5583e93
Revises: 54218146054f
Create Date: 2026-07-25 17:32:17.169347

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e5dda5583e93'
down_revision = '54218146054f'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping several ix_* indexes it doesn't
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
    if "parent_client_id" in {c["name"] for c in _inspector.get_columns("clients")}:
        return

    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('parent_client_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_clients_parent_client_id_clients',
            'clients',
            ['parent_client_id'],
            ['id'],
        )


def downgrade():
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_constraint('fk_clients_parent_client_id_clients', type_='foreignkey')
        batch_op.drop_column('parent_client_id')
