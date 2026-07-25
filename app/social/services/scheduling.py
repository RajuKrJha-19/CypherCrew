"""SchedulingService - engine-owned scheduling.

Targets carry `scheduled_for` (UTC). When due, a PublishJob is created with
a deterministic idempotency_key so the same target/schedule is never
enqueued twice, even if the scheduler cron overlaps a slow run.
"""

from datetime import datetime

from app.extensions import db
from app.models import PublishJob, SocialPostTarget


def _idempotency_key(target):
    ts = int(target.scheduled_for.timestamp()) if target.scheduled_for else 0
    return f"tgt-{target.id}-{ts}"


def schedule_target(target, when, actor_id=None):
    """Mark a target to publish at `when` (UTC)."""
    target.scheduled_for = when
    target.status = "scheduled"
    target.updated_at = datetime.utcnow()
    return target


def enqueue_due(now=None):
    """Create PublishJobs for every target whose time has come. Idempotent.
    Returns a summary dict."""
    now = now or datetime.utcnow()

    due = (
        SocialPostTarget.query
        .filter(
            SocialPostTarget.status == "scheduled",
            SocialPostTarget.scheduled_for.isnot(None),
            SocialPostTarget.scheduled_for <= now,
        )
        .all()
    )

    enqueued = 0
    for target in due:
        key = _idempotency_key(target)
        if PublishJob.query.filter_by(idempotency_key=key).first():
            continue
        db.session.add(PublishJob(
            target_id=target.id,
            state="queued",
            idempotency_key=key,
            next_run_at=now,
            priority=100,
        ))
        target.status = "publishing"
        enqueued += 1

    db.session.commit()
    return {"due": len(due), "enqueued": enqueued}
