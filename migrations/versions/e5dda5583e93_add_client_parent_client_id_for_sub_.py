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
