"""Google review reply inbox + guarded auto-reply, through the simulator
(scripted reviews, no real API). Covers sync/draft/post/skip, the auto-safe
guardrails, the auto-reply run, and the routes + gates.
"""
from app.extensions import db
from app.models import Client, GoogleReview, SocialAccount
from app.social.reviews import service as rsvc
from app.social.tokens.vault import get_vault
from tests.conftest import PYTEST_EMAIL_PREFIX


_acct_counter = {"n": 0}


def _gbp_account(session, client_id=None):
    _acct_counter["n"] += 1
    acct = SocialAccount(
        platform="google_business", external_id=f"LOC{_acct_counter['n']}",
        display_name="Test Location", account_type="location", status="active",
        token_ciphertext=get_vault().encrypt("AT"), token_key_version=1,
        client_id=client_id)
    session.add(acct)
    session.commit()
    return acct


def _client(session, autoreply=False):
    c = Client(client_name=f"{PYTEST_EMAIL_PREFIX}gbp", status="active",
               gmb_autoreply=autoreply)
    session.add(c)
    session.commit()
    return c


# -- AI reply generation ----------------------------------------------------

def test_reply_prompt_carries_review_and_facts():
    from app.ai import prompts
    from app.ai.base import ReplyContext
    system, user = prompts.reply_prompt(ReplyContext(
        review_text="Loved it", rating=5, reviewer="Aditi",
        facts="Phone: 91234"))
    assert "Loved it" in user and "Aditi" in user
    assert "91234" in user
    assert "reply" in system.lower()


def test_sim_reply_tone_by_rating():
    from app.ai.providers.simulation import SimulationProvider
    from app.ai.base import ReplyContext
    prov = SimulationProvider()
    good = prov.generate_reply(ReplyContext(rating=5, reviewer="Sam Jones"))
    bad = prov.generate_reply(ReplyContext(rating=2, reviewer="Sam Jones"))
    assert "Thank" in good
    assert "sorry" in bad.lower()


# -- sync + human actions ---------------------------------------------------

def test_sync_is_idempotent(session):
    acct = _gbp_account(session)
    first = rsvc.sync_reviews(acct)
    assert first["new"] == 3
    second = rsvc.sync_reviews(acct)
    assert second["new"] == 0                       # no duplicates on re-sync
    assert GoogleReview.query.filter_by(account_id=acct.id).count() == 3


def test_draft_then_post_and_skip(session, app):
    acct = _gbp_account(session)
    rsvc.sync_reviews(acct)
    reviews = GoogleReview.query.filter_by(account_id=acct.id).all()
    r = reviews[0]

    with app.test_request_context():
        text = rsvc.draft_reply(r)
    assert text and r.reply_status == "drafted" and r.reply_ai_generated

    class _U:
        id = None
    with app.test_request_context():
        rsvc.post_reply(r, "Thanks so much!", _U())
    assert r.reply_status == "posted" and r.reply_text == "Thanks so much!"

    other = reviews[1]
    rsvc.skip(other, _U())
    assert other.reply_status == "skipped"


# -- auto-safe guardrails ---------------------------------------------------

_rev_counter = {"n": 0}


def _review(session, acct, rating=5, comment=""):
    _rev_counter["n"] += 1
    r = GoogleReview(account_id=acct.id, external_id=f"rev-{_rev_counter['n']}",
                     reviewer_name="X", rating=rating, comment=comment,
                     reply_status="pending")
    session.add(r)
    session.commit()
    return r


