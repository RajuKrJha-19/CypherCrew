"""End-to-end + unit tests for the provider-agnostic Social Publishing
Engine, driven entirely by the FakeProvider (see conftest)."""

from datetime import datetime, timedelta

from app.extensions import db
from app.models import (
    PublishJob, PublishResult, SocialAccount, SocialPostTarget,
    SocialAnalyticsSnapshot,
)
from app.social.queue import worker, retry as retry_engine, ratelimit
from app.social.services import scheduling, analytics, recovery
from app.social import status as engine_status
from app.social.tokens.vault import TokenVault
from app.social.errors import (
    TransientError, PermanentError, RateLimitError, AuthError,
)
from tests.conftest import FakeProvider


# --------------------------------------------------------------------------
# Vault
# --------------------------------------------------------------------------

def test_vault_roundtrip(app):
    from cryptography.fernet import Fernet
    v = TokenVault([Fernet.generate_key().decode()])
    ct = v.encrypt("super-secret-token")
    assert ct != "super-secret-token"
    assert v.decrypt(ct) == "super-secret-token"


def test_vault_disabled_without_key():
    assert TokenVault.from_config({"SOCIAL_TOKEN_KEY": ""}) is None
    assert TokenVault.from_config({}) is None


# --------------------------------------------------------------------------
# Retry engine (unit)
# --------------------------------------------------------------------------

def test_retry_transient_backs_off(session, make_target):
    _, _, target = make_target()
    job = PublishJob(target_id=target.id, state="claimed", attempts=0,
                     max_attempts=5, next_run_at=datetime.utcnow())
    db.session.add(job)
    db.session.flush()
    out = retry_engine.classify_and_schedule(job, TransientError("x"))
    assert out == "retry"
    assert job.attempts == 1 and job.state == "queued"
    assert job.next_run_at > datetime.utcnow()


def test_retry_transient_dead_letters_after_max(session, make_target):
    _, _, target = make_target()
    job = PublishJob(target_id=target.id, state="claimed", attempts=4,
                     max_attempts=5, next_run_at=datetime.utcnow())
    db.session.add(job)
    db.session.flush()
    out = retry_engine.classify_and_schedule(job, TransientError("x"))
    assert out == "dead" and job.state == "dead"


def test_retry_rate_limit_reschedules(session, make_target):
    _, _, target = make_target()
    job = PublishJob(target_id=target.id, state="claimed", attempts=0,
                     max_attempts=5, next_run_at=datetime.utcnow())
    db.session.add(job)
    db.session.flush()
    out = retry_engine.classify_and_schedule(job, RateLimitError("x", retry_after=60))
    # Rate limits are not hard attempts.
    assert out == "rate_limited" and job.attempts == 0 and job.state == "queued"


def test_retry_auth_fails_job(session, make_target):
    _, _, target = make_target()
    job = PublishJob(target_id=target.id, state="claimed", attempts=0,
                     max_attempts=5, next_run_at=datetime.utcnow())
    db.session.add(job)
    db.session.flush()
    out = retry_engine.classify_and_schedule(job, AuthError("bad token"))
    assert out == "auth_failed" and job.state == "failed"


def test_retry_permanent_dead_letters(session, make_target):
    _, _, target = make_target()
    job = PublishJob(target_id=target.id, state="claimed", attempts=0,
                     max_attempts=5, next_run_at=datetime.utcnow())
    db.session.add(job)
    db.session.flush()
    out = retry_engine.classify_and_schedule(job, PermanentError("nope"))
    assert out == "dead" and job.state == "dead"


# --------------------------------------------------------------------------
# Rate gate
# --------------------------------------------------------------------------

def test_ratelimit_reserve_and_over_budget(session, make_target):
    acct, _, _ = make_target()
    assert ratelimit.reserve(acct.id, limit=2, window="24h") is True
    assert ratelimit.reserve(acct.id, limit=2, window="24h") is True
    # Third exceeds the limit of 2.
    assert ratelimit.reserve(acct.id, limit=2, window="24h") is False
    # Releasing frees a slot back.
    ratelimit.release(acct.id, window="24h")
    assert ratelimit.reserve(acct.id, limit=2, window="24h") is True


# --------------------------------------------------------------------------
# Scheduler
# --------------------------------------------------------------------------

def test_scheduler_enqueues_due_and_is_idempotent(session, make_target):
    _, _, target = make_target(when_past=True)
    first = scheduling.enqueue_due()
    assert first["enqueued"] == 1
    # Running again must not double-enqueue the same target/schedule.
    second = scheduling.enqueue_due()
    assert second["enqueued"] == 0
    assert PublishJob.query.filter_by(target_id=target.id).count() == 1


def test_scheduler_skips_future(session, make_target):
    make_target(when_past=False)
    assert scheduling.enqueue_due()["enqueued"] == 0


# --------------------------------------------------------------------------
# Worker: happy path + async + failures
# --------------------------------------------------------------------------

