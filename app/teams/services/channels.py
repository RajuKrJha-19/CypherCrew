"""Creating, finding, joining and leaving conversations."""

import re
from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import TeamChannel, TeamChannelMember, User


class ChannelError(Exception):
    """Something the user did wrong, with a message fit to show them."""


#: Channel handles are lowercase, hyphen-separated, and short enough to sit
#: in a sidebar without truncating.
_SLUG_STRIP = re.compile(r"[^a-z0-9]+")
MAX_NAME = 60


def slugify(value):
    """"Design Team " -> "design-team". Empty if nothing survives."""
    slug = _SLUG_STRIP.sub("-", (value or "").strip().lower()).strip("-")
    return slug[:80]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def channels_for(user, include_archived=False):
    """Every conversation `user` belongs to, newest activity first.

    Ordered by last_message_at (denormalised onto the channel) so this
    never joins teams_messages - it runs on every page of the module.
    """
    query = (
        TeamChannel.query
        .join(TeamChannelMember,
              TeamChannelMember.channel_id == TeamChannel.id)
        .filter(TeamChannelMember.user_id == user.id)
    )
    if not include_archived:
        query = query.filter(TeamChannel.archived_at.is_(None))

    return query.order_by(
        TeamChannel.last_message_at.desc().nullslast(),
        TeamChannel.id.desc(),
    ).all()


def browsable_channels(user):
    """Public channels the user could join but has not. Private channels
    and DMs are absent by construction - you cannot browse into either."""
    joined = db.session.query(TeamChannelMember.channel_id).filter(
        TeamChannelMember.user_id == user.id
    )
    return (
        TeamChannel.query
        .filter(
            TeamChannel.kind == "channel",
            TeamChannel.visibility == "public",
            TeamChannel.archived_at.is_(None),
            ~TeamChannel.id.in_(joined),
        )
        .order_by(TeamChannel.name.asc())
        .all()
    )


def membership(channel_id, user_id):
    """The membership row, or None."""
    return TeamChannelMember.query.filter_by(
        channel_id=channel_id, user_id=user_id
    ).first()


def can_read(channel, user):
    """Public channels are readable by any signed-in member of staff, so
    someone can look before they join. Private channels and DMs require
    membership - that is the entire access model."""
    if channel is None:
        return False
    if channel.visibility == "public" and channel.kind == "channel":
        return True
    return membership(channel.id, user.id) is not None


def can_post(channel, user):
    """Posting always requires membership, and an archived channel is
    read-only for everyone."""
    if channel is None or channel.is_archived:
        return False
    return membership(channel.id, user.id) is not None


def can_administer(channel, user):
    """Rename, archive, manage membership."""
    member = membership(channel.id, user.id)
    return bool(member and member.role == "owner")


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def create_channel(name, creator, description=None, visibility="public",
                   client_id=None, members=(), commit=True):
    """Create a channel and put the creator in it as owner.

    Raises ChannelError with a user-facing message on bad input or a
    duplicate handle.
    """
    name = (name or "").strip()
    if not name:
        raise ChannelError("Channel name is required.")
    if len(name) > MAX_NAME:
        raise ChannelError(f"Channel name must be {MAX_NAME} characters or fewer.")

    key = slugify(name)
    if not key:
        raise ChannelError("Channel name must contain at least one letter or number.")
    if visibility not in ("public", "private"):
        visibility = "public"

    channel = TeamChannel(
        key=key,
        name=name,
        description=(description or "").strip() or None,
        kind="channel",
        visibility=visibility,
        client_id=client_id,
        created_by_id=creator.id,
    )
    db.session.add(channel)

    try:
        # Flush rather than commit: we need the id to attach members, but a
        # duplicate handle should abort the whole thing, not leave an empty
        # channel behind.
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        raise ChannelError(f"A channel called #{key} already exists.")

    add_member(channel, creator, role="owner", commit=False)
    for user in members:
        if user.id != creator.id:
            add_member(channel, user, commit=False)

    if commit:
        db.session.commit()
    return channel


def add_member(channel, user, role="member", commit=True):
    """Idempotent: joining a channel you are already in is a no-op, not an
    error, because the "Join" button is reachable from a stale page."""
    existing = membership(channel.id, user.id)
    if existing:
        return existing

    member = TeamChannelMember(
        channel_id=channel.id,
        user_id=user.id,
        role=role,
        # Start read at the current end of the channel: joining #general
        # should not hand you 4,000 unread messages from before you arrived.
        last_read_message_id=_latest_message_id(channel.id),
    )
    db.session.add(member)

    try:
        db.session.flush()
    except IntegrityError:
        # Two joins raced; the unique constraint settled it. Take theirs.
        db.session.rollback()
        return membership(channel.id, user.id)

    if commit:
        db.session.commit()
    return member


def remove_member(channel, user, commit=True):
    member = membership(channel.id, user.id)
    if not member:
        return False
    db.session.delete(member)
    if commit:
        db.session.commit()
    return True


