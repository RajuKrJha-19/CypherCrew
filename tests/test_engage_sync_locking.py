"""Why "Fetch comments" died with a statement timeout after 38 seconds.

    (psycopg2.errors.QueryCanceled) canceling statement due to statement timeout
    CONTEXT: while updating tuple (57,3) in relation "social_comments"
    [SQL: UPDATE social_comments SET fetched_at=... WHERE id = 1534]

A single-row UPDATE by primary key does not take 30 seconds on its own - it was
QUEUED behind another transaction holding that row's lock. Three things
combined to make that the normal case:

  * sync_comments made a Graph call per post but committed once at the very
    end, so every row lock it took was held across all of them;
  * every comment we already had was re-stamped with fetched_at on every pass,
    so a sync took a write lock on thousands of rows it had no reason to touch;
  * nothing stopped a second Fetch (or the cron) starting while the first was
    still running, so the two fought over exactly those rows.
"""
from datetime import datetime, timedelta

from app.extensions import db
from app.models import BackgroundJob, SocialComment
from app.social import jobs
from app.social.services import engage


def _published(session, make_target, ext="P1"):
    _acct, _post, target = make_target()
    target.status = "published"
    target.external_post_id = ext
    session.commit()
    return target


# ======================================================================
# 1. No write transaction is held across a Graph call
# ======================================================================

def test_each_post_is_committed_before_the_next_is_fetched(
        session, make_target, monkeypatch):
    """The core fix. TWO posts on purpose: with one, the only Graph call
    happens before anything has been written, so the probe would read clean
    whether or not the bug was fixed. The SECOND call is the one that exposes
    a transaction still holding the first post's rows."""
    from datetime import timedelta as _td

    from app.models import SocialPostTarget

    _acct, post, first = make_target()
    first.status = "published"
    first.external_post_id = "P1"
    session.flush()
    second = SocialPostTarget(
        social_post_id=post.id, social_account_id=first.social_account_id,
        platform=first.platform, post_type="image", caption="hi",
        status="published", external_post_id="P2",
        scheduled_for=datetime.utcnow() + _td(hours=1))
    session.add(second)
    session.commit()
    from tests.conftest import FakeProvider

    # Record fetches and commits on one timeline.
    #
    # Two probes that look right and are not: "is the session dirty?" reads
    # clean because the flush inside the savepoint empties session.new while
    # the transaction - and its row locks - stay wide open; and the Session's
    # own after_commit fires for a SAVEPOINT release too, so it reports a
    # commit per post whether or not one happened. Listen at the DBAPI level
    # instead: Connection "commit" fires only for a real transaction commit.
    from sqlalchemy import event

    timeline = []

    def _list(self, post_id, token, limit=50):
        timeline.append("fetch")
        return [{"external_id": f"{post_id}-c1", "message": "hi",
                 "author_name": "Sam"}]

    def _on_commit(_conn):
        timeline.append("commit")

    engine = db.session.get_bind()
    event.listen(engine, "commit", _on_commit)
    monkeypatch.setattr(FakeProvider, "list_comments", _list, raising=False)
    monkeypatch.setattr(FakeProvider.capabilities, "supports_comments", True,
                        raising=False)
    try:
        engage.sync_comments()
    finally:
        event.remove(engine, "commit", _on_commit)

    assert timeline.count("fetch") == 2, timeline
    first, second = (i for i, e in enumerate(timeline) if e == "fetch")
    assert "commit" in timeline[first:second], (
        f"no commit between the two posts ({timeline}) - the first post's row "
        "locks were still held while the second was being fetched")


def test_comments_already_read_survive_a_failure_halfway_through(
        session, make_target, monkeypatch):
    """A per-post commit also means a post that blows up later cannot discard
    the comments already stored."""
    target = _published(session, make_target)
    from tests.conftest import FakeProvider

    calls = {"n": 0}

    def _list(self, post_id, token, limit=50):
        calls["n"] += 1
        if calls["n"] == 1:
            return [{"external_id": "kept1", "message": "hi",
                     "author_name": "Sam"}]
        raise RuntimeError("graph exploded")

    monkeypatch.setattr(FakeProvider, "list_comments", _list, raising=False)
    monkeypatch.setattr(FakeProvider.capabilities, "supports_comments", True,
                        raising=False)

    engage.sync_comments()
    engage.sync_comments()          # second pass raises inside the loop
    assert SocialComment.query.filter_by(external_id="kept1").count() == 1


# ======================================================================
# 2. A re-sync stops rewriting fetched_at on every row it has seen before
# ======================================================================

