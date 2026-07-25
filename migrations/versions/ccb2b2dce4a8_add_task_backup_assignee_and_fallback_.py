"""add task backup assignee and fallback fields

Revision ID: ccb2b2dce4a8
Revises: 55cf46b91e88
Create Date: 2026-07-25 14:07:52.272817

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'ccb2b2dce4a8'
down_revision = '55cf46b91e88'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping ix_tasks_*, ix_notifications_*
# and ix_task_visibility_user_id - those are the indexes from
# 55cf46b91e88 (add_performance_indexes), just declared as raw SQL
# rather than index=True on the model columns, so autogenerate can't
# see they're intentional. Only the tasks columns this revision
# actually adds are kept below.


def upgrade():
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.add_column(sa.Column('backup_assignee_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('fallback_hours', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('fallback_triggered_at', sa.DateTime(), nullable=True))
        batch_op.create_foreign_key(
            'fk_tasks_backup_assignee_id_users',
            'users',
            ['backup_assignee_id'],
            ['id'],
        )


def downgrade():
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_constraint('fk_tasks_backup_assignee_id_users', type_='foreignkey')
        batch_op.drop_column('fallback_triggered_at')
        batch_op.drop_column('fallback_hours')
        batch_op.drop_column('backup_assignee_id')
