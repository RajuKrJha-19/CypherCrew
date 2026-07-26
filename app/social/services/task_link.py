"""Bridge: reflect Social Studio publishing back onto the originating Task.

When a SocialPost is created from a task (post.task_id set), the task's own
lifecycle stays the single source of truth: a real API publish moves the task
to Published and records which platforms went live - the automated equivalent
of the manual "did you publish on X?" confirmation gate.

Safe to call from the background worker: it never touches current_user (it
would be Anonymous there). Status timing reuses the task module's
record_status_time so the duration buckets stay correct; the TaskActivity is
written directly with a nullable actor (the system convention).
"""

from datetime import datetime

from app.extensions import db
from app.utils import social_platforms as sp


def _task_of(post):
    if not post or not post.task_id:
        return None
    from app.models import Task
    return db.session.get(Task, post.task_id)


def mark_task_scheduled(post, actor_id=None):
    """Move the task to 'Scheduled' when its post is scheduled to auto-publish.
    It flips to 'Published' automatically once the post goes live."""
    task = _task_of(post)
    if task is None or task.status in ("Published", "Scheduled"):
        return
    from app.routes.tasks import record_status_time
    from app.models import TaskActivity
    old = record_status_time(task, "Scheduled")
    db.session.add(TaskActivity(
        task_id=task.id, actor_id=actor_id, action="social_scheduled",
        message="Scheduled via Social Studio - publishes automatically at the "
        "set time.",
        old_status=old, new_status="Scheduled", created_at=datetime.utcnow()))


def mark_task_unscheduled(post, actor_id=None):
    """A scheduled post was reopened/unscheduled - send the task back to Client
    Review so it can be re-scheduled or re-approved."""
    task = _task_of(post)
    if task is None or task.status != "Scheduled":
        return
    from app.routes.tasks import record_status_time
    from app.models import TaskActivity
    old = record_status_time(task, "Client Review")
    db.session.add(TaskActivity(
        task_id=task.id, actor_id=actor_id, action="social_unscheduled",
        message="Unscheduled in Social Studio - back to Client Review.",
        old_status=old, new_status="Client Review",
        created_at=datetime.utcnow()))


def mark_task_published(post, actor_id=None):
    """Once every target of a task-linked post is live, move the task to
    Published and stamp the platforms that went out. Idempotent."""
    task = _task_of(post)
    if task is None or task.status == "Published":
        return
    platforms = sorted({t.platform for t in post.targets
                        if t.status == "published"})
    if not platforms:
        return
    task.social_platforms_published = sp.format_platforms(platforms)
    # Reuse the task module's timing-aware status change (no current_user use).
    from app.routes.tasks import record_status_time
    from app.models import TaskActivity
    from app.utils.timezone import ist_now
    old = record_status_time(task, "Published")
    # Mirror the manual approve_task publish path so Social-Studio publishes
    # count in throughput/turnaround metrics (which filter on completed_at) and
    # the deliverable tally, instead of silently dropping out.
    task.completed_at = ist_now()
    if task.deliverable is not None:
        task.deliverable.completed_count = \
            (task.deliverable.completed_count or 0) + 1
    db.session.add(TaskActivity(
        task_id=task.id, actor_id=actor_id, action="published",
        message=("Published via Social Studio: "
                 + ", ".join(sp.label(p) for p in platforms) + "."),
        old_status=old, new_status="Published",
        created_at=datetime.utcnow()))
