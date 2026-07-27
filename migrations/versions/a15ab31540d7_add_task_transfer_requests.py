"""add task transfer requests

Revision ID: a15ab31540d7
Revises: 4908547106d8
Create Date: 2026-07-27

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'a15ab31540d7'
down_revision = '4908547106d8'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping several ix_* indexes it does not
# recognise (declared as raw SQL rather than index=True on the model,
# same as prior revisions) - only the new table is kept below.


def upgrade():
    op.create_table(
        'task_transfer_requests',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('task_id', sa.Integer(), nullable=False),
        sa.Column('from_user_id', sa.Integer(), nullable=False),
        sa.Column('to_user_id', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(length=20), nullable=False),
        sa.Column('message', sa.Text(), nullable=True),
        sa.Column('response_message', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('responded_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['from_user_id'], ['users.id'], ),
        sa.ForeignKeyConstraint(['task_id'], ['tasks.id'], ),
        sa.ForeignKeyConstraint(['to_user_id'], ['users.id'], ),
        sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('task_transfer_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_task_transfer_requests_status'), ['status'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_transfer_requests_task_id'), ['task_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_task_transfer_requests_to_user_id'), ['to_user_id'], unique=False)


def downgrade():
    with op.batch_alter_table('task_transfer_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_task_transfer_requests_to_user_id'))
        batch_op.drop_index(batch_op.f('ix_task_transfer_requests_task_id'))
        batch_op.drop_index(batch_op.f('ix_task_transfer_requests_status'))

    op.drop_table('task_transfer_requests')
