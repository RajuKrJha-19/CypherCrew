"""A publish interrupted by a worker death must never silently republish.

Covers the dispatch-marker fix: start_publish records a `dispatched` marker
(committed, job still 'claimed') before the side-effecting call, so a worker
that dies mid-upload is resumed as an interrupted-publish for manual
verification instead of re-uploading and duplicating the post - while a normal
error still clears the marker and retries cleanly.
"""

from datetime import datetime, timedelta

from app.extensions import db
from app.models import PublishJob, PublishResult, SocialPostTarget
from app.social.queue import worker
from app.social.services import scheduling, recovery
from tests.conftest import FakeProvider


def test_interrupted_publish_is_not_republished(session, make_target):
    """A job left 'claimed' + stale with a dispatch marker but no confirmed
    result (a worker died mid-publish) is flagged, not re-sent."""
    _, _, target = make_target()
    job = PublishJob(
        target_id=target.id, state="claimed",
        locked_at=datetime.utcnow() - timedelta(minutes=20),   # stale
        next_run_at=datetime.utcnow() - timedelta(seconds=1),
        provider_state={"dispatched": True},                   # no "started"
        idempotency_key=f"tgt-{target.id}-interrupt")
    db.session.add(job)
    db.session.commit()

    worker.drain()   # _reset_stale requeues it, then _process resumes it
    db.session.expire_all()

    t = db.session.get(SocialPostTarget, target.id)
    job = db.session.get(PublishJob, job.id)
    assert job.state == "dead"
    assert t.status == "failed"
    assert "interrupt" in (t.last_error or "").lower()
    # It must NOT have been published a (second) time.
    assert t.external_post_id is None
    assert PublishResult.query.filter_by(target_id=target.id).count() == 0


def test_normal_error_clears_dispatch_marker_and_retries_cleanly(
        session, make_target):
    """A normal start_publish error must not be mistaken for an interruption:
    the marker is cleared and the job retries as a fresh first attempt."""
    _, _, target = make_target()
    FakeProvider.mode = "transient"          # start_publish raises
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()

    job = PublishJob.query.filter_by(target_id=target.id).first()
    # Transient failure -> rescheduled for retry, NOT dead-interrupted.
    assert job.state == "queued"
    ps = job.provider_state or {}
    assert not ps.get("dispatched")          # cleared, so no false interrupt
    assert not ps.get("started")             # a clean first attempt next time


def test_requeue_clears_dispatch_so_interrupted_can_be_retried(
        session, make_target):
    """Manually retrying an interrupted job runs a clean attempt instead of
    tripping the interrupted guard forever."""
    _, _, target = make_target()
    job = PublishJob(
        target_id=target.id, state="dead",
        provider_state={"dispatched": True},
        idempotency_key=f"tgt-{target.id}-req")
    db.session.add(job)
    db.session.commit()

    assert recovery.requeue_job(job, commit=True) is True
    db.session.expire_all()
    job = db.session.get(PublishJob, job.id)
    assert not (job.provider_state or {}).get("dispatched")

    # And a drain now publishes cleanly (FakeProvider default mode = ok).
    job.next_run_at = datetime.utcnow() - timedelta(seconds=1)
    db.session.commit()
    worker.drain()
    db.session.expire_all()
    t = db.session.get(SocialPostTarget, target.id)
    assert t.status == "published"


def test_successful_publish_leaves_no_dispatch_marker(session, make_target):
    """The happy path clears the dispatch marker (started, not dispatched)."""
    _, _, target = make_target()
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert job.state == "succeeded"
    assert not (job.provider_state or {}).get("dispatched")
