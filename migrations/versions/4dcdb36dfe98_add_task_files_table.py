"""add task_files table

Revision ID: 4dcdb36dfe98
Revises: 917d678de805
Create Date: 2026-07-13 18:44:15.577505

"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = '4dcdb36dfe98'
down_revision = '917d678de805'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "task_files",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("task_id", sa.Integer(), nullable=False),
        sa.Column("bucket_name", sa.String(length=100), nullable=False),
        sa.Column("storage_provider", sa.String(length=30), nullable=False),
        sa.Column("object_key", sa.String(length=1000), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("stored_filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=150), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("folder_type", sa.String(length=30), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("is_final", sa.Boolean(), nullable=False),
        sa.Column("uploaded_by_id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),

        sa.ForeignKeyConstraint(
            ["task_id"],
            ["tasks.id"],
            ondelete="CASCADE"
        ),

        sa.ForeignKeyConstraint(
            ["uploaded_by_id"],
            ["users.id"]
        ),

        sa.PrimaryKeyConstraint("id"),

        sa.UniqueConstraint(
            "object_key",
            name="uq_task_files_object_key"
        ),
    )

    op.create_index(
        "ix_task_files_task_id",
        "task_files",
        ["task_id"]
    )

    op.create_index(
        "ix_task_files_storage_provider",
        "task_files",
        ["storage_provider"]
    )


def downgrade():
    op.drop_index(
        "ix_task_files_storage_provider",
        table_name="task_files"
    )

    op.drop_index(
        "ix_task_files_task_id",
        table_name="task_files"
    )

    op.drop_table("task_files")