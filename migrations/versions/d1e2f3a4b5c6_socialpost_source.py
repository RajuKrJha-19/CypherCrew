"""SocialPost.source discriminator (studio | ad).

Additive + inspector-guarded. Existing rows default to "studio"; synthetic
ad-comment posts are tagged "ad" and excluded from every Studio list/report.

Revision ID: d1e2f3a4b5c6
Revises: c0d1e2f3a4b5
"""

import sqlalchemy as sa
from alembic import op

revision = "d1e2f3a4b5c6"
down_revision = "c0d1e2f3a4b5"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "social_posts" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("social_posts")}
    if "source" not in cols:
        op.add_column("social_posts", sa.Column(
            "source", sa.String(20), nullable=False,
            server_default="studio"))
        op.create_index("ix_social_posts_source", "social_posts", ["source"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "social_posts" not in set(inspector.get_table_names()):
        return
    cols = {c["name"] for c in inspector.get_columns("social_posts")}
    if "source" in cols:
        try:
            op.drop_index("ix_social_posts_source", "social_posts")
        except Exception:  # noqa: BLE001
            pass
        op.drop_column("social_posts", "source")
