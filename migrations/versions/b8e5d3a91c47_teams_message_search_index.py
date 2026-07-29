"""Full-text index for chat search.

A GIN index over to_tsvector(body). Without it every search is a
sequential scan of teams_messages, which is fine on the day it ships and
is not fine a year in - and search is the one query somebody runs while
staring at the screen waiting for it.

The configuration is 'simple' rather than 'english', and it MUST match
messages.SEARCH_CONFIG exactly: Postgres will only use this index for a
query whose configuration is the same, and a mismatch does not error, it
just silently falls back to the scan this index exists to prevent.

Expression indexes need the expression to be immutable, which is why the
configuration is written as a literal here rather than left to
default_text_search_config.

Revision ID: b8e5d3a91c47
Revises: a7c4e2f81d36
"""

import sqlalchemy as sa
from alembic import op

revision = "b8e5d3a91c47"
down_revision = "a7c4e2f81d36"
branch_labels = None
depends_on = None

INDEX = "ix_teams_messages_body_fts"
TABLE = "teams_messages"


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        return
    if any(i["name"] == INDEX for i in inspector.get_indexes(TABLE)):
        return

    op.execute(
        sa.text(
            f"CREATE INDEX {INDEX} ON {TABLE} "
            f"USING GIN (to_tsvector('simple', coalesce(body, '')))"
        )
    )


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if TABLE not in inspector.get_table_names():
        return
    if any(i["name"] == INDEX for i in inspector.get_indexes(TABLE)):
        op.execute(sa.text(f"DROP INDEX {INDEX}"))