def test_is_auto_safe_guardrails(session, app):
    cl = _client(session, autoreply=True)
    acct = _gbp_account(session, client_id=cl.id)
    with app.test_request_context():
        app.config["GBP_AUTOREPLY_ENABLED"] = True
        try:
            # Safe: 5-star, short, opted-in, global on.
            assert rsvc.is_auto_safe(_review(session, acct, 5, "Great!")) is True
            # Low rating -> human.
            assert rsvc.is_auto_safe(_review(session, acct, 2, "Bad")) is False
            # Blocklist word -> human.
            assert rsvc.is_auto_safe(_review(session, acct, 5, "I want a refund")) is False
            # Long text -> human.
            assert rsvc.is_auto_safe(_review(session, acct, 5, "x" * 500)) is False
            # Global switch off -> never.
            app.config["GBP_AUTOREPLY_ENABLED"] = False
            assert rsvc.is_auto_safe(_review(session, acct, 5, "Great!")) is False
        finally:
            app.config["GBP_AUTOREPLY_ENABLED"] = False

    # Client not opted in -> never, even when global on.
    cl2 = _client(session, autoreply=False)
    acct2 = _gbp_account(session, client_id=cl2.id)
    with app.test_request_context():
        app.config["GBP_AUTOREPLY_ENABLED"] = True
        try:
            assert rsvc.is_auto_safe(_review(session, acct2, 5, "Great!")) is False
        finally:
            app.config["GBP_AUTOREPLY_ENABLED"] = False


def test_auto_reply_run_only_touches_safe_reviews(session, app):
    cl = _client(session, autoreply=True)
    acct = _gbp_account(session, client_id=cl.id)
    with app.test_request_context():
        app.config["GBP_AUTOREPLY_ENABLED"] = True
        try:
            out = rsvc.auto_reply_run(acct)     # syncs 3 sim reviews, replies safe ones
        finally:
            app.config["GBP_AUTOREPLY_ENABLED"] = False

    posted = GoogleReview.query.filter_by(account_id=acct.id, reply_status="posted").all()
    pending = GoogleReview.query.filter_by(account_id=acct.id, reply_status="pending").all()
    # The two 5-star sim reviews auto-post; the 2-star complaint stays for a human.
    assert out["auto_replied"] == 2
    assert all(p.replied_by_id is None for p in posted)    # auto = no user
    assert all(p.auto_sent for p in posted)               # recorded explicitly
    assert len(pending) == 1 and pending[0].rating == 2


def test_auto_reply_skips_when_generated_reply_hits_blocklist(session, app, monkeypatch):
    """A crafted review that steers the model into a blocklisted reply must NOT
    auto-post - the GENERATED text is scanned, not just the review (M1)."""
    cl = _client(session, autoreply=True)
    acct = _gbp_account(session, client_id=cl.id)
    # Every draft comes back with a blocklisted word, as if steered by injection.
    monkeypatch.setattr(rsvc, "_draft_text",
                        lambda review, actor_id=None: "Sure, we'll issue a full refund.")
    with app.test_request_context():
        app.config["GBP_AUTOREPLY_ENABLED"] = True
        try:
            out = rsvc.auto_reply_run(acct)
        finally:
            app.config["GBP_AUTOREPLY_ENABLED"] = False

    assert out["auto_replied"] == 0
    assert GoogleReview.query.filter_by(
        account_id=acct.id, reply_status="posted").count() == 0


# -- routes + gates ---------------------------------------------------------

def test_inbox_renders_for_social_user(session, client, login, make_user):
    _gbp_account(session)
    login(make_user("employee", permissions=["manage_social"]))
    r = client.get("/reviews/")
    assert r.status_code == 200
    assert b"Review Replies" in r.data


def test_inbox_forbidden_without_permission(client, login, make_user):
    login(make_user("employee"))
    assert client.get("/reviews/").status_code == 403


def test_sync_route_creates_reviews(session, client, login, make_user):
    _gbp_account(session)
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/reviews/sync")
    assert r.status_code in (302, 303)
    assert GoogleReview.query.count() == 3


def test_draft_route_returns_a_reply(session, client, login, make_user):
    acct = _gbp_account(session)
    rsvc.sync_reviews(acct)
    review = GoogleReview.query.first()
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post(f"/reviews/{review.id}/draft")
    assert r.status_code == 200
    assert r.get_json()["reply"]
