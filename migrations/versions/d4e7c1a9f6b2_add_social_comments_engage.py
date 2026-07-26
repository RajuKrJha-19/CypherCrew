"""Engage: social_comments table (comments inbox).

Additive only: one new table.

Revision ID: d4e7c1a9f6b2
Revises: c9f2a1b7d3e4
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa


revision = "d4e7c1a9f6b2"
down_revision = "c9f2a1b7d3e4"
branch_labels = None
depends_on = None


def _has_table(table):
    return sa.inspect(op.get_bind()).has_table(table)


def upgrade():
    if _has_table("social_comments"):
        return
    op.create_table(
        "social_comments",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_id", sa.Integer(),
                  sa.ForeignKey("social_post_targets.id"), nullable=False,
                  index=True),
        sa.Column("platform", sa.String(length=30), nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("parent_external_id", sa.String(length=255), nullable=True),
        sa.Column("author_name", sa.String(length=255), nullable=True),
        sa.Column("author_id", sa.String(length=255), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("created_time", sa.String(length=40), nullable=True),
        sa.Column("is_ours", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("replied", sa.Boolean(), nullable=False,
                  server_default=sa.text("false")),
        sa.Column("status", sa.String(length=20), nullable=False,
                  server_default="open"),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False,
                  server_default=sa.text("now()")),
        sa.UniqueConstraint("platform", "external_id",
                            name="uq_social_comment_platform_external"),
    )
    with op.batch_alter_table("social_comments", schema=None) as b:
        b.create_index(b.f("ix_social_comments_status"), ["status"], unique=False)


def downgrade():
    if _has_table("social_comments"):
        op.drop_table("social_comments")
