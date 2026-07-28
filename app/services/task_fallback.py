"""Backup-assignee fallback for tasks that stall in Assigned.

A manager can pick a backup employee and a fallback window (hours) when
assigning a task. If the task is still sitting in Assigned - the
assignee never moved it to In Progress - past that window, it auto-
shifts to the backup.

Same shape as app.services.reminders: no in-process scheduler, this
runs when system cron hits the token-protected
/internal/task-fallback/run endpoint. fallback_triggered_at makes the
shift a one-time move - once set, a task is never picked up again even
if it later returns to Assigned, so it can't bounce back and forth
between the original assignee and the backup.
"""

from datetime import datetime, timedelta

from flask import url_for

from app.extensions import db
from app.models import Task, TaskActivity
from app.utils import task_status
from app.utils.notifications import create_notification


def run_task_fallback_reassignment():
    """Shift stalled tasks to their backup assignee.

    Returns a summary dict {checked, shifted}.
    """

    now = datetime.utcnow()

    candidates = Task.query.filter(
        Task.status == task_status.ASSIGNED,
        Task.backup_assignee_id.isnot(None),
        Task.fallback_hours.isnot(None),
        Task.fallback_triggered_at.is_(None),
        Task.status_started_at.isnot(None),
    ).all()

    shifted = 0

    for candidate in candidates:

        # Lock the row and re-read it before shifting. This endpoint is hit by
        # system cron; if two runs overlap, both would otherwise see the same
        # fallback_triggered_at IS NULL candidate and shift it twice - double
        # notifications, and record_status_time banked twice. FOR UPDATE
        # SKIP LOCKED lets a second run pass over a row the first still holds,
        # and the guard is re-checked under the lock because the task's state
        # (status, fallback_triggered_at) may have changed since the scan.
        task = (
            Task.query
            .filter(Task.id == candidate.id)
            .with_for_update(skip_locked=True)
            .first()
        )

        if task is None:
            continue

        if (
            task.status != task_status.ASSIGNED
            or task.fallback_triggered_at is not None
            or not task.backup_assignee_id
            or task.fallback_hours is None
            or task.status_started_at is None
        ):
            continue

        deadline = task.status_started_at + timedelta(
            hours=task.fallback_hours
        )

        if now < deadline:
            continue

        # The backup may have gone inactive since assignment - skip
        # rather than hand a task to a deactivated account. It stays
        # a candidate for the next run in case the account is
        # reactivated, or a manager picks a different backup.
        backup = task.backup_assignee

        if not backup or backup.status != "active":
            continue

        original_assignee = task.assigned_to

        # Bank the time already spent in Assigned into pending_seconds before
        # re-arming the clock, so the original assignee's pending time isn't
        # discarded on the shift. record_status_time also resets
        # status_started_at to now (same status, so it just re-arms).
        from app.routes.tasks import record_status_time
        record_status_time(task, "Assigned")

        task.assigned_to_id = task.backup_assignee_id
        task.employee_completed = False
        task.employee_completed_at = None
        task.fallback_triggered_at = now

        db.session.add(
            TaskActivity(
                task_id=task.id,
                actor_id=None,
                action="auto_reassigned",
                message=(
                    f"Auto-shifted to {backup.name} - "
                    f"{original_assignee.name if original_assignee else 'the assignee'} "
                    f"did not start it within {task.fallback_hours}h of "
                    "being assigned."
                ),
            )
        )

        link = url_for("tasks.task_detail", task_id=task.id)

        create_notification(
            user_id=task.backup_assignee_id,
            title="Task auto-assigned to you",
            message=(
                f"'{task.title}' was shifted to you as backup - the "
                "original assignee didn't start it in time."
            ),
            link=link,
            task_id=task.id,
        )

        if original_assignee:
            create_notification(
                user_id=original_assignee.id,
                title="Task reassigned",
                message=(
                    f"'{task.title}' was auto-shifted to {backup.name} "
                    f"after {task.fallback_hours}h with no progress."
                ),
                link=link,
                task_id=task.id,
            )

        shifted += 1

    db.session.commit()

    return {"checked": len(candidates), "shifted": shifted}