def test_a_resync_does_not_restamp_a_freshly_seen_comment(
        session, make_target, monkeypatch):
    target = _published(session, make_target)
    from tests.conftest import FakeProvider
    monkeypatch.setattr(
        FakeProvider, "list_comments",
        lambda self, *a, **k: [{"external_id": "c1", "message": "hi",
                                "author_name": "Sam"}], raising=False)
    monkeypatch.setattr(FakeProvider.capabilities, "supports_comments", True,
                        raising=False)

    engage.sync_comments()
    row = SocialComment.query.filter_by(external_id="c1").one()
    first = row.fetched_at

    engage.sync_comments()
    db.session.refresh(row)
    assert row.fetched_at == first, (
        "an unchanged comment was written again - that UPDATE is the row lock "
        "the timeout was waiting on")


def test_a_stale_comment_is_restamped(session, make_target, monkeypatch):
    """The column still means "when we last saw it", just not to the second."""
    target = _published(session, make_target)
    from tests.conftest import FakeProvider
    monkeypatch.setattr(
        FakeProvider, "list_comments",
        lambda self, *a, **k: [{"external_id": "c2", "message": "hi",
                                "author_name": "Sam"}], raising=False)
    monkeypatch.setattr(FakeProvider.capabilities, "supports_comments", True,
                        raising=False)

    engage.sync_comments()
    row = SocialComment.query.filter_by(external_id="c2").one()
    row.fetched_at = datetime.utcnow() - timedelta(days=2)
    db.session.commit()
    stale = row.fetched_at

    engage.sync_comments()
    db.session.refresh(row)
    assert row.fetched_at > stale


# ======================================================================
# 3. A second Fetch is refused while one is still running
# ======================================================================

def _running(kind, client_id=None, started_at=None):
    job = BackgroundJob(kind=kind, client_id=client_id, status="running",
                        started_at=started_at or datetime.utcnow())
    db.session.add(job)
    db.session.commit()
    return job


def test_a_running_job_blocks_a_duplicate(session):
    _running("fetch_comments")
    assert jobs.is_running("fetch_comments") is True
    assert jobs.is_running("auto_reply") is False


def test_a_global_run_also_blocks_a_per_client_trigger(session):
    _running("fetch_comments", client_id=None)
    assert jobs.is_running("fetch_comments", client_id=7) is True


def test_another_clients_run_does_not_block_this_one(session):
    """background_jobs.client_id is a real foreign key, so the RUNNING row
    needs a real client. The id we then ask about does not - it is only a
    query filter - so a neighbouring id stands in for "a different client"."""
    from app.models import Client
    from tests.conftest import PYTEST_EMAIL_PREFIX

    other = Client(client_name=f"{PYTEST_EMAIL_PREFIX}jobscope", status="active")
    session.add(other)
    session.commit()
    try:
        _running("fetch_comments", client_id=other.id)
        assert jobs.is_running("fetch_comments", client_id=other.id) is True
        assert jobs.is_running("fetch_comments", client_id=other.id + 1) is False
    finally:
        BackgroundJob.query.filter_by(client_id=other.id).delete()
        db.session.delete(other)
        db.session.commit()


def test_a_stale_running_row_never_blocks_forever(session):
    """The worker is an unsupervised daemon thread: a restart mid-job leaves
    its row "running" for good. Without a cutoff, one crash would disable the
    button permanently."""
    _running("fetch_comments",
             started_at=datetime.utcnow() - jobs.STALE_AFTER - timedelta(minutes=1))
    assert jobs.is_running("fetch_comments") is False


