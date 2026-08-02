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