def test_worker_publishes_successfully(session, make_target):
    _, post, target = make_target()
    scheduling.enqueue_due()
    result = worker.drain()
    assert result["claimed"] == 1
    db.session.expire_all()
    t = db.session.get(SocialPostTarget, target.id)
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert t.status == "published"
    assert t.external_post_id == "EXT_POST_1"
    assert job.state == "succeeded"
    assert PublishResult.query.filter_by(target_id=target.id).count() == 1
    assert db.session.get(type(post), post.id).status == "published"


def test_worker_pending_then_polls_to_done(session, make_target):
    _, _, target = make_target()
    FakeProvider.mode = "pending"
    scheduling.enqueue_due()
    worker.drain()  # start -> PENDING
    db.session.expire_all()
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert job.state == "queued"
    assert (job.provider_state or {}).get("started") is True
    # Make it due again and drain -> poll -> DONE.
    job.next_run_at = datetime.utcnow() - timedelta(seconds=1)
    db.session.commit()
    worker.drain()
    db.session.expire_all()
    t = db.session.get(SocialPostTarget, target.id)
    assert t.status == "published"


def test_worker_transient_retries(session, make_target):
    _, _, target = make_target()
    FakeProvider.mode = "transient"
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert job.state == "queued" and job.attempts == 1
    assert job.next_run_at > datetime.utcnow()


def test_worker_auth_marks_account_needs_reauth(session, make_target):
    acct, _, target = make_target()
    FakeProvider.mode = "auth"
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    assert db.session.get(SocialAccount, acct.id).status == "needs_reauth"
    assert db.session.get(SocialPostTarget, target.id).status == "failed"


def test_worker_permanent_dead_letters(session, make_target):
    _, _, target = make_target()
    FakeProvider.mode = "permanent"
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert job.state == "dead"
    assert db.session.get(SocialPostTarget, target.id).status == "failed"


def test_worker_no_provider_dead_letters(session, make_target):
    # A platform with no registered adapter must fail cleanly, not crash.
    _, _, target = make_target(platform="nonexistent")
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert job.state == "dead"


# --------------------------------------------------------------------------
# Failure recovery
# --------------------------------------------------------------------------

def test_recovery_requeues_dead_job(session, make_target):
    _, _, target = make_target()
    FakeProvider.mode = "permanent"
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert job.state == "dead"
    # Recover.
    assert recovery.requeue_job(job, actor_id=None, commit=True) is True
    db.session.expire_all()
    job = db.session.get(PublishJob, job.id)
    assert job.state == "queued" and job.attempts == 0 and job.last_error is None
    assert db.session.get(SocialPostTarget, target.id).status == "publishing"


# --------------------------------------------------------------------------
# Analytics + status
# --------------------------------------------------------------------------

def test_analytics_sync_snapshots_published(session, make_target):
    _, _, target = make_target()
    scheduling.enqueue_due()
    worker.drain()  # publish it
    db.session.expire_all()
    out = analytics.sync_recent()
    assert out["synced"] == 1
    snap = SocialAnalyticsSnapshot.query.filter_by(target_id=target.id).first()
    assert snap and snap.metrics["likes"] == 3


def test_engine_status_counts(session, make_target):
    make_target()
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    st = engine_status.engine_status()
    assert st["targets"]["published"] == 1
    assert st["jobs"]["succeeded"] == 1
    assert st["accounts"]["active"] == 1


# --------------------------------------------------------------------------
# Internal cron endpoint auth
# --------------------------------------------------------------------------

def test_cron_endpoint_requires_token(client):
    assert client.post("/internal/social/scheduler/run").status_code == 403
    ok = client.post("/internal/social/scheduler/run",
                     headers={"X-Social-Token": "test-worker-token"})
    assert ok.status_code == 200


# --------------------------------------------------------------------------
# Simulation mode: providers, loop-back OAuth, full workflow
# --------------------------------------------------------------------------

def test_simulation_providers_registered(app):
    from app.social.registry import registry
    for key in ("instagram", "facebook", "linkedin", "youtube"):
        p = registry.get(key)
        assert p is not None and getattr(p, "is_simulation", False)


def test_simulation_oauth_loopback_stores_encrypted_token(session):
    import urllib.parse as up
    from app.social.oauth.manager import OAuthManager
    from app.social.services.accounts import AccountManager

    url = OAuthManager.start("instagram",
                             "http://x/oauth/instagram/callback", None)
    q = dict(up.parse_qsl(up.urlparse(url).query))
    bundle, accounts = OAuthManager.finish("instagram", q["code"], q["state"])
    assert accounts and accounts[0].external_id == "sim_ig_1"

    acct = AccountManager.upsert_from_oauth("instagram", accounts[0], bundle, None)
    db.session.commit()
    # Token is encrypted at rest, and decrypts back to the real value.
    assert acct.token_ciphertext and acct.token_ciphertext != bundle.access_token
    assert AccountManager.access_token(acct) == bundle.access_token