def test_the_route_refuses_a_second_fetch(session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    _running("fetch_comments")
    before = BackgroundJob.query.filter_by(kind="fetch_comments").count()

    resp = client.post("/social/engage/sync", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert "already running" in resp.get_data(as_text=True)
    # ...and no second job was queued.
    assert BackgroundJob.query.filter_by(kind="fetch_comments").count() == before


def test_the_route_starts_normally_when_nothing_is_running(
        session, client, make_user, login):
    login(make_user("admin", permissions=["manage_social"]))
    before = BackgroundJob.query.filter_by(kind="fetch_comments").count()
    resp = client.post("/social/engage/sync", data={}, follow_redirects=True)
    assert resp.status_code == 200
    assert BackgroundJob.query.filter_by(kind="fetch_comments").count() == before + 1


# ======================================================================
# 4. The sweep is bounded — 689 posts is what made one run take 13 minutes
# ======================================================================

def _target_aged(session, post, account, ext, days_ago):
    from app.models import SocialPostTarget
    t = SocialPostTarget(
        social_post_id=post.id, social_account_id=account.id,
        platform="fake", post_type="image", caption="c", status="published",
        external_post_id=ext,
        scheduled_for=datetime.utcnow() - timedelta(days=days_ago))
    session.add(t)
    session.commit()
    return t


def test_old_posts_are_left_out_of_the_sweep(session, app, make_target):
    """A sweep costs one Graph call per post. Unbounded, a real account had
    689 of them and the run took 792 seconds - and got slower every time a
    post was published."""
    _acct, post, first = make_target()
    first.status = "published"
    first.external_post_id = "recent"
    first.scheduled_for = datetime.utcnow() - timedelta(days=1)
    session.commit()
    _target_aged(session, post, first.account, "ancient", days_ago=400)

    with app.test_request_context():
        app.config["ENGAGE_SYNC_MAX_AGE_DAYS"] = 45
        app.config["ENGAGE_SYNC_MAX_POSTS"] = 300
        picked = {t.external_post_id for t in engage._published_targets()}
    assert "recent" in picked
    assert "ancient" not in picked


def test_the_cap_keeps_the_newest_posts(session, app, make_target):
    """If the cap bites it must keep the posts most likely to still be
    collecting comments, not an arbitrary slice."""
    _acct, post, first = make_target()
    first.status = "published"
    first.external_post_id = "d0"
    first.scheduled_for = datetime.utcnow()
    session.commit()
    for n in range(1, 5):
        _target_aged(session, post, first.account, f"d{n}", days_ago=n)

    with app.test_request_context():
        app.config["ENGAGE_SYNC_MAX_AGE_DAYS"] = 45
        app.config["ENGAGE_SYNC_MAX_POSTS"] = 2
        picked = [t.external_post_id for t in engage._published_targets()]
    assert picked == ["d0", "d1"], picked


def test_zero_restores_the_unbounded_sweep(session, app, make_target):
    """An escape hatch, so an account that really does need the whole archive
    can have it back without a code change."""
    _acct, post, first = make_target()
    first.status = "published"
    first.external_post_id = "recent"
    session.commit()
    _target_aged(session, post, first.account, "ancient", days_ago=400)

    with app.test_request_context():
        app.config["ENGAGE_SYNC_MAX_AGE_DAYS"] = 0
        app.config["ENGAGE_SYNC_MAX_POSTS"] = 0
        picked = {t.external_post_id for t in engage._published_targets()}
    assert {"recent", "ancient"} <= picked


# ======================================================================
# 5. A job the worker never came back from stops claiming to be running
# ======================================================================

def test_a_stale_running_job_is_settled(session):
    job = _running("fetch_comments",
                   started_at=datetime.utcnow() - jobs.STALE_AFTER
                   - timedelta(minutes=5))
    assert jobs.reap_stale() >= 1
    db.session.refresh(job)
    assert job.status == "failed"
    assert "Interrupted" in job.message
    assert job.finished_at is not None


def test_a_genuinely_running_job_is_left_alone(session):
    job = _running("fetch_comments")
    jobs.reap_stale()
    db.session.refresh(job)
    assert job.status == "running"


def test_the_activity_list_settles_zombies_before_showing_them(session):
    """The screen said "running 2879s" for a job that had died with the
    process. recent() reaps first, so the count above it is honest."""
    _running("fetch_comments",
             started_at=datetime.utcnow() - jobs.STALE_AFTER
             - timedelta(minutes=5))
    rows = jobs.recent()
    assert not [j for j in rows if j.status == "running"]


def test_a_long_running_ad_is_never_aged_out(session, app, make_target):
    """The window must not reach ads. An ad target has no scheduled_for, so its
    date here is when WE discovered it, and ads run for months - a cutoff would
    stop reading comments on a campaign that is still live and still earning
    replies."""
    from app.models import SocialPost

    _acct, post, first = make_target()
    first.status = "published"
    first.external_post_id = "organic_old"
    first.scheduled_for = datetime.utcnow() - timedelta(days=400)
    post.source = "studio"
    session.flush()

    ad_post = SocialPost(source="ad", status="published", title="Ad post",
                         published_externally=True)
    session.add(ad_post)
    session.flush()
    # Built the way engage_ads really builds one: NO scheduled_for, so the
    # date falls back to created_at - which is when we discovered the ad.
    from app.models import SocialPostTarget
    ad = SocialPostTarget(
        social_post_id=ad_post.id, social_account_id=first.social_account_id,
        platform="fake", post_type="image", status="published",
        external_post_id="ad_old",
        created_at=datetime.utcnow() - timedelta(days=400))
    session.add(ad)
    session.commit()
    assert ad.scheduled_for is None

    with app.test_request_context():
        app.config["ENGAGE_SYNC_MAX_AGE_DAYS"] = 45
        app.config["ENGAGE_SYNC_MAX_POSTS"] = 300
        picked = {t.external_post_id for t in engage._published_targets()}

    assert "ad_old" in picked, "a live ad stopped being polled once it aged"
    assert "organic_old" not in picked     # the window still applies to posts
    assert ad.id is not None
