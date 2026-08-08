"""TaskFile.preview_key / preview_state — small faststart video preview.

A persistent 720p, faststart web preview of a video task file, so playback
starts fast instead of buffering the full-resolution source. Additive +
guarded; preview_state defaults to 'pending' with a server_default so existing
rows are valid. Nothing references these until a preview is generated, and the
player always falls back to the original when there is none.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
"""

import sqlalchemy as sa
from alembic import op

revision = "f9a0b1c2d3e4"
down_revision = "e8f9a0b1c2d3"
branch_labels = None
depends_on = None

_TABLE = "task_files"
_IX = "ix_task_files_preview_state"


def _cols(inspector):
    return {c["name"] for c in inspector.get_columns(_TABLE)}


def _indexes(inspector):
    return {i["name"] for i in inspector.get_indexes(_TABLE)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    cols = _cols(inspector)
    if "preview_key" not in cols:
        op.add_column(_TABLE, sa.Column("preview_key", sa.String(length=1000),
                                        nullable=True))
    if "preview_state" not in cols:
        op.add_column(_TABLE, sa.Column(
            "preview_state", sa.String(length=16), nullable=False,
            server_default="pending"))
    if _IX not in _indexes(inspector):
        op.create_index(_IX, _TABLE, ["preview_state"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if _TABLE not in set(inspector.get_table_names()):
        return
    if _IX in _indexes(inspector):
        op.drop_index(_IX, table_name=_TABLE)
    cols = _cols(inspector)
    if "preview_state" in cols:
        op.drop_column(_TABLE, "preview_state")
    if "preview_key" in cols:
        op.drop_column(_TABLE, "preview_key")
