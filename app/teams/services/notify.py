"""Who gets told about a message.

The rule, and the reason for it: a busy channel writes thousands of
messages a day, and one notification per message would mean thousands of
`notifications` rows, a visibly slower 5-second topbar poll for everyone,
and a bell that people stop looking at within a week. The unread dot IS the
notification for ordinary channel traffic.

So exactly three things raise one:
  - an explicit @mention
  - a direct message
  - a meeting invite (Phase 5)

Everything else is deliberately silent.
"""

from flask import current_app

from app.models import User
from app.utils.notifications import create_notification


def notify_message(message, channel, author, link, commit=True):
    """Fan out a newly posted message. Returns the users notified.

    `link` is built by the caller, the same contract as
    utils.mentions.notify_mentioned_users - it keeps this module free of
    url_for, and therefore free of needing a request context to be
    testable.

    Never raises: a notification is a courtesy, and failing to send one
    must not roll back the message it is about. It does LOG, though - a
    silent `except: pass` here would hide a broken bell indefinitely.
    """
    try:
        recipients = _recipients(message, channel, author)
        if not recipients:
            return []

        preview = _preview(message.body)
        notified = []

        for user, category in recipients:
            if user.id == author.id:
                continue
            if category == "mention":
                title = "You were mentioned"
                body = f"{author.name} mentioned you in " + _where(channel, author)
            else:
                title = f"Message from {author.name}"
                body = preview

            create_notification(
                user_id=user.id,
                title=title,
                message=body,
                link=link,
                actor_id=author.id,
                # No email: chat is high-frequency and an inbox copy of every
                # DM would be its own problem. The in-app bell is enough.
                email=False,
                category=category,
            )
            notified.append(user)

        if commit:
            from app.extensions import db
            db.session.commit()
        return notified
    except Exception:
        current_app.logger.exception(
            "Teams: failed to notify for message %s", getattr(message, "id", "?"))
        return []


def _recipients(message, channel, author):
    """[(user, category)], de-duplicated, mention winning over DM.

    A mention inside a DM should read as a mention, not as generic DM
    traffic - so mentions are collected first and the DM pass skips
    anyone already on the list.
    """
    seen = set()
    out = []

    # Mentions are NOT filtered by mute. Muting a channel silences its
    # ambient traffic (see unread.channel_state); somebody typing your name
    # is not ambient, and a mention nobody receives is worse than a channel
    # nobody muted.
    for user in _mentioned(message):
        if user.id in seen:
            continue
        seen.add(user.id)
        out.append((user, "mention"))

    if channel.is_dm:
        for member in channel.members:
            if member.user_id in seen or member.user_id == author.id:
                continue
            if member.muted:
                continue
            if member.user is not None:
                seen.add(member.user_id)
                out.append((member.user, "activity"))

    return out


def _mentioned(message):
    """Users tagged in the message.

    Read straight off mention_user_ids, which post_message resolved once at
    write time - resolving here would mean re-running the whole-users-table
    regex for every recipient of every message.
    """
    ids = message.mention_user_ids or []
    if not ids:
        return []
    return User.query.filter(
        User.id.in_(ids), User.status == "active"
    ).all()


def _preview(body, limit=120):
    text = (body or "").strip().replace("\n", " ")
    if not text:
        return "Sent an attachment"
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _where(channel, author):
    if channel.is_dm:
        return "a direct message"
    return f"#{channel.key}"
