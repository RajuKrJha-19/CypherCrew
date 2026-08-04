"""Engage guarded comment auto-reply: the config gates, the auto-safe matrix,
and the run (only safe/short/non-question comments auto-post, per-post cap,
questions + complaints stay for a human). Simulation provider, no network.
"""
import pytest

from app.ai import settings as ai_settings
from app.extensions import db
from app.models import AISettings, Client, SocialComment
from app.social.services import engage
from tests.conftest import PYTEST_EMAIL_PREFIX


@pytest.fixture(autouse=True)
def _clean_ai_settings(app):
    def wipe():
        with app.app_context():
            AISettings.query.delete()
            db.session.commit()
    wipe(); yield; wipe()


_cn = {"n": 0}


def _mk(session, target, msg, author="Sam Jones"):
    _cn["n"] += 1
    c = SocialComment(target_id=target.id, platform=target.platform,
                      external_id=f"cmt{_cn['n']}", author_name=author,
                      message=msg, is_ours=False, status="open")
    session.add(c)
    session.commit()
    return c


def _client_and_target(session, make_target, opted_in=True):
    client = Client(client_name=f"{PYTEST_EMAIL_PREFIX}engage", status="active",
                    comment_autoreply=opted_in)
    session.add(client)
    session.commit()
    acct, post, target = make_target()
    post.client_id = client.id
    session.commit()
    return client, target


def _cfg(**over):
    cfg = {"enabled": True, "max_len": 120, "max_per_post": 5,
           "blocklist": ["refund", "legal"]}
    cfg.update(over)
    return cfg


# -- config gating ----------------------------------------------------------

def test_comment_config_off_by_default(app):
    with app.test_request_context():
        assert ai_settings.comment_config()["enabled"] is False


def test_comment_config_needs_env_feature_and_admin(app):
    with app.app_context():
        db.session.add(AISettings(enabled=True, comment_enabled=True,
                                  comment_autoreply_enabled=True,
                                  gbp_blocklist="refund"))
        db.session.commit()
    with app.test_request_context():
        # Admin + feature on, but env master off -> still off.
        assert ai_settings.comment_config()["enabled"] is False
        app.config["ENGAGE_AUTOREPLY_ENABLED"] = True
        try:
            assert ai_settings.comment_config()["enabled"] is True
        finally:
            app.config["ENGAGE_AUTOREPLY_ENABLED"] = False


# -- auto-safe matrix -------------------------------------------------------

def test_comment_auto_safe_matrix(session, make_target):
    client, target = _client_and_target(session, make_target)
    cfg = _cfg()
    assert engage.comment_is_auto_safe(_mk(session, target, "Love it!"), cfg) is True
    assert engage.comment_is_auto_safe(_mk(session, target, "price?"), cfg) is False       # question
    assert engage.comment_is_auto_safe(_mk(session, target, "price kitna"), cfg) is False  # Hinglish question, no '?'
    assert engage.comment_is_auto_safe(_mk(session, target, "kaha milega"), cfg) is False  # Hinglish question
    assert engage.comment_is_auto_safe(_mk(session, target, "visit bit.ly/x"), cfg) is False  # link / spam
    assert engage.comment_is_auto_safe(_mk(session, target, "x" * 200), cfg) is False      # long
    assert engage.comment_is_auto_safe(_mk(session, target, "I want a refund"), cfg) is False  # blocklist
    assert engage.comment_is_auto_safe(_mk(session, target, "nice"),
                                       _cfg(enabled=False)) is False                        # global off

    client.comment_autoreply = False
    session.commit()
    assert engage.comment_is_auto_safe(_mk(session, target, "great"), cfg) is False         # not opted in


# -- the run ----------------------------------------------------------------

def test_auto_reply_run_only_touches_safe_comments(session, app, make_target, monkeypatch):
    client, target = _client_and_target(session, make_target)
    safe = _mk(session, target, "Love this!")
    _mk(session, target, "kitna price hai?")        # question -> human
    _mk(session, target, "y" * 200)                 # long -> human

    monkeypatch.setattr(engage, "sync_comments", lambda cid=None: None)
    posted = []

    def _stub_reply(comment, text, actor_id=None):
        comment.replied = True
        comment.status = "done"
        db.session.commit()
        posted.append(comment.id)
        return f"ext-{comment.id}"

    monkeypatch.setattr(engage, "reply", _stub_reply)

    with app.app_context():
        db.session.add(AISettings(enabled=True, comment_enabled=True,
                                  comment_autoreply_enabled=True,
                                  gbp_blocklist="refund"))
        db.session.commit()

    with app.test_request_context():
        app.config["ENGAGE_AUTOREPLY_ENABLED"] = True
        try:
            out = engage.auto_reply_comments_run(client_id=client.id)
        finally:
            app.config["ENGAGE_AUTOREPLY_ENABLED"] = False

    assert out["auto_replied"] == 1                 # only the safe one
    assert posted == [safe.id]
    assert SocialComment.query.get(safe.id).auto_sent is True


