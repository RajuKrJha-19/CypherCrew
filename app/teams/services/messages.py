"""Posting, editing, deleting and reacting - plus the two read queries the
poller lives on.

Every mutation here has to leave two things true, or the UI goes wrong in a
way that is hard to see in testing and obvious in use:

  1. `channel.last_message_id/at` tracks the newest message, because that
     pair is what makes an unread badge free.
  2. `message.updated_at` moves whenever an open client would need to
     re-render the bubble - including for reactions, which live in another
     table and therefore do not trigger SQLAlchemy's `onupdate`.
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import TeamChannel, TeamMessage, TeamReaction
from app.utils.mentions import find_mentioned_users

#: How many messages the first paint of a channel loads, and the ceiling on
#: a single delta. Every query in this module is bounded - an unbounded read
#: against a busy channel is the one thing that could actually reach the
#: 30-second statement_timeout.
PAGE_SIZE = 50
DELTA_LIMIT = 60

MAX_BODY = 8000


class MessageError(Exception):
    """Something the user did wrong, with a message fit to show them."""


# ---------------------------------------------------------------------------
# Writing
# ---------------------------------------------------------------------------

def post_message(channel, author, body, client_msg_id=None, parent_id=None,
                 kind="text", meta=None, has_attachments=False, commit=True):
    """Add a message to `channel`. Idempotent on `client_msg_id`.

    `has_attachments` is passed by the caller because the attachment rows
    cannot exist yet - they need this message's id. Without it, dragging a
    file in with no caption would be rejected as an empty message.
    """
    body = (body or "").strip()
    if kind == "text" and not body and not has_attachments:
        raise MessageError("Message cannot be empty.")
    if len(body) > MAX_BODY:
        raise MessageError(f"Message must be {MAX_BODY} characters or fewer.")

    # Fast path for a retried send: the client kept its id, so return the
    # message it already made rather than making a second one.
    if client_msg_id:
        existing = TeamMessage.query.filter_by(
            channel_id=channel.id, client_msg_id=client_msg_id
        ).first()
        if existing:
            return existing

    thread_root_id = None
    parent = None
    if parent_id:
        parent = TeamMessage.query.filter_by(
            id=parent_id, channel_id=channel.id
        ).first()
        if parent is None:
            raise MessageError("The message you replied to no longer exists.")
        # One level of threading: replying to a reply lands on the same
        # root, so a thread is always a single flat range fetch.
        thread_root_id = parent.thread_root_id or parent.id

    message = TeamMessage(
        channel_id=channel.id,
        user_id=author.id,
        parent_id=parent.id if parent else None,
        thread_root_id=thread_root_id,
        body=body or None,
        kind=kind,
        meta=meta,
        client_msg_id=client_msg_id or None,
        mention_user_ids=_mention_ids(body, author),
    )
    db.session.add(message)

    try:
        db.session.flush()
    except IntegrityError:
        # Two sends raced on the same client id; the unique index settled
        # it. Whichever landed first is the real message.
        db.session.rollback()
        if client_msg_id:
            existing = TeamMessage.query.filter_by(
                channel_id=channel.id, client_msg_id=client_msg_id
            ).first()
            if existing:
                return existing
        raise

    if parent is not None:
        parent.reply_count = (parent.reply_count or 0) + 1
        # Bump the root too: the "3 replies" line on its bubble changed, so
        # open clients need to re-render it.
        _touch(parent)

    _bump_channel(channel, message)

    if commit:
        db.session.commit()
    return message


def edit_message(message, actor, body, commit=True):
    if message.user_id != actor.id:
        raise MessageError("You can only edit your own messages.")
    if message.is_deleted:
        raise MessageError("That message has been deleted.")

    body = (body or "").strip()
    if not body:
        raise MessageError("Message cannot be empty.")
    if len(body) > MAX_BODY:
        raise MessageError(f"Message must be {MAX_BODY} characters or fewer.")

    message.body = body
    message.mention_user_ids = _mention_ids(body, actor)
    message.edited_at = datetime.utcnow()
    _touch(message)

    if commit:
        db.session.commit()
    return message


def delete_message(message, actor, is_admin=False, commit=True):
    """Soft delete. The row stays so the next poll can tell every open
    client to drop the bubble; the text goes so it is genuinely gone."""
    if message.user_id != actor.id and not is_admin:
        raise MessageError("You can only delete your own messages.")
    if message.is_deleted:
        return message

    message.deleted_at = datetime.utcnow()
    message.body = None
    message.mention_user_ids = None
    _touch(message)

    if message.parent_id:
        parent = db.session.get(TeamMessage, message.parent_id)
        if parent is not None and parent.reply_count:
            parent.reply_count -= 1
            _touch(parent)

    if commit:
        db.session.commit()
    return message


def toggle_reaction(message, user, emoji, commit=True):
    """Add the reaction, or remove it if it is already there. Returns True
    if it is now present."""
    emoji = (emoji or "").strip()
    if not emoji or len(emoji) > 32:
        raise MessageError("That is not a valid reaction.")
    if message.is_deleted:
        raise MessageError("That message has been deleted.")

    existing = TeamReaction.query.filter_by(
        message_id=message.id, user_id=user.id, emoji=emoji
    ).first()

    if existing:
        db.session.delete(existing)
        added = False
    else:
        db.session.add(TeamReaction(
            message_id=message.id, user_id=user.id, emoji=emoji
        ))
        added = True

    # Reactions are a different table, so SQLAlchemy's onupdate never
    # fires. Without this the change sweep cannot see them and the reaction
    # only appears for the person who clicked.
    _touch(message)

    try:
        if commit:
            db.session.commit()
    except IntegrityError:
        # Double-click race on adding; the unique constraint already holds
        # the truth we wanted.
        db.session.rollback()
        return True

    return added


# ---------------------------------------------------------------------------
# Reading - the two queries the poller lives on
# ---------------------------------------------------------------------------

def latest_page(channel_id, limit=PAGE_SIZE):
    """The newest `limit` top-level messages, oldest-first for rendering."""
    rows = (
        TeamMessage.query
        .filter(
            TeamMessage.channel_id == channel_id,
            TeamMessage.parent_id.is_(None),
        )
        .order_by(TeamMessage.id.desc())
        .limit(limit)
        .all()
    )
    return list(reversed(rows))


def messages_after(channel_id, after_id, limit=DELTA_LIMIT):
    """New messages only. Pure index range scan on (channel_id, id).

    Returns (messages, has_more) - `has_more` tells the client to poll
    again immediately instead of waiting out the interval, so catching up
    after a long absence does not take a minute of ticks.
    """
    rows = (
        TeamMessage.query
        .filter(
            TeamMessage.channel_id == channel_id,
            TeamMessage.id > (after_id or 0),
            TeamMessage.parent_id.is_(None),
        )
        .order_by(TeamMessage.id.asc())
        .limit(limit + 1)
        .all()
    )
    has_more = len(rows) > limit
    return rows[:limit], has_more


def messages_changed(channel_id, since, up_to_id, limit=DELTA_LIMIT):
    """Messages the client already holds that have since been edited,
    deleted or reacted to.

    `up_to_id` bounds this to what the client actually has, so a message
    is never delivered twice - once as new, once as changed.
    """
    if not since or not up_to_id:
        return []
    return (
        TeamMessage.query
        .filter(
            TeamMessage.channel_id == channel_id,
            TeamMessage.updated_at > since,
            TeamMessage.id <= up_to_id,
        )
        .order_by(TeamMessage.id.asc())
        .limit(limit)
        .all()
    )


#: Postgres text-search configuration.
#:
#: 'simple', not 'english'. This team writes Hinglish - "polish kro",
#: "deploy kar do" - and the English configuration would stem and strip
#: those as if they were English, dropping words that carry the meaning.
#: 'simple' lower-cases and splits on word boundaries and does nothing
#: clever, which is the right amount of clever for mixed-language chat.
#: The cost is no stemming at all, so "designs" will not find "design" -
#: an acceptable trade against silently losing half a Hindi sentence.
SEARCH_CONFIG = "simple"

SEARCH_LIMIT = 40


def search(user, query, channel_id=None, limit=SEARCH_LIMIT):
    """Find messages `user` is allowed to see.

    Scoped by membership through a join, not by filtering afterwards: the
    database must never return a private channel's text to somebody who is
    not in it, and "we filter it out in Python" is one refactor away from
    not being true.
    """
    from app.models import TeamChannelMember

    query = (query or "").strip()
    if len(query) < 2:
        return []

    vector = db.func.to_tsvector(
        SEARCH_CONFIG, db.func.coalesce(TeamMessage.body, ""))
    terms = db.func.plainto_tsquery(SEARCH_CONFIG, query)

    rows = (
        TeamMessage.query
        .join(TeamChannelMember,
              TeamChannelMember.channel_id == TeamMessage.channel_id)
        .filter(
            TeamChannelMember.user_id == user.id,
            TeamMessage.deleted_at.is_(None),
            TeamMessage.body.isnot(None),
            vector.op("@@")(terms),
        )
    )
    if channel_id:
        rows = rows.filter(TeamMessage.channel_id == channel_id)

    # Newest first rather than by rank: in a conversation "the most recent
    # time we talked about this" is almost always what is being looked for,
    # and relevance ranking over one-line messages is mostly noise.
    #
    # The eager loads are what keep this at three queries instead of 1 + 2N
    # - every result renders its channel name and its author.
    from sqlalchemy.orm import joinedload

    return (
        rows.options(joinedload(TeamMessage.channel),
                     joinedload(TeamMessage.user))
        .order_by(TeamMessage.id.desc())
        .limit(limit)
        .all()
    )


def thread_messages(root_id, after_id=None, limit=DELTA_LIMIT):
    """A thread, as one flat indexed fetch off thread_root_id."""
    query = TeamMessage.query.filter(TeamMessage.thread_root_id == root_id)
    if after_id:
        query = query.filter(TeamMessage.id > after_id)
    return query.order_by(TeamMessage.id.asc()).limit(limit).all()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------

def _touch(message):
    """Move the change cursor. Assigning explicitly rather than relying on
    `onupdate` so it also fires when nothing on the row itself changed -
    which is exactly the reaction case."""
    message.updated_at = datetime.utcnow()


def _bump_channel(channel, message):
    """Keep the denormalised newest-message pointers honest.

    Guarded rather than assigned: a system message written during a
    backfill, or two commits landing out of order, must not drag the
    pointer backwards and mark read messages unread for everyone.
    """
    if not channel.last_message_id or message.id > channel.last_message_id:
        channel.last_message_id = message.id
        channel.last_message_at = message.created_at


def _mention_ids(body, author):
    """Ids of the people tagged in `body`, resolved once, here, at write
    time - never on read. See TeamMessage.mention_user_ids."""
    if not body or "@" not in body:
        return None
    ids = [u.id for u in find_mentioned_users(body) if u.id != author.id]
    return ids or None


def refresh_channel_pointers(channel, commit=True):
    """Recompute last_message_id/at from the table. Only needed after a
    bulk import or a repair - the normal path maintains them inline."""
    row = (
        db.session.query(TeamMessage.id, TeamMessage.created_at)
        .filter(TeamMessage.channel_id == channel.id)
        .order_by(TeamMessage.id.desc())
        .first()
    )
    channel.last_message_id = row[0] if row else None
    channel.last_message_at = row[1] if row else None
    if commit:
        db.session.commit()
    return channel


def latest_message_id(channel_id):
    return db.session.query(db.func.max(TeamMessage.id)).filter(
        TeamMessage.channel_id == channel_id
    ).scalar()
