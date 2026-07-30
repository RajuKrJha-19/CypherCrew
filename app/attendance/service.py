"""Attendance orchestration: the platform-agnostic layer the routes, the
worker and the emulator all call. Zoho specifics stay in zoho_client.

Rules encoded here:
* Effective check-in source defaults to 'zoho' (employees) unless a user is
  explicitly set to 'software' (interns not on Zoho).
* 'software' users check in AND out in this app. 'zoho' users check in on
  Zoho only; they may check OUT here, which writes back to Zoho.
* "Checked in right now" == an AttendanceSession with check_out_at IS NULL.
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    AttendanceSession, AttendanceSettings, User, ZohoConnection,
)
from app.utils import task_status
from app.utils.timezone import IST_OFFSET


def get_settings():
    """The singleton idle-alert settings row, created from config defaults on
    first use so the admin page always has something to edit."""
    from flask import current_app

    row = AttendanceSettings.query.get(1)
    if row is None:
        cfg = current_app.config
        row = AttendanceSettings(
            id=1,
            idle_alerts_enabled=True,
            grace_min=cfg.get("ATTENDANCE_IDLE_GRACE_MIN", 15),
            repeat_min=cfg.get("ATTENDANCE_IDLE_REPEAT_MIN", 10),
            escalate_enabled=True,
            escalate_after=cfg.get("ATTENDANCE_ESCALATE_AFTER", 3),
            snooze_min=cfg.get("ATTENDANCE_SNOOZE_MIN", 15),
        )
        db.session.add(row)
        try:
            db.session.commit()
        except IntegrityError:          # a concurrent first-use created it
            db.session.rollback()
            row = AttendanceSettings.query.get(1)
    return row


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def source_of(user):
    """'zoho' or 'software'. Default 'zoho' - the workforce is on Zoho and
    interns are the explicit exception an admin flips to 'software'."""
    return "software" if (getattr(user, "checkin_source", None) == "software") \
        else "zoho"


def active_connection():
    """The org's active Zoho connection, or None (e.g. not yet connected, or
    in simulation mode where no real connection is needed)."""
    return ZohoConnection.query.filter_by(status="active").order_by(
        ZohoConnection.id.desc()).first()


def current_open_session(user_id):
    """The user's open session (checked-in), or None."""
    return AttendanceSession.query.filter(
        AttendanceSession.user_id == user_id,
        AttendanceSession.check_out_at.is_(None),
    ).order_by(AttendanceSession.check_in_at.desc()).first()


def has_active_task(user_id):
    """True when the user has a task In Progress with its timer running -
    the same shape as the one-active-task invariant in tasks.py."""
    from app.models import Task

    return db.session.query(Task.id).filter(
        Task.assigned_to_id == user_id,
        Task.status == task_status.IN_PROGRESS,
        Task.timer_started_at.isnot(None),
    ).first() is not None


def _ist_label(when, fmt="%I:%M %p"):
    return (when + IST_OFFSET).strftime(fmt).lstrip("0") if when else None


def status_for(user):
    """The top-bar poller payload."""
    session = current_open_session(user.id)
    src = source_of(user)
    checked_in = session is not None
    return {
        "checked_in": checked_in,
        "source": src,
        "since_iso": session.check_in_at.isoformat() + "Z" if checked_in else None,
        "since_label": _ist_label(session.check_in_at) if checked_in else None,
        "can_checkin": (src == "software") and not checked_in,
        "can_checkout": checked_in,
        "has_active_task": has_active_task(user.id) if checked_in else True,
        "snoozed_until": (
            session.snooze_until.isoformat() + "Z"
            if checked_in and session.snooze_until else None
        ),
    }


# ---------------------------------------------------------------------------
# Writes (user-driven)
# ---------------------------------------------------------------------------

class AttendanceError(Exception):
    """A check-in/out that cannot proceed (with a user-facing message)."""


def checkin_user(user):
    """Open a software-sourced session. Refuses zoho-source users - they
    check in on Zoho, never here (defence in depth, enforced server-side)."""
    if source_of(user) != "software":
        raise AttendanceError(
            "Your attendance is managed by Zoho People - please check in there.")
    existing = current_open_session(user.id)
    if existing is not None:
        return existing  # already in, idempotent
    session = AttendanceSession(
        user_id=user.id, source="software", check_in_at=datetime.utcnow())
    db.session.add(session)
    try:
        db.session.commit()
    except IntegrityError:
        # A concurrent check-in (double-click / second tab) won the race
        # against the partial-unique "one open session per user" index. Treat
        # the conflict as the idempotent "already checked in" outcome rather
        # than 500-ing - the user IS checked in.
        db.session.rollback()
        return current_open_session(user.id)
    return session