def test_answer_questions_switch_and_facts_requirement(session, make_target):
    """Questions only auto-answer when the switch is on AND the client has a
    Client Brain to ground the answer in."""
    client, target = _client_and_target(session, make_target)
    q = "price kitna hai?"
    # Default: questions go to a human.
    assert engage.comment_is_auto_safe(_mk(session, target, q), _cfg()) is False
    # Switch on + client HAS a Client Brain -> answerable.
    client.brand_brain = {"products_services": "Course A - 90000"}
    session.commit()
    assert engage.comment_is_auto_safe(
        _mk(session, target, q), _cfg(answer_questions=True)) is True
    # Switch on but NO Client Brain -> still a human (no facts to ground on).
    client.brand_brain = None
    session.commit()
    assert engage.comment_is_auto_safe(
        _mk(session, target, q), _cfg(answer_questions=True)) is False


def test_run_autoreply_now_route_invokes_scan(client, login, make_user, app, monkeypatch):
    """The 'Run auto-reply now' button POSTs, redirects immediately, and runs
    auto_reply_scan in the BACKGROUND (never inline — that would time out)."""
    import threading
    from app.routes import social as social_routes

    # Enable the global gate so the route spawns the worker (not the 'off' path).
    with app.app_context():
        db.session.add(AISettings(enabled=True, comment_enabled=True,
                                  comment_autoreply_enabled=True,
                                  gbp_blocklist="refund"))
        db.session.commit()

    done = threading.Event()
    seen = {}

    def fake_scan(client_id=None):
        seen["called"] = True
        done.set()
        return {"auto_replied": 2}

    monkeypatch.setattr(social_routes.engage_svc, "auto_reply_scan", fake_scan)
    login(make_user("employee", permissions=["manage_social"]))
    app.config["ENGAGE_AUTOREPLY_ENABLED"] = True
    try:
        r = client.post("/social/engage/auto-reply", data={"client": ""})
    finally:
        app.config["ENGAGE_AUTOREPLY_ENABLED"] = False
    assert r.status_code in (302, 303)
    assert done.wait(timeout=5)                 # the background worker ran
    assert seen.get("called") is True


def test_run_autoreply_now_off_path(client, login, make_user, app, monkeypatch):
    """When auto-reply is off, the route says so and never spawns a worker."""
    from app.routes import social as social_routes
    called = {"n": 0}
    monkeypatch.setattr(social_routes.engage_svc, "auto_reply_scan",
                        lambda client_id=None: called.__setitem__("n", called["n"] + 1))
    login(make_user("employee", permissions=["manage_social"]))
    r = client.post("/social/engage/auto-reply", data={"client": ""})
    assert r.status_code in (302, 303)
    assert called["n"] == 0                      # disabled -> no scan spawned


def test_comment_config_exposes_answer_questions(app):
    with app.app_context():
        db.session.add(AISettings(enabled=True, comment_enabled=True,
                                  comment_autoreply_enabled=True,
                                  comment_answer_questions_enabled=True,
                                  gbp_blocklist="refund"))
        db.session.commit()
    with app.test_request_context():
        app.config["ENGAGE_AUTOREPLY_ENABLED"] = True
        try:
            assert ai_settings.comment_config()["answer_questions"] is True
        finally:
            app.config["ENGAGE_AUTOREPLY_ENABLED"] = False


def test_auto_reply_skips_when_generated_reply_has_link(session, app, make_target, monkeypatch):
    """A steered/injected reply containing a link must NOT auto-post (H1)."""
    client, target = _client_and_target(session, make_target)
    _mk(session, target, "great work")              # safe input
    monkeypatch.setattr(engage, "sync_comments", lambda cid=None: None)
    from app.ai import service as ai_service
    monkeypatch.setattr(ai_service, "generate_comment_reply",
                        lambda **k: "Thanks! order now at spam.com")
    called = []
    monkeypatch.setattr(engage, "reply",
                        lambda c, t, actor_id=None: called.append(1) or "x")
    with app.app_context():
        db.session.add(AISettings(enabled=True, comment_enabled=True,
                                  comment_autoreply_enabled=True,
                                  gbp_blocklist="refund"))
        db.session.commit()
    with app.test_request_context():
        app.config["ENGAGE_AUTOREPLY_ENABLED"] = True
        try:
            out = engage.auto_reply_comments_run(client_id=client.id)
        finally:
            app.config["ENGAGE_AUTOREPLY_ENABLED"] = False
    assert out["auto_replied"] == 0 and not called   # link reply blocked, never posted
