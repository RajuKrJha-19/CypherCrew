"""@mentions in task comments.

A comment can tag teammates by name - "@Raju Kr Jha". On save we notify
everyone tagged; on render we highlight the tags. Names (not handles) are
the identity here since the app has no usernames, so longer names are
matched first and matching is anchored so "@Ravi" can't fire inside
"@Ravindra".
"""

import re

from flask import g
from markupsafe import Markup

from app.models import User
from app.utils.notifications import create_notification


def _active_users():
    """Active users, fetched once per request (comment lists render many)."""
    cached = getattr(g, "_mention_users", None)
    if cached is None:
        cached = User.query.filter_by(status="active").all()
        g._mention_users = cached
    return cached


def _mention_pattern(names):
    # Longest first so the full name wins over a shorter prefix.
    ranked = sorted({n for n in names if n}, key=len, reverse=True)
    if not ranked:
        return None, ranked
    return re.compile(r"@(" + "|".join(re.escape(n) for n in ranked) + r")\b"), ranked


def find_mentioned_users(message, users=None):
    """Users referenced as @Full Name in `message`."""
    if not message or "@" not in message:
        return []

    users = users if users is not None else _active_users()
    pattern, _ = _mention_pattern(u.name for u in users)
    if pattern is None:
        return []

    hit = {m.group(1) for m in pattern.finditer(message)}
    return [u for u in users if u.name in hit]


def notify_mentioned_users(task, message, actor, link, skip_user_ids=None, source="comment"):
    """Notify each tagged user (except the author and anyone already
    notified for this mention). Returns the users notified.

    `source` is just the wording of the notification ("comment" /
    "description") - the same tagging rule and category apply either
    way, and both land in the mentions panel, not the general
    activity feed.
    """
    skip = set(skip_user_ids or ())
    skip.add(actor.id)

    verb = "the description of" if source == "description" else "a comment on"

    notified = []
    for user in find_mentioned_users(message):
        if user.id in skip:
            continue
        create_notification(
            user_id=user.id,
            title="You were mentioned",
            message=f"{actor.name} mentioned you in {verb} '{task.title}'",
            link=link,
            actor_id=actor.id,
            task_id=task.id,
            email=True,
            category="mention",
        )
        skip.add(user.id)
        notified.append(user)
    return notified


def highlight_mentions(value):
    """Wrap @Full Name (known active users) in a styled span. Runs on the
    already-escaped/linkified markup, so it only ever inserts a span - it
    can't introduce raw HTML from the comment text."""
    if not value:
        return value

    text = str(value)
    if "@" not in text:
        return value

    pattern, _ = _mention_pattern(u.name for u in _active_users())
    if pattern is None:
        return value

    out = pattern.sub(
        lambda m: '<span class="mention">@' + m.group(1) + "</span>",
        text,
    )
    return Markup(out)
