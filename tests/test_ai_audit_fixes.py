"""Regressions found in the AI Suite audit.

Three defects, all on paths where the model's or the platform's output is
trusted more than it should be:

  1. a finding's severity came straight from the model, so a mis-cased or
     synonym severity ranked as "info" and a check with real errors was
     recorded (and shown) as CLEAN;
  2. Engage auto-reply released its claim when the platform call returned
     without an id, so a reply that WAS posted got posted again on every
     later run - unbounded public duplicates, past the per-post cap;
  3. Google review auto-reply had no claim at all, so two overlapping cron
     runs both posted a reply to the same review.
"""
import pytest

from app.ai.base import Finding
from app.extensions import db
from app.models import AISettings, Client, GoogleReview, SocialAccount
from app.social.reviews import service as reviews_service
from app.social.services import engage
from tests.conftest import PYTEST_EMAIL_PREFIX


@pytest.fixture(autouse=True)
def _clean_ai_settings(app):
    def wipe():
        with app.app_context():
            AISettings.query.delete()
            db.session.commit()
    wipe(); yield; wipe()


# ======================================================================
# 1. Finding severity is normalised before it decides clean vs flagged
# ======================================================================

def test_finding_normalises_case():
    assert Finding(severity="Warning").severity == "warning"
    assert Finding(severity=" ERROR ").severity == "error"
    assert Finding(severity="Info").severity == "info"


def test_finding_maps_severity_synonyms():
    for raw in ("critical", "high", "severe", "major", "blocker"):
        assert Finding(severity=raw).severity == "error", raw
    for raw in ("medium", "moderate", "warn", "caution"):
        assert Finding(severity=raw).severity == "warning", raw
    for raw in ("low", "minor", "note", "notice", "suggestion"):
        assert Finding(severity=raw).severity == "info", raw


def test_unknown_severity_is_visible_not_silently_clean():
    """An unrecognised severity must never rank below "warning" - that is the
    exact path that reported a flagged creative as clean."""
    assert Finding(severity="banana").severity == "warning"
    assert Finding(severity="").severity == "info"       # absent -> advisory


def test_miscased_error_finding_flags_the_check():
    """The whole point: a model that answers "Error" instead of "error" must
    still mark the check flagged."""
    from app.ai.service import _worst
    assert _worst([Finding(severity="Error", message="wrong phone number")]) >= 1


# ======================================================================
# 2. Engage auto-reply never re-posts a reply the platform already took
# ======================================================================

def _engage_client_and_target(session, make_target, opted_in=True):
    client = Client(client_name=f"{PYTEST_EMAIL_PREFIX}audit", status="active",
                    comment_autoreply=opted_in)
    session.add(client)
    session.commit()
    _acct, post, target = make_target()
    post.client_id = client.id
    session.commit()
    return client, target


def _a_comment(session, target, msg="Love this!"):
    from app.models import SocialComment
    c = SocialComment(target_id=target.id, platform=target.platform,
                      external_id="audit-cmt-1", author_name="Sam",
                      message=msg, is_ours=False, status="open")
    session.add(c)
    session.commit()
    return c


def _engage_on(app):
    with app.app_context():
        db.session.add(AISettings(enabled=True, comment_enabled=True,
                                  comment_autoreply_enabled=True,
                                  gbp_blocklist="refund"))
        db.session.commit()


def test_engage_does_not_repost_when_platform_returns_no_id(
        session, app, make_target, monkeypatch):
    """`reply_to_comment` returns resp.get("id"); a success whose body carries
    no id used to release the claim, so the next run posted the same reply
    again - and the per-post cap never counted it."""
    from app.models import SocialComment

    client, target = _engage_client_and_target(session, make_target)
    comment = _a_comment(session, target)
    monkeypatch.setattr(engage, "sync_comments", lambda cid=None: None)

    posts = []

    def _reply_no_id(c, text, actor_id=None):
        # Models the real thing: the platform accepted the reply, the response
        # body just had no id in it.
        posts.append(c.id)
        c.replied = True
        c.status = "done"
        db.session.commit()
        return None

    monkeypatch.setattr(engage, "reply", _reply_no_id)
    _engage_on(app)

    with app.test_request_context():
        app.config["ENGAGE_AUTOREPLY_ENABLED"] = True
        try:
            engage.auto_reply_comments_run(client_id=client.id)
            engage.auto_reply_comments_run(client_id=client.id)   # next cron
            engage.auto_reply_comments_run(client_id=client.id)   # and the next
        finally:
            app.config["ENGAGE_AUTOREPLY_ENABLED"] = False

    assert len(posts) == 1, (
        f"the reply was posted {len(posts)} times - a platform response "
        "without an id must not release the claim")
    row = SocialComment.query.get(comment.id)
    assert row.replied is True and row.status == "done"
    assert row.auto_sent is True          # counts against the per-post cap


