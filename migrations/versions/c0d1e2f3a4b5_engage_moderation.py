"""Engage spam moderation: removed-comment metadata + auto-mod settings.

Additive + inspector-guarded. Adds moderation columns to social_comments, the
spam auto-mod controls to ai_settings, and a per-client opt-in. Existing rows
keep working (all new columns nullable or defaulted).

Revision ID: c0d1e2f3a4b5
Revises: b9c0d1e2f3a4
"""

import sqlalchemy as sa
from alembic import op

revision = "c0d1e2f3a4b5"
down_revision = "b9c0d1e2f3a4"
branch_labels = None
depends_on = None


def _cols(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "social_comments" in tables:
        cols = _cols(inspector, "social_comments")
        adds = [
            ("removed_at", sa.Column("removed_at", sa.DateTime(), nullable=True)),
            ("removed_by_id", sa.Column(
                "removed_by_id", sa.Integer(),
                sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)),
            ("removal_kind", sa.Column("removal_kind", sa.String(10), nullable=True)),
            ("removal_reason", sa.Column("removal_reason", sa.String(255), nullable=True)),
            ("removal_action", sa.Column("removal_action", sa.String(10), nullable=True)),
        ]
        for name, col in adds:
            if name not in cols:
                op.add_column("social_comments", col)

    if "ai_settings" in tables:
        cols = _cols(inspector, "ai_settings")
        if "comment_automod_enabled" not in cols:
            op.add_column("ai_settings", sa.Column(
                "comment_automod_enabled", sa.Boolean(), nullable=False,
                server_default=sa.text("false")))
        if "spam_blocklist" not in cols:
            op.add_column("ai_settings", sa.Column("spam_blocklist", sa.Text(),
                                                   nullable=True))
        if "spam_hide_links" not in cols:
            op.add_column("ai_settings", sa.Column(
                "spam_hide_links", sa.Boolean(), nullable=False,
                server_default=sa.text("true")))
        if "automod_max_per_run" not in cols:
            op.add_column("ai_settings", sa.Column(
                "automod_max_per_run", sa.Integer(), nullable=False,
                server_default="20"))

    if "clients" in tables:
        cols = _cols(inspector, "clients")
        if "comment_automod" not in cols:
            op.add_column("clients", sa.Column(
                "comment_automod", sa.Boolean(), nullable=False,
                server_default=sa.text("false")))


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if "social_comments" in tables:
        cols = _cols(inspector, "social_comments")
        for name in ("removal_action", "removal_reason", "removal_kind",
                     "removed_by_id", "removed_at"):
            if name in cols:
                op.drop_column("social_comments", name)

    if "ai_settings" in tables:
        cols = _cols(inspector, "ai_settings")
        for name in ("automod_max_per_run", "spam_hide_links", "spam_blocklist",
                     "comment_automod_enabled"):
            if name in cols:
                op.drop_column("ai_settings", name)

    if "clients" in tables:
        cols = _cols(inspector, "clients")
        if "comment_automod" in cols:
            op.drop_column("clients", "comment_automod")
