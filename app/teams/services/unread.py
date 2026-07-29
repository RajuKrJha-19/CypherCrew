"""Unread state - the whole sidebar in one query.

The design here is the reason the module can be polled at all. Because the
newest message id is denormalised onto the channel and the read cursor sits
on the membership row, "does this channel have unread?" is a comparison
between two columns of a single joined row. Nothing touches teams_messages,
so the cost does not grow with message volume.

Exact counts DO touch teams_messages, so they are reserved for the places a
number actually helps - DMs and mentions - where the unread range is short
because people read them. Channels get a dot.
"""

from app.extensions import db
from app.models import TeamChannel, TeamChannelMember, TeamMessage


def channel_state(user, include_archived=False):
    """One row per channel the user is in: the channel, their membership,
    and whether it has anything new.

    Returns a list of dicts. One query, no N+1, no per-channel lookups -
    and there is a test asserting exactly that, because this is the first
    thing that would silently regress.
    """
    query = (
        db.session.query(TeamChannel, TeamChannelMember)
        .join(TeamChannelMember,
              TeamChannelMember.channel_id == TeamChannel.id)
        .filter(TeamChannelMember.user_id == user.id)
    )
    if not include_archived:
        query = query.filter(TeamChannel.archived_at.is_(None))

    rows = query.order_by(
        TeamChannel.last_message_at.desc().nullslast(),
        TeamChannel.id.desc(),
    ).all()

    state = []
    for channel, member in rows:
        last_id = channel.last_message_id or 0
        read_id = member.last_read_message_id or 0
        state.append({
            "channel": channel,
            "member": member,
            # Muting hides the dot. That IS what muting a channel means
            # here: ordinary channel traffic never raised a notification in
            # the first place (see services/notify.py), so the badge was
            # the only thing a busy #general was costing anyone. An
            # @mention still comes through - mute silences ambient noise,
            # not somebody addressing you directly.
            "unread": last_id > read_id and not member.muted,
            "muted": bool(member.muted),
            "last_message_id": last_id,
            "last_read_message_id": read_id,
        })
    return state


def unread_counts(user, channel_ids=None):
    """Exact unread counts, keyed by channel id.

    One grouped query for every channel at once - never one query per
    channel. Own messages and deleted messages are excluded: seeing a badge
    for something you just said yourself is the fastest way to make people
    distrust the badge.
    """
    query = (
        db.session.query(
            TeamMessage.channel_id,
            db.func.count(TeamMessage.id),
        )
        .join(
            TeamChannelMember,
            TeamChannelMember.channel_id == TeamMessage.channel_id,
        )
        .filter(
            TeamChannelMember.user_id == user.id,
            TeamMessage.id > db.func.coalesce(
                TeamChannelMember.last_read_message_id, 0),
            TeamMessage.deleted_at.is_(None),
            db.or_(
                TeamMessage.user_id.is_(None),
                TeamMessage.user_id != user.id,
            ),
        )
    )
    if channel_ids:
        query = query.filter(TeamMessage.channel_id.in_(list(channel_ids)))

    return dict(query.group_by(TeamMessage.channel_id).all())


def total_unread(user):
    """A single number for the ERP sidebar badge.

    Deliberately the cheap version: it counts CHANNELS with something new,
    not messages. It rides on the notifications poll that every page
    already makes, so it must stay a single indexed join and never widen
    into a scan of teams_messages.
    """
    return (
        db.session.query(db.func.count(TeamChannel.id))
        .join(TeamChannelMember,
              TeamChannelMember.channel_id == TeamChannel.id)
        .filter(
            TeamChannelMember.user_id == user.id,
            TeamChannelMember.muted.is_(False),
            TeamChannel.archived_at.is_(None),
            TeamChannel.last_message_id.isnot(None),
            TeamChannel.last_message_id > db.func.coalesce(
                TeamChannelMember.last_read_message_id, 0),
        )
        .scalar()
    ) or 0


def mark_read(user, channel, up_to_message_id=None, commit=True):
    """Advance the read cursor. Never moves it backwards - an out-of-order
    request from a slow tab must not resurrect unread messages."""
    member = TeamChannelMember.query.filter_by(
        channel_id=channel.id, user_id=user.id
    ).first()
    if member is None:
        return None

    target = up_to_message_id or channel.last_message_id or 0
    if target > (member.last_read_message_id or 0):
        member.last_read_message_id = target
        if commit:
            db.session.commit()
    return member
