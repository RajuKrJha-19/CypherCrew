"""Deadline reminders.

Notifies assignees of tasks that are overdue or due within the next day.
There is no in-process scheduler; this runs once a day when system cron
hits the token-protected /internal/reminders/run endpoint.

The core is idempotent: it will not create a second reminder for the same
task within ~a day, so a cron retry or a manual re-run never double-sends.
In-app notification only (no daily email spam) - the notification bell is
the source of truth; email copies can be turned on later if wanted.
"""

from datetime import datetime, timedelta

from flask import url_for

from app.extensions import db
from app.models import Task, Notification
from app.utils import task_status
from app.utils.notifications import create_notification
from app.utils.timezone import ist_now


#: Titles this job uses - also how it recognises its own recent reminders
#: for the idempotency check.
REMINDER_TITLES = ("Task overdue", "Task due soon")

_ACTIVE_STATUSES = [
    task_status.ASSIGNED,
    task_status.IN_PROGRESS,
    task_status.PAUSED,
]


def send_deadline_reminders(due_within_hours=24):
    """Remind assignees about tasks overdue or due within the window.

    Returns a small summary dict {checked, sent, skipped}. Safe to call
    repeatedly - `skipped` counts tasks that already had a reminder in the
    last ~day.
    """
    now = ist_now()
    horizon = now + timedelta(hours=due_within_hours)

    tasks = Task.query.filter(
        Task.deadline.isnot(None),
        Task.deadline <= horizon,
        Task.status.in_(_ACTIVE_STATUSES),
        Task.assigned_to_id.isnot(None),
    ).all()

    # A time-window check (not a calendar-day one) keeps this free of any
    # timezone edge cases around midnight.
    cutoff = datetime.utcnow() - timedelta(hours=20)

    sent = 0
    skipped = 0

    for task in tasks:

        already = Notification.query.filter(
            Notification.task_id == task.id,
            Notification.user_id == task.assigned_to_id,
            Notification.title.in_(REMINDER_TITLES),
            Notification.created_at >= cutoff,
        ).first()

        if already:
            skipped += 1
            continue

        overdue = task.deadline < now
        when = task.deadline.strftime("%d %b, %I:%M %p")

        if overdue:
            title = "Task overdue"
            message = f"'{task.title}' is overdue - it was due {when}."
        else:
            title = "Task due soon"
            message = f"'{task.title}' is due soon ({when})."

        create_notification(
            user_id=task.assigned_to_id,
            title=title,
            message=message,
            link=url_for("tasks.task_detail", task_id=task.id),
            task_id=task.id,
        )
        sent += 1

    db.session.commit()

    return {"checked": len(tasks), "sent": sent, "skipped": skipped}
