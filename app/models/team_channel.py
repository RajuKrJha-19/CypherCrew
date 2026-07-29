"""Cypher-Teams conversations and who is in them.

A DM is a channel. That is the one decision the rest of the module leans on:
one message table, one unread mechanism, one polling endpoint, one set of
indexes. The alternative - a parallel Conversation model - would have doubled
every query in services/ for no behavioural gain.
"""

from datetime import datetime

from app.extensions import db


class TeamChannel(db.Model):
    """A place people talk: a named channel, or a two-person DM."""

    __tablename__ = "teams_channels"

    id = db.Column(db.Integer, primary_key=True)

    #: Stable URL/lookup slug. For channels it is the handle typed at
    #: creation ("general", "design-team"). For DMs it is "dm:<lo>:<hi>"
    #: built from the sorted user-id pair - the UNIQUE constraint here is
    #: what actually prevents a duplicate DM when two people open the
    #: conversation at the same instant. An application-level "does it
    #: exist?" check cannot, because both requests read before either writes.
    key = db.Column(db.String(80), nullable=False)

    #: Display name. NULL for DMs, which are titled from the other member.
    name = db.Column(db.String(80), nullable=True)
    description = db.Column(db.String(255), nullable=True)

    # channel | dm
    kind = db.Column(
        db.String(10), nullable=False, default="channel", index=True
    )

    # public  - any signed-in member of staff can find and join it
    # private - invite only; non-members cannot read it or see it exists
    # DMs are always private.
    visibility = db.Column(db.String(10), nullable=False, default="public")

    #: Optional link to a client, so a channel can sit alongside that
    #: client's tasks and Studio posts. Nullable = an internal channel.
    client_id = db.Column(
        db.Integer, db.ForeignKey("clients.id"), nullable=True, index=True
    )

    created_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id"), nullable=True
    )
    created_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    #: Archived channels stay readable but drop out of the sidebar and
    #: refuse new messages. Deliberately not a hard delete - the history is
    #: usually the reason anyone wants the channel back.
    archived_at = db.Column(db.DateTime, nullable=True)

    #: Denormalised from the newest message so the sidebar can order
    #: conversations without joining teams_messages on every request.
    last_message_at = db.Column(db.DateTime, nullable=True, index=True)

    #: The newest message id, denormalised for the same reason but doing
    #: more work: paired with TeamChannelMember.last_read_message_id it
    #: turns "does this channel have unread?" into an integer comparison
    #: between two columns the sidebar query has already loaded. The exact
    #: count still costs a scan, so channels get a dot and only DMs and
    #: mentions - where the ranges are short because people read them -
    #: get a number.
    last_message_id = db.Column(db.Integer, nullable=True)

    __table_args__ = (
        # Named explicitly so the model and the migration agree - an
        # anonymous unique=True leaves Postgres to invent the name, and the
        # next autogenerate then wants to drop and recreate it.
        db.UniqueConstraint("key", name="uq_teams_channels_key"),
    )

    client = db.relationship("Client")
    created_by = db.relationship("User", foreign_keys=[created_by_id])

    members = db.relationship(
        "TeamChannelMember",
        backref="channel",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )

    @property
    def is_dm(self):
        return self.kind == "dm"

    @property
    def is_archived(self):
        return self.archived_at is not None

    @staticmethod
    def dm_key(user_id_a, user_id_b):
        """The canonical key for a DM between two users, in either order."""
        lo, hi = sorted((int(user_id_a), int(user_id_b)))
        return f"dm:{lo}:{hi}"

    def display_name(self, viewer=None):
        """Channel name, or - for a DM - the other person's name."""
        if not self.is_dm:
            return self.name or self.key
        other = self.other_member(viewer)
        return other.name if other else "Direct message"

    def other_member(self, viewer):
        """The User on the far side of a DM, from `viewer`'s point of view."""
        if not self.is_dm or viewer is None:
            return None
        for member in self.members:
            if member.user_id != viewer.id:
                return member.user
        # A DM with yourself (notes to self) - the far side is you.
        first = self.members.first()
        return first.user if first else None

    def __repr__(self):
        return f"<TeamChannel {self.id} {self.key}>"


class TeamChannelMember(db.Model):
    """Membership, and the read cursor that drives every unread badge."""

    __tablename__ = "teams_channel_members"

    id = db.Column(db.Integer, primary_key=True)

    channel_id = db.Column(
        db.Integer,
        db.ForeignKey("teams_channels.id", ondelete="CASCADE"),
        nullable=False,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )

    # owner  - can rename, archive, and manage membership
    # member - can read and post
    role = db.Column(db.String(10), nullable=False, default="member")

    joined_at = db.Column(db.DateTime, nullable=False, default=datetime.utcnow)

    #: Everything the viewer has seen, as a single integer. Unread for EVERY
    #: channel is then one grouped query over teams_messages instead of a
    #: per-user-per-message read table - which at this message volume would
    #: have been the largest table in the database within a month.
    #: NULL means "has never read anything here".
    last_read_message_id = db.Column(db.Integer, nullable=True)

    #: Muted channels still count unread but never raise a notification.
    muted = db.Column(
        db.Boolean, nullable=False, default=False, server_default="false"
    )

    user = db.relationship("User")

    __table_args__ = (
        db.UniqueConstraint(
            "channel_id", "user_id", name="uq_teams_channel_member"
        ),
        # Ordered (user, channel): every read starts from "my channels".
        db.Index("ix_teams_channel_members_user", "user_id", "channel_id"),
    )

    def __repr__(self):
        return f"<TeamChannelMember c{self.channel_id} u{self.user_id}>"
