"""AI settings control panel: caption behaviour, image/perf, and admin-editable
Google review auto-reply guardrails.

Additive + inspector-guarded. Every column ships with a default equal to the
prior behaviour, so nothing changes until an admin edits it.

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
"""

import sqlalchemy as sa
from alembic import op

revision = "f7a8b9c0d1e2"
down_revision = "e6f7a8b9c0d1"
branch_labels = None
depends_on = None

# (name, column) - each additive with a behaviour-preserving default.
_COLS = [
    ("caption_tone", sa.Column("caption_tone", sa.String(length=20), nullable=True)),
    ("caption_variations", sa.Column("caption_variations", sa.Integer(),
                                     nullable=False, server_default="2")),
    ("caption_hashtags", sa.Column("caption_hashtags", sa.Boolean(),
                                   nullable=False, server_default=sa.true())),
    ("image_max_dim", sa.Column("image_max_dim", sa.Integer(),
                                nullable=False, server_default="1568")),
    ("media_max_mb", sa.Column("media_max_mb", sa.Integer(),
                               nullable=False, server_default="10")),
    ("gbp_autoreply_enabled", sa.Column("gbp_autoreply_enabled", sa.Boolean(),
                                        nullable=False, server_default=sa.false())),
    ("gbp_min_rating", sa.Column("gbp_min_rating", sa.Integer(),
                                 nullable=False, server_default="4")),
    ("gbp_max_len", sa.Column("gbp_max_len", sa.Integer(),
                              nullable=False, server_default="200")),
    ("gbp_max_per_run", sa.Column("gbp_max_per_run", sa.Integer(),
                                  nullable=False, server_default="10")),
    ("gbp_blocklist", sa.Column("gbp_blocklist", sa.Text(), nullable=True)),
]


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" not in set(inspector.get_table_names()):
        return
    have = {c["name"] for c in inspector.get_columns("ai_settings")}
    for name, col in _COLS:
        if name not in have:
            op.add_column("ai_settings", col)


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if "ai_settings" not in set(inspector.get_table_names()):
        return
    have = {c["name"] for c in inspector.get_columns("ai_settings")}
    for name, _col in reversed(_COLS):
        if name in have:
            op.drop_column("ai_settings", name)
