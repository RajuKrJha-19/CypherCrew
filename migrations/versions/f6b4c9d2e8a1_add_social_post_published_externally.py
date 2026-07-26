"""Add social_posts.published_externally.

Additive only: one new boolean column (default false). Flags a post that was
published directly on the platform, outside Social Studio, so the Studio's
Published list and the originating task both reflect it.

Revision ID: f6b4c9d2e8a1
Revises: e5a3b8c2f1d7
Create Date: 2026-07-27
"""
from alembic import op
import sqlalchemy as sa


revision = "f6b4c9d2e8a1"
down_revision = "e5a3b8c2f1d7"
branch_labels = None
depends_on = None


def _has_column(table, column):
    insp = sa.inspect(op.get_bind())
    return column in {c["name"] for c in insp.get_columns(table)}


def upgrade():
    if _has_column("social_posts", "published_externally"):
        return
    op.add_column(
        "social_posts",
        sa.Column("published_externally", sa.Boolean(), nullable=False,
                  server_default="false"),
    )


def downgrade():
    if _has_column("social_posts", "published_externally"):
        op.drop_column("social_posts", "published_externally")
