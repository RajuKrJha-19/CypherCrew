"""Idle-task alerts.

Nudges anyone who is checked in (an open AttendanceSession) but has no task
In Progress - the common case where someone is working but forgot to start
the timer, so their tasks look paused. After a grace window from check-in
they are reminded every REPEAT minutes; once the nudges pile up their
manager is looped in. A "Snooze" on the alert suppresses it for a break.

Same shape as app.services.reminders / task_fallback: no in-process
scheduler here - either the attendance worker or system cron hitting
/internal/attendance/idle-alerts drives it. Idempotent + row-locked, so
overlapping runs never double-nudge.
"""

from datetime import datetime, timedelta

from flask import url_for

from app.attendance import service
from app.extensions import db
from app.models import AttendanceSession, User
from app.utils import roles
from app.utils.notifications import create_notification

#: Public so the notification API can flag an idle-alert item (the browser
#: plays the distinct buzzer for these rather than the normal chime).
IDLE_ALERT_TITLE = "You're checked in - but no task is running"
_ALERT_TITLE = IDLE_ALERT_TITLE
_ESCALATE_TITLE = "Team member idle"


def _tasks_link():
    """The task list link. Built defensively: this service runs from the
    background worker (app context, no request) as well as the internal
    endpoint (request context), and url_for needs a request/SERVER_NAME. The
    path is stable (tasks_bp is mounted at /tasks), so fall back to it."""
    try:
        return url_for("tasks.list_tasks")
    except RuntimeError:
        return "/tasks/"


def _managers_for(user):
    """Recipients to escalate an idle alert to. Role-derived (there is no
    per-user reporting-manager); falls back to the owner so it is never
    empty."""
    values = roles.manager_role_values(getattr(user, "role", None))
    recipients = User.query.filter(
        User.role.in_(list(values)),
        User.status == "active",
        User.id != user.id,
    ).all()
    if not recipients:
        recipients = User.query.filter(
            User.role.in_(list(roles.MANAGEMENT_ROLES)),
            User.status == "active",
            User.id != user.id,
        ).all()
    return recipients


def run_idle_task_alerts():
    """Nudge idle checked-in users; escalate the persistent ones.

    Returns {checked, alerted, escalated, skipped}.
    """
    settings = service.get_settings()
    # Master switch - the admin "buzzer on/off". When off, nobody is nudged.
    if not settings.idle_alerts_enabled:
        return {"checked": 0, "alerted": 0, "escalated": 0, "skipped": 0,
                "disabled": True}

    grace = timedelta(minutes=settings.grace_min)
    repeat = timedelta(minutes=max(1, settings.repeat_min))
    escalate_after = settings.escalate_after
    escalate_enabled = settings.escalate_enabled
    escalate_window = repeat * max(1, escalate_after)

    now = datetime.utcnow()

    open_ids = [
        row.id for row in AttendanceSession.query
        .filter(AttendanceSession.check_out_at.is_(None))
        .with_entities(AttendanceSession.id)
        .all()
    ]

    checked = alerted = escalated = skipped = 0

    for session_id in open_ids:
        # Lock the row and re-check under the lock, so two overlapping runs
        # never both nudge the same session.
        session = (
            AttendanceSession.query
            .filter(AttendanceSession.id == session_id)
            .with_for_update(skip_locked=True)
            .first()
        )
        if session is None or session.check_out_at is not None:
            continue

        checked += 1
        user = User.query.get(session.user_id)
        if user is None or user.status != "active":
            skipped += 1
            continue

        # Grace window after check-in.
        if now < session.check_in_at + grace:
            skipped += 1
            continue

        # Snoozed for a break.
        if session.snooze_until and now < session.snooze_until:
            skipped += 1
            continue

        # Actually working: clear the idle streak so escalation restarts fresh
        # next time they go idle. last_idle_alert_at is cleared too, so the
        # very next idle spell nudges promptly instead of waiting out a stale
        # repeat interval.
        if service.has_active_task(user.id):
            if (session.idle_alert_count or session.last_escalated_at
                    or session.last_idle_alert_at):
                session.idle_alert_count = 0
                session.last_escalated_at = None
                session.last_idle_alert_at = None
            skipped += 1
            continue

        # Idle - but don't re-nudge inside the repeat interval.
        if session.last_idle_alert_at and now < session.last_idle_alert_at + repeat:
            skipped += 1
            continue

        create_notification(
            user_id=user.id,
            title=_ALERT_TITLE,
            message=(
                "You're checked in but none of your tasks is In Progress. "
                "Start a task so your time is tracked."
            ),
            link=_tasks_link(),
            category="activity",
        )
        session.idle_alert_count = (session.idle_alert_count or 0) + 1
        session.last_idle_alert_at = now
        alerted += 1

        # Escalate once per window after enough consecutive nudges.
        if escalate_enabled and session.idle_alert_count >= escalate_after and (
            not session.last_escalated_at
            or now >= session.last_escalated_at + escalate_window
        ):
            for manager in _managers_for(user):
                create_notification(
                    user_id=manager.id,
                    title=_ESCALATE_TITLE,
                    message=(
                        f"{user.name} has been checked in with no task running "
                        f"for a while."
                    ),
                    link=_tasks_link(),
                    category="activity",
                )
            session.last_escalated_at = now
            escalated += 1

    db.session.commit()
    return {"checked": checked, "alerted": alerted,
            "escalated": escalated, "skipped": skipped}
