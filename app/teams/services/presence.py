"""Who is around, and who is typing.

Both ride the sync poll rather than owning endpoints of their own. A
heartbeat that costs its own request per tick would double the load of the
thing it is measuring, and a "stopped typing" call is a request the browser
that closed mid-sentence never gets to make.
"""

from datetime import datetime, timedelta

from flask import current_app
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import TeamChannelMember, TeamPresence, TeamTyping

#: Don't rewrite the heartbeat on every tick. At a 2-second cadence that
#: would be one UPDATE per user per 2s purely to move a timestamp that
#: nothing reads at that resolution - pure WAL volume. With this guard the
#: common tick is zero writes and three indexed SELECTs.
HEARTBEAT_MIN_INTERVAL_SECONDS = 20

#: How long a "still typing" signal stays true. Slightly longer than the
#: client's refresh interval so the indicator doesn't flicker between keys.
TYPING_TTL_SECONDS = 6


def _cfg(key, default):
    try:
        return current_app.config.get(key, default)
    except RuntimeError:      # outside an app context (shouldn't happen)
        return default


def touch(user, status=None, commit=True):
    """Record that `user` is alive. Cheap and idempotent.

    Returns the presence row. Writes only when the row is stale or the
    user explicitly changed their status.
    """
    now = datetime.utcnow()
    presence = db.session.get(TeamPresence, user.id)

    if presence is None:
        presence = TeamPresence(
            user_id=user.id, last_seen_at=now, status=status or "online"
        )
        db.session.add(presence)
        try:
            db.session.flush()
        except IntegrityError:
            # Two tabs opened at once; either row is fine.
            db.session.rollback()
            return db.session.get(TeamPresence, user.id)
        if commit:
            db.session.commit()
        return presence

    changed = False
    if status and status != presence.status:
        presence.status = status
        changed = True

    stale = (now - presence.last_seen_at) >= timedelta(
        seconds=HEARTBEAT_MIN_INTERVAL_SECONDS)
    if stale:
        presence.last_seen_at = now
        changed = True

    if changed and commit:
        db.session.commit()
    return presence


def set_status(user, status, status_text=None, commit=True):
    """An explicit choice - online / away / busy / offline. Unlike the
    heartbeat this always writes, because the user asked for it."""
    if status not in ("online", "away", "busy", "offline"):
        raise ValueError(f"Unknown presence status: {status!r}")

    presence = touch(user, commit=False)
    presence.status = status
    presence.status_text = (status_text or "").strip() or None
    presence.last_seen_at = datetime.utcnow()

    if commit:
        db.session.commit()
    return presence


def statuses_for(user_ids):
    """Effective status per user id, derived from the heartbeat.

    Absence needs no write: a closed tab simply stops touching the row and
    decays to offline on its own. Users with no row at all are offline.
    """
    user_ids = list(user_ids or ())
    if not user_ids:
        return {}

    online = _cfg("TEAMS_PRESENCE_ONLINE_SECONDS", 60)
    away = _cfg("TEAMS_PRESENCE_AWAY_SECONDS", 300)
    now = datetime.utcnow()

    rows = TeamPresence.query.filter(
        TeamPresence.user_id.in_(user_ids)
    ).all()

    found = {
        row.user_id: row.derived_status(
            online_seconds=online, away_seconds=away, now=now)
        for row in rows
    }
    return {uid: found.get(uid, "offline") for uid in user_ids}


def channel_member_ids(channel_id):
    """Member ids for a channel, as a flat list - presence is only ever
    reported for the conversation actually on screen."""
    return [
        row[0] for row in db.session.query(TeamChannelMember.user_id)
        .filter(TeamChannelMember.channel_id == channel_id).all()
    ]


# ---------------------------------------------------------------------------
# Typing
# ---------------------------------------------------------------------------

def set_typing(user, channel_id, commit=True):
    """Refresh "X is typing" for a few seconds. Called from the sync tick
    while keys are still landing, so it must be an upsert, not an insert."""
    expires = datetime.utcnow() + timedelta(seconds=TYPING_TTL_SECONDS)

    row = db.session.get(TeamTyping, (channel_id, user.id))
    if row is None:
        row = TeamTyping(
            channel_id=channel_id, user_id=user.id, expires_at=expires
        )
        db.session.add(row)
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            row = db.session.get(TeamTyping, (channel_id, user.id))
            if row is None:
                return None
            row.expires_at = expires
    else:
        row.expires_at = expires

    if commit:
        db.session.commit()
    return row


def typing_in(channel_id, exclude_user_id=None):
    """Everyone currently typing in a channel. Expiry does the cleanup, so
    a stale row is simply filtered out rather than needing a sweep."""
    query = TeamTyping.query.filter(
        TeamTyping.channel_id == channel_id,
        TeamTyping.expires_at > datetime.utcnow(),
    )
    if exclude_user_id:
        query = query.filter(TeamTyping.user_id != exclude_user_id)
    return query.all()


def clear_typing(user, channel_id, commit=True):
    """Explicit stop - on send, when the composer empties, on blur."""
    row = db.session.get(TeamTyping, (channel_id, user.id))
    if row is not None:
        db.session.delete(row)
        if commit:
            db.session.commit()
    return None
