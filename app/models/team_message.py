"""Cypher-Teams messages, their attachments and their reactions.

There are TWO cursors, and both are necessary:

  `id`         - "give me everything after N". Monotonic, so it can only
                 ever report messages that did not exist before.
  `updated_at` - "give me everything that CHANGED since T". An id cursor
                 structurally cannot report an edit, a soft delete or a new
                 reaction, because none of those mint a new id. Without
                 this second sweep a message deleted on one screen stays on
                 every other screen until the page is reloaded.

Hence also: messages are never hard-deleted. A gap in the id sequence is
harmless, but a row that vanishes between two polls can never be reported
as gone.
"""

from datetime import datetime

from sqlalchemy.dialects.postgresql import JSONB

from app.extensions import db


class TeamMessage(db.Model):
    """One message in a channel or DM. Replies point at their root."""

    __tablename__ = "teams_messages"

    id = db.Column(db.Integer, primary_key=True)

    channel_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_channels.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: Nullable + SET NULL: a removed account must not take the team's
    #: history with it. The UI renders these as "Deleted user".
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    #: Thread root. NULL = a top-level message. Only one level deep, like
    #: the task comment thread - replies to replies flatten onto the root.
    parent_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_messages.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )

    #: Denormalised thread root (equal to parent_id at one level of nesting,
    #: NULL on roots themselves). Fetching a thread is then one indexed
    #: range scan instead of a recursive walk, and it stays correct if
    #: deeper nesting is ever allowed.
    thread_root_id = db.Column(db.Integer, nullable=True, index=True)

    body = db.Column(db.Text, nullable=True)

    # text    - somebody typed it
    # system  - "X joined the channel", rendered without a bubble
    # meeting - a meeting card, details in meta
    kind = db.Column(db.String(10), nullable=False, default="text")

    #: Payload for non-text kinds (meeting id, joined user id, ...). JSONB so
    #: a new system message never needs a migration.
    meta = db.Column(JSONB, nullable=True)

    #: Users mentioned here, resolved ONCE when the message is written.
    #: Never resolved on read: app/utils/mentions._active_users() loads every
    #: active user and builds a regex of all their names, and doing that
    #: inside a 2-second poll would put a full users scan on every tick of
    #: every open tab.
    mention_user_ids = db.Column(JSONB, nullable=True)

    #: Client-generated id for the optimistic bubble. Unique per channel, so
    #: a double-submit or a POST retried after a timeout resolves to the
    #: message that already exists instead of posting it twice.
    client_msg_id = db.Column(db.String(40), nullable=True)

    #: Denormalised onto the root so the thread indicator ("3 replies")
    #: costs nothing to render in the message list.
    reply_count = db.Column(
        db.Integer, nullable=False, default=0, server_default="0"
    )

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)
    edited_at = db.Column(db.DateTime, nullable=True)

    #: The second cursor (see the module docstring). Every mutation that an
    #: open client must learn about - edit, soft delete, reaction added or
    #: removed - has to touch this. Reactions live in another table, so
    #: their service bumps it explicitly rather than relying on `onupdate`.
    updated_at = db.Column(
        db.DateTime, nullable=False,
        default=datetime.utcnow, onupdate=datetime.utcnow,
    )

    #: Soft delete. The row survives so the cursor stays monotonic and the
    #: next poll can tell open clients to remove the bubble; `body` is
    #: cleared at the same time so the text is genuinely gone.
    deleted_at = db.Column(db.DateTime, nullable=True)

    user = db.relationship("User")

    #: Deliberately lazy. Search needs it to say which conversation a hit
    #: came from, but the delta query runs every couple of seconds on every
    #: open tab - putting lazy="joined" here would add a join to teams_
    #: channels on the hottest query in the module to serve one page.
    #: services/messages.search eager-loads it explicitly instead.
    channel = db.relationship("TeamChannel")

    replies = db.relationship(
        "TeamMessage",
        backref=db.backref("parent", remote_side=[id]),
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    attachments = db.relationship(
        "TeamAttachment",
        backref="message",
        cascade="all, delete-orphan",
        lazy="joined",
    )

    reactions = db.relationship(
        "TeamReaction",
        backref="message",
        cascade="all, delete-orphan",
        lazy="joined",
    )

    __table_args__ = (
        # The index the whole module rests on. It serves the delta query
        # (channel_id = ? AND id > ?), the unread count, and the initial
        # backwards page - all three are the same access pattern.
        db.Index("ix_teams_messages_channel_id", "channel_id", "id"),
        # The change sweep: channel_id = ? AND updated_at > ?.
        db.Index("ix_teams_messages_channel_updated",
                 "channel_id", "updated_at"),
        # Idempotent send. Scoped to the channel rather than global so a
        # client is free to generate ids however it likes.
        db.UniqueConstraint("channel_id", "client_msg_id",
                            name="uq_teams_messages_client_msg"),
    )

    @property
    def is_deleted(self):
        return self.deleted_at is not None

    def __repr__(self):
        return f"<TeamMessage {self.id} c{self.channel_id}>"


class TeamAttachment(db.Model):
    """A file or image on a message, stored in R2 like every other upload."""

    __tablename__ = "teams_message_attachments"

    id = db.Column(db.Integer, primary_key=True)

    message_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    #: R2 object key: teams/channels/<channel_id>/<yyyy>/<mm>/<uuid>-<name>
    object_key = db.Column(db.String(500), nullable=False)

    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(120), nullable=True)
    size_bytes = db.Column(db.BigInteger, nullable=True)

    #: Present for images, so the bubble can reserve the right box before
    #: the presigned URL resolves and the message list stops jumping.
    width = db.Column(db.Integer, nullable=True)
    height = db.Column(db.Integer, nullable=True)
    thumbnail_key = db.Column(db.String(500), nullable=True)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    @property
    def is_image(self):
        """Whether the bubble renders this inline or as a chip.

        Reads the STORED content type, which StorageService has already
        sanitised - so an SVG uploaded as `image/svg+xml` arrives here as
        `application/octet-stream` and correctly renders as a file rather
        than as an <img> pointing at a script.
        """
        return (self.content_type or "").lower().startswith("image/")

    def __repr__(self):
        return f"<TeamAttachment {self.id} {self.filename}>"


class TeamReaction(db.Model):
    """One emoji, from one person, on one message."""

    __tablename__ = "teams_message_reactions"

    id = db.Column(db.Integer, primary_key=True)

    message_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_messages.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    #: The literal emoji character(s). 32 chars covers multi-codepoint
    #: sequences (skin tones, ZWJ families) with room to spare.
    emoji = db.Column(db.String(32), nullable=False)

    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    user = db.relationship("User")

    __table_args__ = (
        # Makes the toggle idempotent in the database rather than in a
        # read-then-write that two fast clicks can interleave through.
        db.UniqueConstraint(
            "message_id", "user_id", "emoji", name="uq_teams_reaction"
        ),
        db.Index("ix_teams_reactions_message", "message_id"),
    )

    def __repr__(self):
        return f"<TeamReaction {self.emoji} m{self.message_id}>"
