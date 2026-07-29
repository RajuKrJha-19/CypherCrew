"""The one payload the client polls for.

Everything the Teams shell needs per tick - new messages, changed messages,
channel unread state, presence, typing - is assembled here and returned as
one object. Four endpoints polled separately would be four times the
requests, four times the worker occupancy and four times the connection
checkouts, for data drawn from the same three tables inside what could have
been a single transaction.

This module is also the transport seam. `build_sync_payload` knows nothing
about HTTP: no request, no response, no headers. A future SSE or websocket
endpoint emits this same dict as a frame, and the client's `applySync`
stays exactly as it is.

Message bodies are rendered to HTML here rather than shipped as data for a
JavaScript renderer to lay out. One renderer means one escaping path, and
`|linkify`, `|mentions` and `avatar_url` behave identically on first paint
and on the two-hundredth poll. Two renderers would agree on the day they
were written and drift ever after.
"""

from datetime import datetime, timedelta

from flask import current_app, render_template, url_for

from app.teams.services import messages as messages_service
from app.teams.services import presence as presence_service
from app.teams.services import unread as unread_service

#: The cursor is handed back a couple of seconds behind the read, so an
#: edit committing in the same instant as the query is picked up by the
#: next tick instead of falling through the gap. `changed` is applied by
#: id, so re-delivering a message is harmless.
CURSOR_OVERLAP_SECONDS = 2


def build_sync_payload(user, channel=None, after_id=0, since=None,
                       thread_root_id=None, thread_after_id=0,
                       typing=False, focused=True):
    """Assemble one tick's worth of state for `user`.

    `channel` is the conversation currently on screen (may be None - the
    Teams home page and the ERP badge both poll without one).
    """
    now = datetime.utcnow()

    # The heartbeat rides along. It is write-guarded, so on most ticks this
    # is a primary-key read and nothing else.
    presence_service.touch(user, status=None if focused else "away")

    payload = {
        "now": _iso(now),
        "cursor": _iso(now - timedelta(seconds=CURSOR_OVERLAP_SECONDS)),
        "messages": [],
        "changed": [],
        "thread": [],
        "more": False,
        "channels": [],
        "total_unread": 0,
        "presence": [],
        "typing": [],
        "next_poll_ms": _next_poll_ms(focused),
    }

    # ---- channel list + unread -------------------------------------
    state = unread_service.channel_state(user)
    dm_ids = [
        row["channel"].id for row in state
        if row["channel"].is_dm and row["unread"]
    ]
    # Exact numbers only where a number helps and the range is short.
    counts = unread_service.unread_counts(user, dm_ids) if dm_ids else {}

    unread_total = 0
    for row in state:
        chan = row["channel"]
        if row["unread"]:
            unread_total += 1
        payload["channels"].append({
            "id": chan.id,
            "unread": row["unread"],
            "count": counts.get(chan.id),
            "last_id": row["last_message_id"],
            "last_at": _iso(chan.last_message_at),
        })
    payload["total_unread"] = unread_total

    if channel is None:
        return payload

    # ---- the open conversation --------------------------------------
    if typing:
        presence_service.set_typing(user, channel.id)

    new_messages, has_more = messages_service.messages_after(
        channel.id, after_id)
    payload["messages"] = [render_message(m, user) for m in new_messages]
    payload["more"] = has_more

    # Everything the client already holds that has since been edited,
    # deleted or reacted to. Bounded by `after_id` so a message is never
    # delivered twice - once as new and once as changed.
    changed = messages_service.messages_changed(
        channel.id, _parse(since), after_id)
    payload["changed"] = [render_message(m, user) for m in changed]

    if thread_root_id:
        payload["thread"] = [
            render_message(m, user) for m in
            messages_service.thread_messages(thread_root_id, thread_after_id)
        ]

    member_ids = presence_service.channel_member_ids(channel.id)
    statuses = presence_service.statuses_for(member_ids)
    payload["presence"] = [
        {"u": uid, "s": status} for uid, status in statuses.items()
    ]

    payload["typing"] = [
        {"u": row.user_id, "n": row.user.name if row.user else "Someone"}
        for row in presence_service.typing_in(
            channel.id, exclude_user_id=user.id)
    ]

    return payload


def render_message(message, viewer):
    """One message, as both the metadata the client indexes on and the
    HTML it paints - rendered by the same partial that drew the page."""
    return {
        "id": message.id,
        "ch": message.channel_id,
        "u": message.user_id,
        "cid": message.client_msg_id,
        "parent": message.parent_id,
        "root": message.thread_root_id,
        "deleted": message.is_deleted,
        "edited": _iso(message.edited_at),
        "html": render_template(
            "teams/_message.html", message=message, viewer=viewer
        ),
    }


def _next_poll_ms(focused):
    """How long the client should wait before asking again.

    Server-authoritative on purpose. It is the pressure valve for the whole
    feature: if the box is loaded or the team grows, raising
    TEAMS_POLL_ACTIVE_MS slows every open tab at once, with no deploy and
    no client change.
    """
    cfg = current_app.config
    if not focused:
        return cfg.get("TEAMS_POLL_HIDDEN_MS", 15000)
    return cfg.get("TEAMS_POLL_ACTIVE_MS", 2000)


def _iso(value):
    return value.isoformat() + "Z" if value else None


def _parse(value):
    """Parse a cursor the client echoed back. Anything unparseable means
    "I have no cursor", which costs one skipped change sweep - never an
    error, because a malformed query string must not break the poll."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).rstrip("Z"))
    except (TypeError, ValueError):
        return None
