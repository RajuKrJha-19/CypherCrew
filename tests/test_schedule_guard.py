"""A post with no publish time must never silently publish immediately.

Regression guard for the "scheduler publishes posts immediately" bug: when
someone chose "Schedule for later" but left the time blank, scheduled_for was
None and fell through to datetime.utcnow(), so the post went out at once. The
schedule route (which knows the chosen publish mode) now refuses that, and the
scheduling engine only enqueues a genuinely-due (past) target.
"""

from datetime import datetime, timedelta

from app.extensions import db
from app.models import (
    PublishJob, SocialAccount, SocialMediaAsset, SocialPost, SocialPostTarget,
)
from app.social.services import publishing, scheduling


# --- Engine: due vs future ---------------------------------------------------

def test_future_schedule_is_not_enqueued_immediately(make_target):
    _acct, post, target = make_target(when_past=False)  # scheduled ~1h out
    publishing.schedule_post(post)
    assert target.status == "scheduled"

    res = scheduling.enqueue_due()
    assert PublishJob.query.filter_by(target_id=target.id).count() == 0
    assert res["enqueued"] == 0


def test_due_schedule_is_enqueued(make_target):
    _acct, post, target = make_target(when_past=True)  # scheduled in the past
    publishing.schedule_post(post)
    assert target.status == "scheduled"

    scheduling.enqueue_due()
    assert PublishJob.query.filter_by(target_id=target.id).count() == 1


# --- Route guard: blank time is refused, not published ----------------------

def _approved_post_with_blank_time(session):
    acct = SocialAccount(
        platform="facebook", external_id="SGX1", display_name="SG Page",
        account_type="page", status="active")
    post = SocialPost(status="approved", title="sg")
    db.session.add_all([acct, post])
    db.session.flush()
    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=acct.id,
        platform="facebook", post_type="image", status="draft",
        scheduled_for=None)
    db.session.add(target)
    db.session.commit()
    return post, target


def test_blank_schedule_is_refused_not_published(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    post, target = _approved_post_with_blank_time(session)

    # "Schedule for later" with no time in the form.
    r = client.post(f"/social/posts/{post.id}/schedule",
                    data={"publish_mode": "schedule"}, follow_redirects=False)
    assert r.status_code in (302, 303)

    db.session.refresh(post)
    db.session.refresh(target)
    # Not published: post stays approved, nothing enqueued.
    assert post.status == "approved"
    assert PublishJob.query.filter_by(target_id=target.id).count() == 0


def test_publish_now_still_works(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    post, target = _approved_post_with_blank_time(session)

    r = client.post(f"/social/posts/{post.id}/schedule",
                    data={"publish_mode": "now"}, follow_redirects=False)
    assert r.status_code in (302, 303)

    db.session.refresh(target)
    # Publish-now sets a time and schedules it.
    assert target.scheduled_for is not None
    assert target.status in ("scheduled", "publishing", "blocked")


# --- Per-channel times must survive a confirm, and past/mixed refused -------

def _two_channel_post(t1, t2):
    """An approved post on two 'fake' channels (which validate cleanly), each
    with its own scheduled_for."""
    a1 = SocialAccount(platform="fake", external_id="SG2A", display_name="A",
                       account_type="page", status="active")
    a2 = SocialAccount(platform="fake", external_id="SG2B", display_name="B",
                       account_type="page", status="active")
    post = SocialPost(status="approved", title="sg2")
    db.session.add_all([a1, a2, post])
    db.session.flush()
    targets = []
    for acct, when in ((a1, t1), (a2, t2)):
        tg = SocialPostTarget(
            social_post_id=post.id, social_account_id=acct.id,
            platform="fake", post_type="image", status="draft",
            scheduled_for=when)
        db.session.add(tg)
        db.session.flush()
        db.session.add(SocialMediaAsset(
            target_id=tg.id, source="upload", object_key="x.jpg", role="main"))
        targets.append(tg)
    db.session.commit()
    return post, targets[0], targets[1]


def test_per_channel_times_preserved_on_blank_confirm(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    t1 = datetime.utcnow() + timedelta(hours=2)
    t2 = datetime.utcnow() + timedelta(hours=5)
    post, tg1, tg2 = _two_channel_post(t1, t2)

    # "Schedule" with a BLANK field (the template blanks it for per-channel).
    client.post(f"/social/posts/{post.id}/schedule",
                data={"publish_mode": "schedule"}, follow_redirects=False)

    db.session.refresh(tg1)
    db.session.refresh(tg2)
    assert tg1.status == "scheduled" and tg2.status == "scheduled"
    # Distinct per-channel times are NOT collapsed to the first channel's.
    assert abs((tg1.scheduled_for - t1).total_seconds()) < 2
    assert abs((tg2.scheduled_for - t2).total_seconds()) < 2


def test_mixed_none_time_is_refused(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    t1 = datetime.utcnow() + timedelta(hours=2)
    post, tg1, tg2 = _two_channel_post(t1, None)   # one channel blank

    client.post(f"/social/posts/{post.id}/schedule",
                data={"publish_mode": "schedule"}, follow_redirects=False)

    db.session.refresh(post)
    assert post.status == "approved"               # refused, not published
    assert PublishJob.query.filter_by(target_id=tg1.id).count() == 0
    assert PublishJob.query.filter_by(target_id=tg2.id).count() == 0


def test_past_schedule_is_refused(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    past = datetime.utcnow() - timedelta(hours=1)
    post, tg1, tg2 = _two_channel_post(past, past)

    client.post(f"/social/posts/{post.id}/schedule",
                data={"publish_mode": "schedule"}, follow_redirects=False)

    db.session.refresh(post)
    assert post.status == "approved"               # refused
    assert PublishJob.query.filter_by(target_id=tg1.id).count() == 0
