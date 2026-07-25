"""Failure Recovery - bring dead-lettered / failed jobs back to life.

When a publish exhausts its retries (state 'dead') or hits an auth wall
(state 'failed'), it stops and waits for a human. This module lets an
operator requeue it: attempts reset, provider_state cleared so the multi-
step publish restarts cleanly, and the target flipped back to 'publishing'.
Every requeue is audited.
"""

from datetime import datetime

from app.extensions import db
from app.models import PublishJob
from app.social.services import audit


RECOVERABLE_STATES = ("dead", "failed")


def dead_jobs(limit=100):
    return (
        PublishJob.query
        .filter(PublishJob.state.in_(RECOVERABLE_STATES))
        .order_by(PublishJob.updated_at.desc())
        .limit(limit)
        .all()
    )


def requeue_job(job, actor_id=None, commit=False):
    """Reset a dead/failed job to run again immediately. No-op (returns
    False) if the job isn't in a recoverable state."""
    if job.state not in RECOVERABLE_STATES:
        return False

    job.state = "queued"
    job.attempts = 0
    job.last_error = None
    job.next_run_at = datetime.utcnow()
    job.locked_at = None
    job.locked_by = None

    # Clear the resumption markers so the publish restarts from step one
    # (a fresh attempt, not a resume of a half-finished remote op).
    provider_state = dict(job.provider_state or {})
    provider_state.pop("started", None)
    provider_state.pop("_reserved", None)
    job.provider_state = provider_state or None

    target = job.target
    task_id = target.post.task_id if (target and target.post) else None
    if target is not None and target.status == "failed":
        target.status = "publishing"
        target.last_error = None

    audit.record(
        "job_requeued",
        target_id=job.target_id,
        post_id=(target.social_post_id if target else None),
        actor_id=actor_id,
        task_id=task_id,
        detail={"job_id": job.id},
        message="Requeued for another publish attempt",
    )

    if commit:
        db.session.commit()
    return True


def requeue_all_dead(actor_id=None):
    jobs = dead_jobs(limit=500)
    count = sum(1 for j in jobs if requeue_job(j, actor_id=actor_id))
    db.session.commit()
    return count
