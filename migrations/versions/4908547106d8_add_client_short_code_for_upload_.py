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
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.add_column(sa.Column('short_code', sa.String(length=12), nullable=True))


def downgrade():
    with op.batch_alter_table('clients', schema=None) as batch_op:
        batch_op.drop_column('short_code')
