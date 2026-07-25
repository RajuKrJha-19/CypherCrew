"""add client_assets table for brand assets

Revision ID: f4afabbda8a4
Revises: f8f1b17419fd
Create Date: 2026-07-25 18:41:13.850968

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f4afabbda8a4'
down_revision = 'f8f1b17419fd'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping several ix_* indexes it doesn't
# recognise (declared as raw SQL rather than index=True on the model,
# same as prior revisions) - only the new client_assets table is kept
# below.


def upgrade():
    op.create_table(
        'client_assets',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('client_id', sa.Integer(), nullable=False),
        sa.Column('category', sa.String(length=20), nullable=False),
        sa.Column('bucket_name', sa.String(length=100), nullable=False),
        sa.Column('storage_provider', sa.String(length=30), nullable=False),
        sa.Column('object_key', sa.String(length=1000), nullable=False),
        sa.Column('original_filename', sa.String(length=255), nullable=False),
        sa.Column('stored_filename', sa.String(length=255), nullable=False),
        sa.Column('mime_type', sa.String(length=150), nullable=True),
        sa.Column('file_size', sa.BigInteger(), nullable=True),
        sa.Column('uploaded_by_id', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['client_id'], ['clients.id'], ),
        sa.ForeignKeyConstraint(['uploaded_by_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('object_key')
    )
    with op.batch_alter_table('client_assets', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_client_assets_client_id'),
            ['client_id'],
            unique=False,
        )


def downgrade():
    with op.batch_alter_table('client_assets', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_client_assets_client_id'))

    op.drop_table('client_assets')
