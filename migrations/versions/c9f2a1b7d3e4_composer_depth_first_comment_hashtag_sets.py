"""Composer depth: first_comment on targets + saved hashtag sets.

Additive only:
  - social_post_targets.first_comment (nullable TEXT)
  - social_hashtag_sets (new table)

Revision ID: c9f2a1b7d3e4
Revises: b7e1c2a4d9f3
Create Date: 2026-07-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "c9f2a1b7d3e4"
down_revision = "b7e1c2a4d9f3"
branch_labels = None
depends_on = None


def _has_column(table, column):
    bind = op.get_bind()
    insp = sa.inspect(bind)
    return column in {c["name"] for c in insp.get_columns(table)}


def _has_table(table):
    bind = op.get_bind()
    return sa.inspect(bind).has_table(table)


def upgrade():
    if not _has_column("social_post_targets", "first_comment"):
        op.add_column(
            "social_post_targets",
            sa.Column("first_comment", sa.Text(), nullable=True),
        )

    if not _has_table("social_hashtag_sets"):
        op.create_table(
            "social_hashtag_sets",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("client_id", sa.Integer(),
                      sa.ForeignKey("clients.id"), nullable=True, index=True),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("hashtags", sa.Text(), nullable=False),
            sa.Column("created_by_id", sa.Integer(),
                      sa.ForeignKey("users.id"), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
        )


def downgrade():
    if _has_table("social_hashtag_sets"):
        op.drop_table("social_hashtag_sets")
    if _has_column("social_post_targets", "first_comment"):
        op.drop_column("social_post_targets", "first_comment")