def rename_channel(channel, name, description=None, commit=True):
    """Change the display name and description.

    The KEY is deliberately not regenerated. It is in every link anyone has
    shared, and in the DM identity scheme; renaming "Design Team" to
    "Brand" should change the label, not break the bookmarks.
    """
    name = (name or "").strip()
    if not name:
        raise ChannelError("Channel name is required.")
    if len(name) > MAX_NAME:
        raise ChannelError(f"Channel name must be {MAX_NAME} characters or fewer.")

    channel.name = name
    channel.description = (description or "").strip() or None
    if commit:
        db.session.commit()
    return channel


def set_muted(channel, user, muted, commit=True):
    """Silence a channel without leaving it.

    The alternative people reach for is leaving, which loses the history
    and the ability to be @mentioned back in - so this exists to stop a
    busy #general from costing the team its channel.
    """
    member = membership(channel.id, user.id)
    if member is None:
        return None
    member.muted = bool(muted)
    if commit:
        db.session.commit()
    return member


def is_muted(channel_id, user_id):
    member = membership(channel_id, user_id)
    return bool(member and member.muted)


def members_of(channel):
    """Members with their user rows, owners first then by name."""
    rows = [m for m in channel.members if m.user is not None]
    return sorted(rows, key=lambda m: (m.role != "owner", (m.user.name or "").lower()))


def addable_users(channel):
    """Active people not already in this channel."""
    present = {m.user_id for m in channel.members}
    return (
        User.query
        .filter(User.status == "active", ~User.id.in_(present or {0}))
        .order_by(User.name.asc())
        .all()
    )


def archive_channel(channel, commit=True):
    channel.archived_at = datetime.utcnow()
    if commit:
        db.session.commit()
    return channel


def unarchive_channel(channel, commit=True):
    channel.archived_at = None
    if commit:
        db.session.commit()
    return channel


# ---------------------------------------------------------------------------
# Direct messages
# ---------------------------------------------------------------------------

def get_or_create_dm(user_a, user_b, commit=True):
    """The DM between two people, creating it on first use.

    Concurrency-safe by construction: both sides compute the same key and
    the unique index decides. The IntegrityError path is the normal path
    when two people open the conversation at the same moment, not an
    exceptional one.
    """
    key = TeamChannel.dm_key(user_a.id, user_b.id)

    existing = TeamChannel.query.filter_by(key=key).first()
    if existing:
        # Self-heal: an interrupted first creation could have left the
        # channel without both members.
        add_member(existing, user_a, commit=False)
        add_member(existing, user_b, commit=False)
        if commit:
            db.session.commit()
        return existing

    channel = TeamChannel(
        key=key,
        name=None,
        kind="dm",
        visibility="private",
        created_by_id=user_a.id,
    )
    db.session.add(channel)

    try:
        db.session.flush()
    except IntegrityError:
        db.session.rollback()
        return TeamChannel.query.filter_by(key=key).first()

    add_member(channel, user_a, commit=False)
    if user_b.id != user_a.id:
        add_member(channel, user_b, commit=False)

    if commit:
        db.session.commit()
    return channel


DEFAULT_CHANNEL_KEY = "general"


def ensure_default_channel(user, commit=True):
    """Make sure #general exists and `user` is in it.

    Created lazily on first visit rather than in app/seed.py: seeding runs
    on every boot (AUTO_SEED defaults on, tests included), and a channel is
    domain data belonging to a feature that may well be switched off.

    Idempotent by way of the unique key, so a dozen people signing in at
    once produce one channel, not a dozen. Never raises - a hiccup here
    must not be the thing that stops someone reaching Teams.
    """
    try:
        channel = TeamChannel.query.filter_by(key=DEFAULT_CHANNEL_KEY).first()

        if channel is None:
            channel = TeamChannel(
                key=DEFAULT_CHANNEL_KEY,
                name="General",
                description="Everyone, everything. The default channel.",
                kind="channel",
                visibility="public",
                created_by_id=user.id,
            )
            db.session.add(channel)
            try:
                db.session.flush()
            except IntegrityError:
                db.session.rollback()
                channel = TeamChannel.query.filter_by(
                    key=DEFAULT_CHANNEL_KEY).first()
                if channel is None:
                    return None

        # Whoever brings #general into existence owns it, the same as any
        # other channel they create. Arbitrary - it is whoever opened Teams
        # first - but the alternative is a company-wide channel with no
        # owner at all, which nobody can rename, archive or add anyone to.
        # Everybody after them joins as a member.
        first = channel.created_by_id == user.id and channel.members.count() == 0
        add_member(channel, user, role="owner" if first else "member",
                   commit=False)

        if commit:
            db.session.commit()
        return channel
    except Exception:
        db.session.rollback()
        return None


def dm_candidates(user):
    """Active colleagues you could start a DM with."""
    return (
        User.query
        .filter(User.status == "active", User.id != user.id)
        .order_by(User.name.asc())
        .all()
    )


def _latest_message_id(channel_id):
    from app.models import TeamMessage
    return db.session.query(
        db.func.max(TeamMessage.id)
    ).filter(TeamMessage.channel_id == channel_id).scalar()
