"""Scheduling a meeting, and getting into it.

READ THIS BEFORE TOUCHING A DATETIME HERE.

`Meeting.meeting_date` is **IST-naive**. It predates Teams: the old
meetings form parsed a browser `datetime-local` string straight into the
column, and `routes/meetings.py` compares it against `ist_now()`. Every
existing row, the calendar and the dashboard's "Upcoming" panel all assume
that.

`started_at` and `ended_at` are **UTC-naive**, like every other timestamp
in this codebase - they are recorded by the server when somebody actually
joins or the meeting is closed.

So the two must never be compared or subtracted. `is_live` below asks
"has it started and not ended", which is answerable from the UTC pair
alone; `starts_within` asks a calendar question, which is answerable from
the IST value alone. Mixing them is what would make a meeting appear to
start five and a half hours late, twice a year for whoever is debugging it.
"""

from datetime import datetime, timedelta

from flask import current_app

from app.extensions import db
from app.models import Meeting, TeamChannel, User
from app.teams.providers.jitsi import new_room_key
from app.teams.providers.registry import get_provider
from app.utils.timezone import ist_now

#: How long before its scheduled time a meeting is joinable. Early enough
#: to let people gather, not so early that tomorrow's standup shows a Join
#: button all afternoon.
JOIN_WINDOW_MINUTES = 15

DEFAULT_DURATION_MINUTES = 30
MAX_TITLE = 150


class MeetingError(Exception):
    """Something the user did wrong, with a message fit to show them."""


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------

def schedule(title, starts_at, organiser, participants=(), agenda=None,
             duration_minutes=DEFAULT_DURATION_MINUTES, client_id=None,
             channel=None, instant=False, commit=True):
    """Create a meeting. `starts_at` is IST-naive - see the module docstring.

    The organiser is always a participant: a meeting you called but are not
    invited to is not a state worth being able to reach.
    """
    title = (title or "").strip()
    if not title:
        raise MeetingError("Meeting title is required.")
    if len(title) > MAX_TITLE:
        raise MeetingError(f"Title must be {MAX_TITLE} characters or fewer.")
    if starts_at is None:
        raise MeetingError("Please choose a date and time.")

    try:
        duration = max(5, min(int(duration_minutes or DEFAULT_DURATION_MINUTES), 600))
    except (TypeError, ValueError):
        duration = DEFAULT_DURATION_MINUTES

    meeting = Meeting(
        title=title,
        meeting_date=starts_at,
        agenda=(agenda or "").strip() or None,
        client_id=client_id or None,
        duration_minutes=duration,
        status="live" if instant else "scheduled",
        created_by_id=organiser.id,
        channel_id=channel.id if channel is not None else None,
        provider=current_app.config.get("TEAMS_MEETING_PROVIDER", "jitsi"),
        # Minted at creation, not at first join: the room key IS the
        # authorisation on the public Jitsi, so it must never be derived
        # from anything about the meeting, and it must be stable for
        # everyone who opens the invite.
        room_key=new_room_key(),
        started_at=datetime.utcnow() if instant else None,
    )
    db.session.add(meeting)
    db.session.flush()

    people = {organiser.id: organiser}
    for person in participants:
        if person is not None:
            people[person.id] = person
    meeting.participants.extend(people.values())

    if commit:
        db.session.commit()
    return meeting


def start_now(title, organiser, channel=None, commit=True):
    """"Start a call" - a meeting that begins the moment it is created."""
    return schedule(
        title=title or "Quick call",
        starts_at=ist_now(),
        organiser=organiser,
        participants=_channel_members(channel),
        channel=channel,
        instant=True,
        commit=commit,
    )


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def ensure_room_key(meeting, commit=True):
    """Mint a key for a meeting that predates Teams, so it is joinable."""
    if meeting.room_key:
        return meeting.room_key
    meeting.room_key = new_room_key()
    if commit:
        db.session.commit()
    return meeting.room_key


def mark_started(meeting, commit=True):
    """First join flips it live. UTC, because it is an actual event."""
    if meeting.started_at is None:
        meeting.started_at = datetime.utcnow()
    if meeting.status == "scheduled":
        meeting.status = "live"
    if commit:
        db.session.commit()
    return meeting


