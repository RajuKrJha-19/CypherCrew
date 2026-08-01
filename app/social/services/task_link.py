"""Bridge: reflect Social Studio publishing back onto the originating Task.

When a SocialPost is created from a task (post.task_id set), the task's own
lifecycle stays the single source of truth. One derivation, `_derive`, reads
the linked posts' targets and maps them to a task board column + a publish
sub-state badge, so the kanban/list/detail always agree:

    draft in Studio / no channels  -> Scheduled column, "Draft in Social Studio"
    scheduled for later            -> Scheduled column, "Scheduled"
    in the publish queue (now)     -> Published column, "In publish queue"
    all targets live               -> Published column, "Live" (+ completed_at)
    a target failed                -> Published column, "Publish failed · retry"
    marked manually published      -> Published column, "Published outside Studio"

Board column stays either Scheduled or Published (both already exist); the
badge carries the nuance. `completed_at`/deliverable count are stamped ONLY
when every target is truly live, so throughput metrics never count an
in-queue or failed post as done.

Safe to call from the background worker: never touches current_user; status
timing reuses the task module's record_status_time so duration buckets stay
correct; TaskActivity is written with a nullable actor (system convention).
"""

from datetime import datetime

from app.extensions import db
from app.utils import social_platforms as sp


def _task_of(post):
    if not post or not post.task_id:
        return None
    from app.models import Task
    return db.session.get(Task, post.task_id)


#: Key on flask.g holding {task_id: [posts]} for a primed batch. Absent unless
#: prime_badges() ran, so nothing outside a list render is affected.
_PRIME_KEY = "_task_link_posts"


def prime_badges(tasks):
    """Load the Studio posts for a whole page of tasks in one go.

    publish_badge() is rendered twice per row on the task list (the row itself
    and the board card), and each call went through linked_posts(), which is a
    fresh query - then touched post.targets, which lazy-loads another. On a
    list of social tasks that is four queries per task before anything is
    drawn, and the list has no pagination, so it grew with the table forever.

    Priming turns the whole page into two queries. The cache is deliberately
    opt-in and per-request: only a caller that has just loaded a batch of
    tasks knows the data is fresh, so nothing else - the worker, the publish
    path, the detail page - reads it or has to think about invalidating it.
    """
    from flask import g, has_request_context

    if not tasks or not has_request_context():
        return

    from sqlalchemy.orm import selectinload

    from app.models import SocialPost

    ids = [t.id for t in tasks if getattr(t, "is_social_media", False)]
    if not ids:
        # Still mark the batch primed: a page of non-social tasks must not
        # fall through to a query each.
        setattr(g, _PRIME_KEY, {})
        return

    posts = (SocialPost.query
             .options(selectinload(SocialPost.targets))
             .filter(SocialPost.task_id.in_(ids),
                     SocialPost.status != "removed")
             .order_by(SocialPost.created_at.asc())
             .all())

    grouped = {task_id: [] for task_id in ids}
    for post in posts:
        grouped.setdefault(post.task_id, []).append(post)

    setattr(g, _PRIME_KEY, grouped)


def linked_posts(task):
    """Non-removed Social Studio posts created from this task, newest last."""
    if task is None:
        return []

    from flask import g, has_request_context

    if has_request_context():
        primed = getattr(g, _PRIME_KEY, None)
        if primed is not None and task.id in primed:
            return primed[task.id]

    from app.models import SocialPost
    return (SocialPost.query
            .filter(SocialPost.task_id == task.id,
                    SocialPost.status != "removed")
            .order_by(SocialPost.created_at.asc())
            .all())


