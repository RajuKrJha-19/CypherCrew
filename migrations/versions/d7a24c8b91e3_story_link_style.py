"""Story style: plain, or tappable through to a feed post.

Instagram's Content Publishing API accepts image_url/video_url for a
STORIES container and nothing else - no sticker, no link. So a story that
is supposed to open a post is published like any other story and the
sticker is added by hand in the app afterwards. These columns record the
intent, the post it should open, and who closed the loop.

Written defensively (inspector-guarded) so it is safe to re-run against a
database where part of it already landed.

Revision ID: d7a24c8b91e3
Revises: c3f7a91b60d4
"""

import sqlalchemy as sa
from alembic import op

revision = "d7a24c8b91e3"
down_revision = "c3f7a91b60d4"
branch_labels = None
depends_on = None

TABLE = "social_post_targets"


def _columns(inspector):
    return {c["name"] for c in inspector.get_columns(TABLE)}


def upgrade():
    inspector = sa.inspect(op.get_bind())
    existing = _columns(inspector)

    with op.batch_alter_table(TABLE) as batch:
        if "story_style" not in existing:
            # server_default so the rows already in the table become
            # "plain" without a separate backfill pass.
            batch.add_column(sa.Column(
                "story_style", sa.String(length=20),
                nullable=False, server_default="plain",
            ))
        if "story_link_target_id" not in existing:
            batch.add_column(sa.Column(
                "story_link_target_id", sa.Integer(), nullable=True,
            ))
        if "story_link_done_at" not in existing:
            batch.add_column(sa.Column(
                "story_link_done_at", sa.DateTime(), nullable=True,
            ))
        if "story_link_done_by_id" not in existing:
            batch.add_column(sa.Column(
                "story_link_done_by_id", sa.Integer(), nullable=True,
            ))

    inspector = sa.inspect(op.get_bind())
    fks = {fk.get("name") for fk in inspector.get_foreign_keys(TABLE)}

    # Self-referential: the story points at the feed target it should open.
    if "fk_spt_story_link_target" not in fks:
        op.create_foreign_key(
            "fk_spt_story_link_target", TABLE, TABLE,
            ["story_link_target_id"], ["id"], ondelete="SET NULL",
        )
    if "fk_spt_story_link_done_by" not in fks:
        op.create_foreign_key(
            "fk_spt_story_link_done_by", TABLE, "users",
            ["story_link_done_by_id"], ["id"], ondelete="SET NULL",
        )

    # Partial index: the Studio only ever asks "what still needs a
    # sticker?", which is a tiny slice of a table that grows forever.
    indexes = {ix["name"] for ix in inspector.get_indexes(TABLE)}
    if "ix_spt_story_link_pending" not in indexes:
        op.create_index(
            "ix_spt_story_link_pending", TABLE, ["status"],
            postgresql_where=sa.text(
                "story_style = 'post_link' AND story_link_done_at IS NULL"
            ),
        )


def downgrade():
    inspector = sa.inspect(op.get_bind())

    indexes = {ix["name"] for ix in inspector.get_indexes(TABLE)}
    if "ix_spt_story_link_pending" in indexes:
        op.drop_index("ix_spt_story_link_pending", table_name=TABLE)

    fks = {fk.get("name") for fk in inspector.get_foreign_keys(TABLE)}
    for name in ("fk_spt_story_link_target", "fk_spt_story_link_done_by"):
        if name in fks:
            op.drop_constraint(name, TABLE, type_="foreignkey")

    existing = _columns(inspector)
    with op.batch_alter_table(TABLE) as batch:
        for column in ("story_link_done_by_id", "story_link_done_at",
                       "story_link_target_id", "story_style"):
            if column in existing:
                batch.drop_column(column)