def end(meeting, commit=True):
    if meeting.ended_at is None:
        meeting.ended_at = datetime.utcnow()
    meeting.status = "ended"
    if commit:
        db.session.commit()
    return meeting


def cancel(meeting, commit=True):
    meeting.status = "cancelled"
    if commit:
        db.session.commit()
    return meeting


def is_live(meeting):
    """Started and not finished. Answered from the UTC pair only."""
    return meeting.started_at is not None and meeting.ended_at is None


def ends_at(meeting):
    """Scheduled end, in IST - the same clock as meeting_date."""
    if not meeting.meeting_date:
        return None
    return meeting.meeting_date + timedelta(
        minutes=meeting.duration_minutes or DEFAULT_DURATION_MINUTES)


def is_joinable(meeting, now=None):
    """Whether the Join button should be live.

    A cancelled or ended meeting never is. A live one always is - somebody
    is in there. Otherwise it opens shortly before the scheduled time and
    stays open until the scheduled end, so a meeting that runs over does
    not lock out the person arriving late.
    """
    if meeting.status in ("cancelled", "ended"):
        return False
    if is_live(meeting):
        return True

    now = now or ist_now()          # IST, to match meeting_date
    if not meeting.meeting_date:
        return False

    opens = meeting.meeting_date - timedelta(minutes=JOIN_WINDOW_MINUTES)
    closes = ends_at(meeting)
    return opens <= now <= closes


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------

def can_join(meeting, user):
    """Who may open the room.

    Three ways in, in order of how specific they are:

      1. invited to this meeting;
      2. a member of the channel it belongs to - "huddle in #design" should
         not need everyone individually invited;
      3. if it belongs to no channel at all, any active member of staff.

    Rule 3 matches the module the old meetings page had, where every
    meeting was visible to everyone. Narrowing that quietly would strand
    people out of meetings they have always been able to see.
    """
    if user is None or getattr(user, "status", None) != "active":
        return False

    if any(p.id == user.id for p in meeting.participants):
        return True

    if meeting.channel_id:
        from app.teams.services.channels import membership
        return membership(meeting.channel_id, user.id) is not None

    return True


def join_context(meeting, user):
    """Everything the join page needs, or None if the provider is missing."""
    provider = get_provider(meeting.provider)
    if provider is None:
        return None

    ensure_room_key(meeting)
    moderator = meeting.created_by_id == user.id

    return {
        "provider": provider,
        "embed": provider.embed_config(meeting, user, moderator=moderator),
        "fallback_url": provider.join_url(meeting, user),
        "moderator": moderator,
    }


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------

def upcoming_for(user, limit=50):
    """Meetings this person can still attend, soonest first."""
    now = ist_now() - timedelta(minutes=JOIN_WINDOW_MINUTES)
    rows = (
        Meeting.query
        .filter(
            Meeting.meeting_date >= now,
            Meeting.status.notin_(("cancelled", "ended")),
        )
        .order_by(Meeting.meeting_date.asc())
        .limit(limit)
        .all()
    )
    return [m for m in rows if can_join(m, user)]


def past_for(user, limit=50):
    now = ist_now()
    rows = (
        Meeting.query
        .filter(
            db.or_(Meeting.meeting_date < now,
                   Meeting.status.in_(("cancelled", "ended"))),
        )
        .order_by(Meeting.meeting_date.desc())
        .limit(limit)
        .all()
    )
    return [m for m in rows if can_join(m, user)]


def live_for(user):
    """Meetings happening right now that `user` may join - the banner."""
    rows = (
        Meeting.query
        .filter(Meeting.started_at.isnot(None), Meeting.ended_at.is_(None),
                Meeting.status == "live")
        .order_by(Meeting.started_at.desc())
        .limit(10)
        .all()
    )
    return [m for m in rows if can_join(m, user)]


def invitable_users(exclude_id=None):
    query = User.query.filter(User.status == "active")
    if exclude_id:
        query = query.filter(User.id != exclude_id)
    return query.order_by(User.name.asc()).all()


def _channel_members(channel):
    if channel is None:
        return []
    return [m.user for m in channel.members if m.user is not None]


def channel_for(channel_id):
    return db.session.get(TeamChannel, channel_id) if channel_id else None
