"""add task social media platform fields

Revision ID: 54218146054f
Revises: ccb2b2dce4a8
Create Date: 2026-07-25 16:13:31.773183

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '54218146054f'
down_revision = 'ccb2b2dce4a8'
branch_labels = None
depends_on = None


# Autogenerate also proposed dropping several ix_* indexes it doesn't
# recognise (declared as raw SQL rather than index=True on the model,
# same as the previous two revisions) - only the tasks columns this
# revision actually adds are kept below.


def upgrade():
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        # server_default so the NOT NULL is satisfiable for existing
        # rows; dropped right after so it doesn't linger as an implicit
        # default new inserts could accidentally rely on instead of the
        # model's own default=False.
        batch_op.add_column(
            sa.Column(
                'is_social_media',
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch_op.add_column(sa.Column('social_platforms', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('social_platforms_published', sa.String(length=255), nullable=True))
        batch_op.alter_column('is_social_media', server_default=None)


def downgrade():
    with op.batch_alter_table('tasks', schema=None) as batch_op:
        batch_op.drop_column('social_platforms_published')
        batch_op.drop_column('social_platforms')
        batch_op.drop_column('is_social_media')