def checkout_user(user):
    """Close the user's open session. For zoho-source users this also writes
    the check-out back to Zoho; if that call fails the local checkout still
    lands and is flagged for the worker to retry (the user never loses it)."""
    session = current_open_session(user.id)
    if session is None:
        raise AttendanceError("You are not checked in.")

    now = datetime.utcnow()
    if source_of(user) == "zoho":
        from app.attendance import zoho_client
        try:
            zoho_client.checkout(active_connection(), user, now)
            session.checkout_pending_zoho = False
        except Exception:  # noqa: BLE001 - never lose the user's checkout
            session.checkout_pending_zoho = True

    session.check_out_at = now
    db.session.commit()
    return session


def snooze(user, minutes=None):
    """Suppress idle alerts for `minutes` (default from settings). No-op if
    not checked in."""
    from datetime import timedelta

    session = current_open_session(user.id)
    if session is None:
        return None
    if minutes is None:
        minutes = get_settings().snooze_min
    session.snooze_until = datetime.utcnow() + timedelta(minutes=minutes)
    db.session.commit()
    return session


# ---------------------------------------------------------------------------
# Sync (Zoho -> local sessions)
# ---------------------------------------------------------------------------

def _user_by_email(email):
    if not email:
        return None
    return User.query.filter(db.func.lower(User.email) == email).first()


def sync_attendance():
    """Pull Zoho attendance and reconcile it into local sessions. Idempotent
    and safe to run per-worker + per-webhook. Returns a summary dict."""
    from app.attendance import zoho_client

    conn = active_connection()
    # Real mode needs a connection; simulation does not.
    if conn is None and not zoho_client.simulation():
        return {"checked": 0, "opened": 0, "closed": 0, "skipped": 0,
                "note": "no active Zoho connection"}

    _retry_pending_checkouts(conn)

    entries = zoho_client.get_entries(conn)
    checked = opened = closed = skipped = 0

    for entry in entries:
        user = _user_by_email(entry.get("email"))
        if user is None or source_of(user) != "zoho":
            skipped += 1
            continue
        checked += 1

        eid = entry.get("entry_id")
        # Match an OPEN session only. Zoho's per-entry id can be missing and
        # fall back to the email (identical across days), so matching a closed
        # row by id would resurrect yesterday's session and rewrite its
        # check-in - leaving the user shown as checked out. Scope strictly to
        # the currently-open span.
        session = current_open_session(user.id)
        if session is None and eid:
            session = AttendanceSession.query.filter_by(
                zoho_entry_id=eid, check_out_at=None).first()

        if entry.get("check_out_utc") is None:
            # Still checked in on Zoho.
            if session is None:
                # Insert inside a SAVEPOINT: the in-process worker runs in
                # every gunicorn process, and the webhook + cron endpoint can
                # sync concurrently, so two runs can both see "no open session"
                # and race on the partial-unique index. On a collision we roll
                # back just this insert and adopt the row the other run made.
                try:
                    with db.session.begin_nested():
                        session = AttendanceSession(
                            user_id=user.id, source="zoho",
                            check_in_at=(entry.get("check_in_utc")
                                         or datetime.utcnow()),
                            zoho_entry_id=eid)
                        db.session.add(session)
                        db.session.flush()
                    opened += 1
                except IntegrityError:
                    session = current_open_session(user.id)
            else:
                if eid and not session.zoho_entry_id:
                    session.zoho_entry_id = eid
                # Only refresh the check-in time on a genuine zoho session -
                # never rewrite a software session's start if the source was
                # flipped to zoho mid-shift.
                if entry.get("check_in_utc") and session.source == "zoho":
                    session.check_in_at = entry["check_in_utc"]
            if session is not None:
                session.last_synced_at = datetime.utcnow()
        else:
            # Checked out on Zoho: close the open session, if any.
            if session is not None and session.check_out_at is None:
                session.check_out_at = entry.get("check_out_utc") or datetime.utcnow()
                session.last_synced_at = datetime.utcnow()
                closed += 1

    db.session.commit()
    return {"checked": checked, "opened": opened, "closed": closed,
            "skipped": skipped}


def _retry_pending_checkouts(conn):
    """Re-attempt Zoho write-back for checkouts that landed locally but whose
    Zoho call failed earlier."""
    from app.attendance import zoho_client

    if zoho_client.simulation():
        # Simulation write-back never fails, so nothing pends.
        return
    pending = AttendanceSession.query.filter_by(
        checkout_pending_zoho=True).all()
    for session in pending:
        user = User.query.get(session.user_id)
        if user is None:
            session.checkout_pending_zoho = False
            continue
        try:
            zoho_client.checkout(conn, user, session.check_out_at)
            session.checkout_pending_zoho = False
        except Exception:  # noqa: BLE001 - stays pending for the next run
            pass
