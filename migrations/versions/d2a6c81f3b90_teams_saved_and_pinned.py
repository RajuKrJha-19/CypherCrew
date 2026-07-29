"""Saved messages and pinned messages.

Two shapes for two different things, which is the whole reason they are
not one feature:

  * A PIN belongs to a channel. A message lives in exactly one channel, so
    it can be pinned in exactly one place - two columns on the message say
    that precisely, and a join table would model a relationship that
    cannot exist.
  * A SAVE belongs to a person. Everyone has their own list, so it needs a
    row per person either way, and it gets a table.

The pin index is PARTIAL. A channel has a handful of pins among tens of
thousands of messages; indexing every row to find five of them would be a
second copy of the table.

Revision ID: d2a6c81f3b90
Revises: c1f9b47e02a5
"""

import sqlalchemy as sa
from alembic import op

revision = "d2a6c81f3b90"
down_revision = "c1f9b47e02a5"
branch_labels = None
depends_on = None

MESSAGES = "teams_messages"
SAVED = "teams_saved_messages"
PIN_INDEX = "ix_teams_messages_pinned"

PIN_COLUMNS = (
    ("pinned_at", sa.DateTime(), {"nullable": True}),
    ("pinned_by_id", sa.Integer(), {"nullable": True}),
)


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if MESSAGES in tables:
        present = {c["name"] for c in inspector.get_columns(MESSAGES)}
        for name, type_, kwargs in PIN_COLUMNS:
            if name not in present:
                op.add_column(MESSAGES, sa.Column(name, type_, **kwargs))

        keys = {fk["name"] for fk in inspector.get_foreign_keys(MESSAGES)}
        if "fk_teams_messages_pinned_by" not in keys:
            op.create_foreign_key(
                "fk_teams_messages_pinned_by", MESSAGES, "users",
                ["pinned_by_id"], ["id"], ondelete="SET NULL")

        if not any(i["name"] == PIN_INDEX for i in inspector.get_indexes(MESSAGES)):
            op.execute(sa.text(
                f"CREATE INDEX {PIN_INDEX} ON {MESSAGES} (channel_id) "
                f"WHERE pinned_at IS NOT NULL"
            ))

    if SAVED not in tables:
        op.create_table(
            SAVED,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("message_id", sa.Integer(), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["message_id"], [f"{MESSAGES}.id"],
                                    ondelete="CASCADE"),
            sa.UniqueConstraint("user_id", "message_id", name="uq_teams_saved"),
        )
        op.create_index("ix_teams_saved_user", SAVED, ["user_id", "id"])


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    tables = set(inspector.get_table_names())

    if SAVED in tables:
        op.drop_table(SAVED)

    if MESSAGES in tables:
        if any(i["name"] == PIN_INDEX for i in inspector.get_indexes(MESSAGES)):
            op.execute(sa.text(f"DROP INDEX {PIN_INDEX}"))

        keys = {fk["name"] for fk in inspector.get_foreign_keys(MESSAGES)}
        if "fk_teams_messages_pinned_by" in keys:
            op.drop_constraint("fk_teams_messages_pinned_by", MESSAGES,
                               type_="foreignkey")

        present = {c["name"] for c in inspector.get_columns(MESSAGES)}
        for name, _type, _kwargs in reversed(PIN_COLUMNS):
            if name in present:
                op.drop_column(MESSAGES, name)