def _derive(task):
    """Map a task's linked posts to (board_status, badge). badge is a dict the
    templates render, or None when the task has no Studio post yet."""
    posts = linked_posts(task)
    if not posts:
        return None, None

    if any(p.published_externally for p in posts):
        return "Published", {
            "label": "Published outside Studio", "tone": "muted",
            "icon": "fa-arrow-up-right-from-square", "post_id": posts[-1].id}

    targets = [t for p in posts for t in p.targets if t.status != "removed"]
    pid = posts[-1].id

    if not targets:
        # Draft created but the client has no channels bound yet.
        return "Scheduled", {
            "label": "Draft in Studio · no channels bound", "tone": "warning",
            "icon": "fa-triangle-exclamation", "post_id": pid,
            "needs_channels": True}

    statuses = [t.status for t in targets]
    now = datetime.utcnow()
    # A target scheduled for now-or-past is due for immediate publish - it's in
    # the queue even if the worker hasn't flipped it to "publishing" yet.
    due_now = any(t.status == "scheduled" and t.scheduled_for
                  and t.scheduled_for <= now for t in targets)
    sched_future = any(t.status == "scheduled" and t.scheduled_for
                       and t.scheduled_for > now for t in targets)

    if all(s == "published" for s in statuses):
        links = [t.permalink for t in targets if t.permalink]
        return "Published", {
            "label": "Live", "tone": "success", "icon": "fa-circle-check",
            "post_id": pid, "permalinks": links}

    # "blocked" is terminal like "failed" (a target that cannot publish as it
    # stands). Without it here, a post live on one channel but blocked on
    # another fell through to "Draft in Social Studio" - the task board said
    # un-published while the post was actually live, and completed_at never
    # stamped. Treat it as delivered-with-a-problem, distinguishing the partial
    # case so the label isn't misleading.
    if any(s in ("failed", "blocked") for s in statuses):
        some_live = any(s == "published" for s in statuses)
        return "Published", {
            "label": "Partially published · retry" if some_live
            else "Publish failed · retry",
            "tone": "warning" if some_live else "danger",
            "icon": "fa-triangle-exclamation", "post_id": pid, "retry": True}

    if any(s == "publishing" for s in statuses) or due_now:
        return "Published", {
            "label": "In publish queue", "tone": "warning",
            "icon": "fa-paper-plane", "post_id": pid}

    if sched_future:
        return "Scheduled", {
            "label": "Scheduled", "tone": "info", "icon": "fa-clock",
            "post_id": pid}

    # draft / pending_approval / approved: handed to Studio, not yet published.
    return "Scheduled", {
        "label": "Draft in Social Studio", "tone": "warning",
        "icon": "fa-pen-to-square", "post_id": pid}


def publish_badge(task):
    """Render-time badge for kanban/list/detail. None for non-social tasks or
    tasks with no linked Studio post."""
    if not task or not getattr(task, "is_social_media", False):
        return None
    _, badge = _derive(task)
    return badge


def sync_task_from_posts(task, actor_id=None):
    """Set the task's board column from its linked Studio posts. Idempotent -
    only writes a status change when the column actually moves, and stamps
    completed_at exactly once, when every target is live."""
    if task is None:
        return
    desired, badge = _derive(task)
    if desired is None:
        return
    live = bool(badge) and badge.get("label") in ("Live",
                                                   "Published outside Studio")

    # A task that already reached Published is terminal for downward moves: a
    # newly linked/rescheduled post must never drag a completed task back to
    # Scheduled (which would bank time backward while completed_at stays set).
    if task.completed_at and desired != "Published":
        return

    from app.routes.tasks import record_status_time
    from app.models import TaskActivity

    if desired != task.status:
        old = record_status_time(task, desired)
        db.session.add(TaskActivity(
            task_id=task.id, actor_id=actor_id, action="social_publish_state",
            message="Social Studio: " + (badge.get("label") if badge else desired),
            old_status=old, new_status=desired, created_at=datetime.utcnow()))

    if live and not task.completed_at:
        from app.utils.timezone import ist_now
        from app.models import Task
        from app.services import deliverables

        # Re-read the task under its own row lock before stamping. This runs
        # from the publish worker's thread pool, in every gunicorn worker, and
        # the caller (_maybe_finalize_post) locks the SocialPost only - so two
        # posts finishing for one task, or two tasks on one deliverable, both
        # saw completed_at as NULL here and both counted a delivery.
        #
        # The lock is taken only on the path that actually writes, so the
        # common case (a target settling with nothing to finalise) still costs
        # nothing.
        locked = (
            db.session.query(Task)
            .filter_by(id=task.id)
            .populate_existing()
            .with_for_update()
            .first()
        )
        if locked is None or locked.completed_at:
            return

        locked.completed_at = ist_now()
        deliverables.adjust_count(locked.deliverable_id, +1)
        posts = linked_posts(task)
        plats = sorted({t.platform for p in posts for t in p.targets
                        if t.status == "published"})
        if plats:
            task.social_platforms_published = sp.format_platforms(plats)


# -- Thin wrappers kept for existing call sites ---------------------------
# They all route through the single derivation above, so behaviour stays
# consistent no matter which event fired.

def mark_task_scheduled(post, actor_id=None):
    sync_task_from_posts(_task_of(post), actor_id=actor_id)


def mark_task_unscheduled(post, actor_id=None):
    sync_task_from_posts(_task_of(post), actor_id=actor_id)


def mark_task_published(post, actor_id=None):
    sync_task_from_posts(_task_of(post), actor_id=actor_id)
