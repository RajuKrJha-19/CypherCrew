"""Cypher-Teams: channels, messages, presence, and joinable meetings.

Six new tables plus additive columns on `meetings`. Nothing existing is
dropped or renamed: every added column is nullable or carries a server
default, so the rows that predate Teams stay valid and the current readers
of `meetings` (the calendar, the dashboard "Upcoming" panel, the meetings
list) keep working untouched.

The index that matters is ix_teams_messages_channel_id on
(channel_id, id). Chat is polled, so that one composite serves the delta
query, the unread count and the initial page - all the same access pattern -
and it is what keeps a 2-second poll off the CPU.

Guarded throughout so a partial run is safe to repeat.

Revision ID: a3d81f5c2b74
Revises: e91c4d7ab820
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "a3d81f5c2b74"
down_revision = "e91c4d7ab820"
branch_labels = None
depends_on = None


CHANNELS = "teams_channels"
MEMBERS = "teams_channel_members"
MESSAGES = "teams_messages"
ATTACHMENTS = "teams_message_attachments"
REACTIONS = "teams_message_reactions"
PRESENCE = "teams_presence"
TYPING = "teams_typing"

#: (column, type, kwargs) added to `meetings`.
MEETING_COLUMNS = (
    ("room_key", sa.String(length=64), {"nullable": True}),
    ("duration_minutes", sa.Integer(),
     {"nullable": False, "server_default": "30"}),
    ("status", sa.String(length=20),
     {"nullable": False, "server_default": "scheduled"}),
    ("created_by_id", sa.Integer(), {"nullable": True}),
    ("channel_id", sa.Integer(), {"nullable": True}),
    ("provider", sa.String(length=30),
     {"nullable": False, "server_default": "jitsi"}),
    ("started_at", sa.DateTime(), {"nullable": True}),
    ("ended_at", sa.DateTime(), {"nullable": True}),
)


def _tables(inspector):
    return set(inspector.get_table_names())


def _columns(inspector, table):
    return {c["name"] for c in inspector.get_columns(table)}


def upgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = _tables(inspector)

    # ---- Channels -----------------------------------------------------
    if CHANNELS not in existing:
        op.create_table(
            CHANNELS,
            sa.Column("id", sa.Integer(), primary_key=True),
            # Unique because it is what makes a DM idempotent: two people
            # opening the same conversation at the same moment both compute
            # "dm:<lo>:<hi>" and the second insert loses here rather than
            # creating a second, half-populated conversation.
            sa.Column("key", sa.String(length=80), nullable=False),
            sa.Column("name", sa.String(length=80), nullable=True),
            sa.Column("description", sa.String(length=255), nullable=True),
            sa.Column("kind", sa.String(length=10), nullable=False,
                      server_default="channel"),
            sa.Column("visibility", sa.String(length=10), nullable=False,
                      server_default="public"),
            sa.Column("client_id", sa.Integer(), nullable=True),
            sa.Column("created_by_id", sa.Integer(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("archived_at", sa.DateTime(), nullable=True),
            # Denormalised newest-message pointers. Paired with a member's
            # last_read_message_id, "has unread" becomes a comparison of
            # two columns the sidebar query already has in hand - no touch
            # of teams_messages at all.
            sa.Column("last_message_at", sa.DateTime(), nullable=True),
            sa.Column("last_message_id", sa.Integer(), nullable=True),
            sa.ForeignKeyConstraint(["client_id"], ["clients.id"]),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"]),
            sa.UniqueConstraint("key", name="uq_teams_channels_key"),
        )
        op.create_index("ix_teams_channels_kind", CHANNELS, ["kind"])
        op.create_index("ix_teams_channels_client_id", CHANNELS, ["client_id"])
        op.create_index("ix_teams_channels_last_message_at", CHANNELS,
                        ["last_message_at"])

    # ---- Membership + read cursor --------------------------------------
    if MEMBERS not in existing:
        op.create_table(
            MEMBERS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("role", sa.String(length=10), nullable=False,
                      server_default="member"),
            sa.Column("joined_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            # The entire unread mechanism: one integer per person per
            # channel, instead of a read-receipt row per message which
            # would be the biggest table here inside a month.
            sa.Column("last_read_message_id", sa.Integer(), nullable=True),
            sa.Column("muted", sa.Boolean(), nullable=False,
                      server_default="false"),
            sa.ForeignKeyConstraint(["channel_id"], [f"{CHANNELS}.id"],
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="CASCADE"),
            sa.UniqueConstraint("channel_id", "user_id",
                                name="uq_teams_channel_member"),
        )
        # Ordered (user, channel): every read path starts from "my channels".
        op.create_index("ix_teams_channel_members_user", MEMBERS,
                        ["user_id", "channel_id"])

    # ---- Messages ------------------------------------------------------
    if MESSAGES not in existing:
        op.create_table(
            MESSAGES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("channel_id", sa.Integer(), nullable=False),
            # SET NULL, not CASCADE: removing an account must not take the
            # team's history with it.
            sa.Column("user_id", sa.Integer(), nullable=True),
            sa.Column("parent_id", sa.Integer(), nullable=True),
            sa.Column("thread_root_id", sa.Integer(), nullable=True),
            sa.Column("body", sa.Text(), nullable=True),
            sa.Column("kind", sa.String(length=10), nullable=False,
                      server_default="text"),
            sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()),
                      nullable=True),
            # Resolved once at write time. Resolving on read would put a
            # full scan of `users` (mentions._active_users builds a regex of
            # every active name) on every 2-second poll of every open tab.
            sa.Column("mention_user_ids",
                      postgresql.JSONB(astext_type=sa.Text()), nullable=True),
            sa.Column("client_msg_id", sa.String(length=40), nullable=True),
            sa.Column("reply_count", sa.Integer(), nullable=False,
                      server_default="0"),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("edited_at", sa.DateTime(), nullable=True),
            # The second cursor. An id cursor structurally cannot report an
            # edit, a delete or a reaction - none of them mint a new id -
            # so without this column a message deleted on one screen stays
            # on every other screen until someone reloads.
            sa.Column("updated_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            # Soft delete keeps the id sequence gap-free for the poll
            # cursor and lets the next delta tell open clients to drop the
            # bubble. A hard DELETE would just silently vanish.
            sa.Column("deleted_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["channel_id"], [f"{CHANNELS}.id"],
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["parent_id"], [f"{MESSAGES}.id"],
                                    ondelete="CASCADE"),
            # Idempotent send: a retried POST resolves to the message that
            # already exists instead of posting it a second time.
            sa.UniqueConstraint("channel_id", "client_msg_id",
                                name="uq_teams_messages_client_msg"),
        )
        # The one index the module rests on - see the module docstring.
        op.create_index("ix_teams_messages_channel_id", MESSAGES,
                        ["channel_id", "id"])
        # The change sweep that delivers edits, deletes and reactions.
        op.create_index("ix_teams_messages_channel_updated", MESSAGES,
                        ["channel_id", "updated_at"])
        op.create_index("ix_teams_messages_parent_id", MESSAGES, ["parent_id"])
        op.create_index("ix_teams_messages_thread_root_id", MESSAGES,
                        ["thread_root_id"])
        op.create_index("ix_teams_messages_user_id", MESSAGES, ["user_id"])

    if ATTACHMENTS not in existing:
        op.create_table(
            ATTACHMENTS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("message_id", sa.Integer(), nullable=False),
            sa.Column("object_key", sa.String(length=500), nullable=False),
            sa.Column("filename", sa.String(length=255), nullable=False),
            sa.Column("content_type", sa.String(length=120), nullable=True),
            sa.Column("size_bytes", sa.BigInteger(), nullable=True),
            sa.Column("width", sa.Integer(), nullable=True),
            sa.Column("height", sa.Integer(), nullable=True),
            sa.Column("thumbnail_key", sa.String(length=500), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["message_id"], [f"{MESSAGES}.id"],
                                    ondelete="CASCADE"),
        )
        op.create_index("ix_teams_message_attachments_message_id",
                        ATTACHMENTS, ["message_id"])

    if REACTIONS not in existing:
        op.create_table(
            REACTIONS,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("message_id", sa.Integer(), nullable=False),
            sa.Column("user_id", sa.Integer(), nullable=False),
            sa.Column("emoji", sa.String(length=32), nullable=False),
            sa.Column("created_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["message_id"], [f"{MESSAGES}.id"],
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="CASCADE"),
            # Makes the toggle idempotent in the database, where two fast
            # clicks cannot interleave through it.
            sa.UniqueConstraint("message_id", "user_id", "emoji",
                                name="uq_teams_reaction"),
        )
        op.create_index("ix_teams_reactions_message", REACTIONS,
                        ["message_id"])

    # ---- Presence + typing ---------------------------------------------
    # Separate tables rather than columns on `users`: these rows are
    # rewritten on every poll tick, and that churn on `users` would make
    # new row versions of the table every dashboard and people-picker reads.
    if PRESENCE not in existing:
        op.create_table(
            PRESENCE,
            sa.Column("user_id", sa.Integer(), primary_key=True),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.Column("status", sa.String(length=10), nullable=False,
                      server_default="online"),
            sa.Column("status_text", sa.String(length=80), nullable=True),
            sa.Column("updated_at", sa.DateTime(), nullable=False,
                      server_default=sa.text("now()")),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="CASCADE"),
        )
        op.create_index("ix_teams_presence_last_seen_at", PRESENCE,
                        ["last_seen_at"])

    if TYPING not in existing:
        op.create_table(
            TYPING,
            sa.Column("channel_id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), primary_key=True),
            # Expiry instead of an explicit "stopped typing" call - the
            # browser that closes mid-sentence never sends one.
            sa.Column("expires_at", sa.DateTime(), nullable=False),
            sa.ForeignKeyConstraint(["channel_id"], [f"{CHANNELS}.id"],
                                    ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"],
                                    ondelete="CASCADE"),
        )
        op.create_index("ix_teams_typing_expires_at", TYPING, ["expires_at"])

    # ---- meetings: additive columns -------------------------------------
    inspector = sa.inspect(bind)
    if "meetings" in _tables(inspector):
        present = _columns(inspector, "meetings")
        for name, type_, kwargs in MEETING_COLUMNS:
            if name not in present:
                op.add_column("meetings", sa.Column(name, type_, **kwargs))

        present = _columns(inspector, "meetings")
        constraints = {
            c["name"] for c in inspector.get_unique_constraints("meetings")
        }
        if "room_key" in present and "uq_meetings_room_key" not in constraints:
            op.create_unique_constraint(
                "uq_meetings_room_key", "meetings", ["room_key"])

        keys = {fk["name"] for fk in inspector.get_foreign_keys("meetings")}
        if "created_by_id" in present and "fk_meetings_created_by" not in keys:
            op.create_foreign_key(
                "fk_meetings_created_by", "meetings", "users",
                ["created_by_id"], ["id"])
        if "channel_id" in present and "fk_meetings_channel" not in keys:
            op.create_foreign_key(
                "fk_meetings_channel", "meetings", CHANNELS,
                ["channel_id"], ["id"], ondelete="SET NULL")


def downgrade():
    bind = op.get_bind()
    inspector = sa.inspect(bind)

    if "meetings" in _tables(inspector):
        keys = {fk["name"] for fk in inspector.get_foreign_keys("meetings")}
        if "fk_meetings_channel" in keys:
            op.drop_constraint("fk_meetings_channel", "meetings",
                               type_="foreignkey")
        if "fk_meetings_created_by" in keys:
            op.drop_constraint("fk_meetings_created_by", "meetings",
                               type_="foreignkey")

        constraints = {
            c["name"] for c in inspector.get_unique_constraints("meetings")
        }
        if "uq_meetings_room_key" in constraints:
            op.drop_constraint("uq_meetings_room_key", "meetings",
                               type_="unique")

        present = _columns(inspector, "meetings")
        for name, _type, _kwargs in reversed(MEETING_COLUMNS):
            if name in present:
                op.drop_column("meetings", name)

    # Children before parents.
    for table in (TYPING, PRESENCE, REACTIONS, ATTACHMENTS, MESSAGES,
                  MEMBERS, CHANNELS):
        inspector = sa.inspect(bind)
        if table in _tables(inspector):
            op.drop_table(table)
