"""Regression tests for the full-audit fixes (bugs found by review)."""

from datetime import datetime, timedelta

from app.extensions import db
from app.models import PublishJob, PublishResult, SocialPostTarget


# --- C1: oversized images must never be re-encoded to MP4 -------------------

def test_is_video_discriminator():
    from app.social.media import fit
    assert fit.is_video("video/mp4", "reel") is True
    assert fit.is_video("image/jpeg", "image") is False
    assert fit.is_video(None, "reel") is True      # falls back to post_type
    assert fit.is_video(None, "image") is False
    assert fit.is_video("image/png", "reel") is False  # mime wins over post_type


# --- B1: a target already published must not be published again -------------

def test_already_published_target_is_not_republished(session, make_target):
    from app.social.queue import worker
    _, _, target = make_target()
    target.status = "published"
    target.external_post_id = "EXT_EXISTING"
    db.session.commit()

    job = PublishJob(
        target_id=target.id, state="queued",
        next_run_at=datetime.utcnow() - timedelta(seconds=1),
        idempotency_key=f"tgt-{target.id}-dup")
    db.session.add(job)
    db.session.commit()

    worker.drain()
    db.session.expire_all()
    job = db.session.get(PublishJob, job.id)
    t = db.session.get(SocialPostTarget, target.id)
    assert job.state == "succeeded"
    assert t.external_post_id == "EXT_EXISTING"          # unchanged
    assert PublishResult.query.filter_by(target_id=target.id).count() == 0


def test_publish_target_now_wont_double_queue(session, make_target):
    from app.social.services import publishing
    _, _, target = make_target()
    db.session.add(PublishJob(
        target_id=target.id, state="queued",
        idempotency_key=f"tgt-{target.id}-live",
        next_run_at=datetime.utcnow()))
    db.session.commit()

    publishing.publish_target_now(target)   # a live job already exists
    assert PublishJob.query.filter_by(target_id=target.id).count() == 1


# --- M-2: an async publish that never completes must dead-letter ------------

def test_pending_poll_loop_is_capped(session, make_target, monkeypatch):
    from app.social.registry import get_provider
    from app.social.dto import PublishStep
    from app.social.queue import worker

    _, _, target = make_target()
    prov = get_provider("fake")
    monkeypatch.setattr(
        prov, "poll_publish",
        lambda *a, **k: PublishStep(status="pending", provider_state={}))

    # A job already at the poll ceiling; one more PENDING poll must give up.
    job = PublishJob(
        target_id=target.id, state="queued",
        next_run_at=datetime.utcnow() - timedelta(seconds=1),
        provider_state={"started": True, "polls": 39},
        idempotency_key=f"tgt-{target.id}-poll")
    db.session.add(job)
    db.session.commit()

    worker.drain()
    db.session.expire_all()
    job = db.session.get(PublishJob, job.id)
    t = db.session.get(SocialPostTarget, target.id)
    assert job.state == "dead"
    assert t.status == "failed"


# --- B3: OAuth state must be bound to the user who started the connect -------

def test_oauth_state_is_bound_to_the_creating_user(session, make_user):
    from app.social.oauth import state as state_store
    u1 = make_user("admin")
    u2 = make_user("admin")

    row = state_store.create_state("facebook", "https://x/callback", u1.id)
    # Wrong user -> rejected (and the single-use state is consumed).
    assert state_store.consume_state(row.state, expected_by_id=u2.id) is None

    row2 = state_store.create_state("facebook", "https://x/callback", u1.id)
    got = state_store.consume_state(row2.state, expected_by_id=u1.id)
    assert got is not None and got.created_by_id == u1.id


# --- RBAC: a deactivated user's live session is cut immediately --------------

def test_deactivated_user_loses_access(make_user, login, client):
    user = make_user("admin")
    c = login(user)
    assert c.get("/notifications/api").status_code == 200

    user.status = "inactive"
    db.session.commit()

    # load_user now returns None -> anonymous -> login_required redirects.
    r = c.get("/notifications/api")
    assert r.status_code in (302, 401)


# --- B5: a partially-live post reflects onto the task, not "draft" ----------

def test_blocked_target_reads_as_partially_published(monkeypatch):
    from app.social.services import task_link

    class _T:
        def __init__(self, status):
            self.status = status
            self.scheduled_for = None
            self.permalink = None

    class _Post:
        id = 1
        published_externally = False
        targets = [_T("published"), _T("blocked")]

    monkeypatch.setattr(task_link, "linked_posts", lambda task: [_Post()])

    status, badge = task_link._derive(object())
    assert status == "Published"                  # not "Scheduled/Draft"
    assert "Partially published" in badge["label"]
