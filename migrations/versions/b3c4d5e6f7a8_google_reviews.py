"""Google Business Profile: google_reviews table (reply inbox + auto-reply).

Additive. With GBP_REVIEWS_ENABLED off nothing writes here.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""

import sqlalchemy as sa
from alembic import op

revision = "b3c4d5e6f7a8"
down_revision = "a2b3c4d5e6f7"
branch_labels = None
depends_on = None

TABLE = "google_reviews"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    # Per-client guarded-auto-reply opt-in (additive, default off).
    if "clients" in set(inspector.get_table_names()):
        if "gmb_autoreply" not in {c["name"] for c in inspector.get_columns("clients")}:
            op.add_column("clients", sa.Column(
                "gmb_autoreply", sa.Boolean(), nullable=False,
                server_default=sa.false()))

    if TABLE in set(inspector.get_table_names()):
        return
    op.create_table(
        "google_reviews",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("account_id", sa.Integer(),
                  sa.ForeignKey("social_accounts.id", ondelete="CASCADE"),
                  nullable=False),
        sa.Column("external_id", sa.String(length=255), nullable=False),
        sa.Column("reviewer_name", sa.String(length=150), nullable=True),
        sa.Column("rating", sa.Integer(), nullable=True),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("review_created_at", sa.DateTime(), nullable=True),
        sa.Column("fetched_at", sa.DateTime(), nullable=True),
        sa.Column("reply_status", sa.String(length=20), nullable=False,
                  server_default="pending"),
        sa.Column("reply_text", sa.Text(), nullable=True),
        sa.Column("reply_ai_generated", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("auto_sent", sa.Boolean(), nullable=False,
                  server_default=sa.false()),
        sa.Column("replied_at", sa.DateTime(), nullable=True),
        sa.Column("replied_by_id", sa.Integer(),
                  sa.ForeignKey("users.id", ondelete="SET NULL"),
                  nullable=True),
        sa.UniqueConstraint("account_id", "external_id",
                            name="uq_google_review_account_external"),
    )
    op.create_index("ix_google_reviews_account_id", "google_reviews",
                    ["account_id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())
    if TABLE in tables:
        op.drop_table("google_reviews")
    if "clients" in tables:
        if "gmb_autoreply" in {c["name"] for c in inspector.get_columns("clients")}:
            op.drop_column("clients", "gmb_autoreply")