def test_full_workflow_compose_to_published(session):
    import urllib.parse as up
    from app.models import SocialMediaAsset, SocialPost
    from app.social.oauth.manager import OAuthManager
    from app.social.services import approval, publishing, scheduling
    from app.social.services.accounts import AccountManager

    # 1) connect a simulated Instagram account (real loop-back handshake)
    url = OAuthManager.start("instagram",
                             "http://x/oauth/instagram/callback", None)
    q = dict(up.parse_qsl(up.urlparse(url).query))
    bundle, accounts = OAuthManager.finish("instagram", q["code"], q["state"])
    acct = AccountManager.upsert_from_oauth("instagram", accounts[0], bundle, None)
    db.session.commit()

    # 2) compose a draft with one platform target + media
    post = SocialPost(status="draft", title="Launch", base_caption="Hello!")
    db.session.add(post)
    db.session.flush()
    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=acct.id,
        platform="instagram", post_type="image", caption="Hello!",
        status="draft", scheduled_for=datetime.utcnow() - timedelta(minutes=1),
    )
    db.session.add(target)
    db.session.flush()
    db.session.add(SocialMediaAsset(target_id=target.id, source="upload",
                                    object_key="x.jpg", role="main"))
    db.session.commit()

    # 3) approve -> schedule -> enqueue -> publish
    approval.approve_post(post, approver_id=None)
    db.session.commit()
    result = publishing.schedule_post(post, actor_id=None)
    assert result["problems"] == {}
    scheduling.enqueue_due()
    worker.drain()

    db.session.expire_all()
    t = db.session.get(SocialPostTarget, target.id)
    assert t.status == "published"
    assert t.permalink.startswith("https://simulated.local/instagram/")
    assert db.session.get(SocialPost, post.id).status == "published"


def test_meta_error_mapping():
    """Meta error codes classify into the right typed errors so the retry
    engine acts correctly (retry / rate-defer / re-auth / dead-letter)."""
    import requests
    from app.social.providers.meta_common import map_meta_error, MetaGraphError
    from app.social.errors import (
        AuthError, RateLimitError, TransientError, PermanentError,
    )
    assert isinstance(map_meta_error(
        MetaGraphError({"code": 190, "message": "expired"}, 400)), AuthError)
    assert isinstance(map_meta_error(
        MetaGraphError({"code": 4, "message": "rate"}, 400)), RateLimitError)
    assert isinstance(map_meta_error(
        MetaGraphError({"error_subcode": 2446079, "message": "r"}, 400)),
        RateLimitError)
    assert isinstance(map_meta_error(
        MetaGraphError({"code": 1, "message": "srv"}, 500)), TransientError)
    assert isinstance(map_meta_error(
        MetaGraphError({"code": 100, "message": "bad"}, 400)), PermanentError)
    assert isinstance(map_meta_error(
        requests.exceptions.ConnectionError("down")), TransientError)


def test_meta_provider_shapes():
    """Both Meta adapters conform to the SocialProvider contract with the
    right scopes/capabilities - no network needed."""
    from app.social.providers.meta_facebook import MetaFacebookProvider
    from app.social.providers.meta_instagram import MetaInstagramProvider
    fb = MetaFacebookProvider()
    ig = MetaInstagramProvider()
    assert fb.key == "facebook" and "pages_manage_posts" in fb.SCOPES
    assert ig.key == "instagram" and ig.capabilities.requires_container_poll
    assert "carousel" in ig.capabilities.post_types
    assert ig.capabilities.publish_rate == (100, "24h")
    assert "instagram_content_publish" in ig.SCOPES


def test_simulation_caption_markers_fail_and_recover(session):
    """#simfail dead-letters; recovery requeues it."""
    from app.models import SocialMediaAsset, SocialPost
    from app.social.services import approval, publishing, scheduling, recovery

    acct = SocialAccount(
        platform="facebook", external_id="sim_fb_1", display_name="Demo Page",
        account_type="page", status="active",
    )
    from app.social.tokens.vault import get_vault
    acct.token_ciphertext = get_vault().encrypt("AT")
    db.session.add(acct)
    db.session.flush()
    post = SocialPost(status="approved", title="x", base_caption="oops #simfail")
    db.session.add(post)
    db.session.flush()
    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=acct.id, platform="facebook",
        post_type="text", caption="oops #simfail", status="draft",
        scheduled_for=datetime.utcnow() - timedelta(minutes=1),
    )
    db.session.add(target)
    db.session.commit()

    publishing.schedule_post(post, actor_id=None)
    scheduling.enqueue_due()
    worker.drain()
    db.session.expire_all()
    job = PublishJob.query.filter_by(target_id=target.id).first()
    assert job.state == "dead"

    # Recover: fix the caption and requeue.
    target = db.session.get(SocialPostTarget, target.id)
    target.caption = "fixed"
    db.session.commit()
    recovery.requeue_job(job, actor_id=None, commit=True)
    worker.drain()
    db.session.expire_all()
    assert db.session.get(SocialPostTarget, target.id).status == "published"