def test_engage_leaves_comment_open_when_it_cannot_post_at_all(
        session, app, make_target, monkeypatch):
    """A comment we have no way to post with (channel disconnected) must stay
    in the human queue, not be silently consumed by the claim."""
    from app.models import SocialComment

    client, target = _engage_client_and_target(session, make_target)
    comment = _a_comment(session, target)
    monkeypatch.setattr(engage, "sync_comments", lambda cid=None: None)
    # No provider for this platform -> nothing can be posted.
    monkeypatch.setattr(engage, "get_provider", lambda platform: None)
    _engage_on(app)

    with app.test_request_context():
        app.config["ENGAGE_AUTOREPLY_ENABLED"] = True
        try:
            out = engage.auto_reply_comments_run(client_id=client.id)
        finally:
            app.config["ENGAGE_AUTOREPLY_ENABLED"] = False

    assert out["auto_replied"] == 0
    row = SocialComment.query.get(comment.id)
    assert row.status == "open" and row.replied is False


# ======================================================================
# 3. Review auto-reply claims a review before posting
# ======================================================================

def _gbp_account(session, opted_in=True):
    from app.social.tokens.vault import get_vault
    client = Client(client_name=f"{PYTEST_EMAIL_PREFIX}gbp", status="active",
                    gmb_autoreply=opted_in)
    session.add(client)
    session.flush()
    acct = SocialAccount(
        platform="google_business", external_id="AUDIT-GBP",
        display_name="Audit Location", account_type="page", status="active",
        client_id=client.id,
        token_ciphertext=get_vault().encrypt("AT"), token_key_version=1)
    session.add(acct)
    session.commit()
    return client, acct


def test_review_auto_reply_is_not_posted_twice_by_overlapping_runs(
        session, app, monkeypatch):
    """Two cron runs overlapping used to both see the same `pending` review and
    both post to Google. The drafting call is slow, so the window is real."""
    _client, acct = _gbp_account(session)
    session.add(GoogleReview(
        account_id=acct.id, external_id="audit-rev-1",
        reviewer_name="Aditi", rating=5, comment="", reply_status="pending"))
    session.commit()

    monkeypatch.setattr(reviews_service, "sync_reviews",
                        lambda account: {"fetched": 0, "new": 0})
    monkeypatch.setattr(reviews_service, "_draft_text",
                        lambda review, actor_id=None: "Thank you!")

    posted = []
    reentered = {"done": False}

    class _Source:
        def post_reply(self, account, external_id, text):
            posted.append(external_id)
            # While this post is in flight, a second cron run starts.
            if not reentered["done"]:
                reentered["done"] = True
                reviews_service.auto_reply_run(acct)
            return True

    monkeypatch.setattr(reviews_service, "get_source", lambda: _Source())

    with app.app_context():
        db.session.add(AISettings(enabled=True, reply_enabled=True,
                                  gbp_autoreply_enabled=True,
                                  gbp_blocklist="refund"))
        db.session.commit()

    with app.test_request_context():
        app.config["GBP_AUTOREPLY_ENABLED"] = True
        try:
            reviews_service.auto_reply_run(acct)
        finally:
            app.config["GBP_AUTOREPLY_ENABLED"] = False

    assert posted == ["audit-rev-1"], (
        f"the review was replied to {len(posted)} times - it must be claimed "
        "before the reply is posted")


# ======================================================================
# 4. An AI-usage row with no owner cannot be resolved by a passing user
# ======================================================================

def test_ownerless_usage_row_rejects_an_actor(app):
    """Auto-reply logs its spend with no user. Ids are sequential, so any
    signed-in social user could walk them and skew the keep-rate."""
    from app.ai import usage
    from app.models import AIUsage

    with app.app_context():
        uid = usage.record(feature="comment", provider="simulation",
                           model="simulation", actor_id=None)
        try:
            assert usage.set_outcome(uid, "used", actor_id=424242) is False
            assert AIUsage.query.get(uid).outcome is None
            # The internal caller (no actor) is still allowed.
            assert usage.set_outcome(uid, "used") is True
        finally:
            AIUsage.query.filter_by(id=uid).delete()
            db.session.commit()


# ======================================================================
# 5. The bounce-back referrer cannot leave the site
# ======================================================================

def test_safe_referrer_rejects_another_origin(app):
    from app.utils.redirects import safe_referrer
    with app.test_request_context(
            "/", headers={"Referer": "https://evil.example/hook"}):
        assert safe_referrer() == "/"          # url_for('dashboard.index')


def test_safe_referrer_keeps_our_own_pages(app):
    from app.utils.redirects import safe_referrer
    with app.test_request_context("/", headers={"Referer": "/tasks/12"}):
        assert safe_referrer() == "/tasks/12"
    with app.test_request_context(
            "/", base_url="http://crew.test",
            headers={"Referer": "http://crew.test/social/queue"}):
        assert safe_referrer() == "http://crew.test/social/queue"


def test_safe_referrer_falls_back_when_absent(app):
    from app.utils.redirects import safe_referrer
    with app.test_request_context("/"):
        assert safe_referrer("social.queue") == "/social/queue"
