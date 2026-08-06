"""Engage spam moderation: the spam test, the auto-mod config gates, the
guarded auto-HIDE run (reversible, per-client opt-in, reverts on a failed
platform call), and the manual hide / delete / restore actions. Simulation
provider (instagram target -> SimulationProvider), no network.
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


def _mk(session, target, msg, ext=None, is_ours=False, status="open"):
    _cn["n"] += 1
    c = SocialComment(target_id=target.id, platform=target.platform,
                      external_id=ext or f"mc{_cn['n']}", author_name="Sam",
                      message=msg, is_ours=is_ours, status=status)
    session.add(c)
    session.commit()
    return c


def _client_and_target(session, make_target, opted_in=True):
    client = Client(client_name=f"{PYTEST_EMAIL_PREFIX}mod", status="active",
                    comment_automod=opted_in)
    session.add(client)
    session.commit()
    # instagram -> SimulationProvider (has set_comment_hidden/delete_comment).
    acct, post, target = make_target(platform="instagram")
    post.client_id = client.id
    session.commit()
    return client, target


def _cfg(**over):
    cfg = {"enabled": True, "blocklist": ["followers", "promo"],
           "hide_links": True, "max_per_run": 20}
    cfg.update(over)
    return cfg


def _enable(app):
    with app.app_context():
        db.session.add(AISettings(comment_automod_enabled=True,
                                  spam_blocklist="followers"))
        db.session.commit()


# -- is_spam ----------------------------------------------------------------

def test_is_spam_matrix(session, make_target):
    _, target = _client_and_target(session, make_target)
    cfg = _cfg()
    assert engage.is_spam(_mk(session, target, "buy followers now"), cfg) == "blocklist: followers"
    assert engage.is_spam(_mk(session, target, "visit bit.ly/x"), cfg) == "link spam"
    assert engage.is_spam(_mk(session, target, "Love this post"), cfg) is None
    assert engage.is_spam(_mk(session, target, "check site.com", is_ours=True), cfg) is None
    assert engage.is_spam(_mk(session, target, "spam link.com", status="done"), cfg) is None
    assert engage.is_spam(_mk(session, target, "visit bit.ly/x"), _cfg(hide_links=False)) is None


def test_automod_off_when_ai_master_switch_off(app):
    """Spam auto-hide is an unattended public action — the AI master switch must
    kill it too, not just the per-feature toggles."""
    with app.app_context():
        db.session.add(AISettings(enabled=False, comment_automod_enabled=True,
                                  spam_blocklist="followers"))
        db.session.commit()
    with app.test_request_context():
        app.config["ENGAGE_AUTOMOD_ENABLED"] = True
        try:
            assert ai_settings.automod_config()["enabled"] is False   # master off
        finally:
            app.config["ENGAGE_AUTOMOD_ENABLED"] = False


def test_ad_comment_is_exempt_from_the_link_heuristic(session, make_target):
    """On an ad/boosted post a link alone must NOT auto-hide (it would bury a
    lead), but the explicit blocklist still applies."""
    client, target = _client_and_target(session, make_target)
    target.post.source = "ad"
    session.commit()
    cfg = _cfg()
    assert engage.is_spam(_mk(session, target, "interested! wa.me/123"), cfg) is None
    assert engage.is_spam(_mk(session, target, "buy followers now"), cfg) == "blocklist: followers"


def test_is_spam_matches_abuse_wordlist_on_every_lane(session, make_target):
    """Abuse/profanity is auto-hide-worthy on EVERY post, ads included (unlike
    the link heuristic, which is ad-exempt) — abuse is never a lead."""
    _acct, _post, target = make_target()
    cfg = _cfg(abuse_blocklist=["moron", "slur1"])
    assert engage.is_spam(_mk(session, target, "you moron"), cfg) == "abuse: moron"
    assert engage.is_spam(_mk(session, target, "lovely work"), cfg) is None
    # Ad lane: still caught (abuse is not exempt the way links are).
    target.post.source = "ad"
    session.commit()
    assert engage.is_spam(
        _mk(session, target, "total slur1 here"), cfg) == "abuse: slur1"


def test_blocklist_matches_whole_words_not_substrings(session, make_target):
    """A term matches a WHOLE word only — 'ass' must not hide 'assessment' and
    'hell' must not hide 'hello', or the auto-hide list becomes untrustworthy."""
    _acct, _post, target = make_target()
    cfg = _cfg(blocklist=["ass"], abuse_blocklist=["hell"])
    # innocent words that merely CONTAIN the term -> not flagged
    assert engage.is_spam(_mk(session, target, "great assessment!"), cfg) is None
    assert engage.is_spam(_mk(session, target, "hello there team"), cfg) is None
    # the term as a whole word -> flagged
    assert engage.is_spam(_mk(session, target, "what an ass"), cfg) == "blocklist: ass"
    assert engage.is_spam(_mk(session, target, "go to hell"), cfg) == "abuse: hell"


# -- automod_config gating --------------------------------------------------

def test_automod_config_off_by_default(app):
    with app.test_request_context():
        assert ai_settings.automod_config()["enabled"] is False


def test_automod_config_needs_env_admin_and_blocklist(app):
    with app.app_context():
        db.session.add(AISettings(comment_automod_enabled=True,
                                  spam_blocklist="followers"))
        db.session.commit()
    with app.test_request_context():
        assert ai_settings.automod_config()["enabled"] is False      # env off
        app.config["ENGAGE_AUTOMOD_ENABLED"] = True
        try:
            assert ai_settings.automod_config()["enabled"] is True
        finally:
            app.config["ENGAGE_AUTOMOD_ENABLED"] = False


def test_automod_config_blocklist_is_mandatory(app):
    with app.app_context():
        db.session.add(AISettings(comment_automod_enabled=True,
                                  spam_blocklist=None))
        db.session.commit()
    with app.test_request_context():
        app.config["ENGAGE_AUTOMOD_ENABLED"] = True
        try:
            assert ai_settings.automod_config()["enabled"] is False   # no words
        finally:
            app.config["ENGAGE_AUTOMOD_ENABLED"] = False


def test_automod_config_abuse_list_alone_arms_it(app):
    """Either list is enough: an abuse list with NO spam list still enables
    auto-moderation (blocklist-mandatory holds — one of the two)."""
    with app.app_context():
        db.session.add(AISettings(comment_automod_enabled=True,
                                  spam_blocklist=None, abuse_blocklist="slur1"))
        db.session.commit()
    with app.test_request_context():
        app.config["ENGAGE_AUTOMOD_ENABLED"] = True
        try:
            cfg = ai_settings.automod_config()
            assert cfg["enabled"] is True
            assert cfg["abuse_blocklist"] == ["slur1"]
        finally:
            app.config["ENGAGE_AUTOMOD_ENABLED"] = False


# -- automod_scan (guarded auto-hide) ---------------------------------------

def _run_scan(app, client_id):
    with app.test_request_context():
        app.config["ENGAGE_AUTOMOD_ENABLED"] = True
        try:
            return engage.automod_scan(client_id=client_id)
        finally:
            app.config["ENGAGE_AUTOMOD_ENABLED"] = False


def test_automod_scan_hides_only_spam(session, app, make_target):
    client, target = _client_and_target(session, make_target)
    spam = _mk(session, target, "buy followers cheap")
    clean = _mk(session, target, "great work")
    link = _mk(session, target, "visit bit.ly/x")
    _enable(app)

    out = _run_scan(app, client.id)

    assert out["hidden"] == 2
    s = SocialComment.query.get(spam.id)
    assert s.status == "removed" and s.removal_kind == "auto"
    assert s.removal_action == "hidden" and s.removed_by_id is None
    assert s.removal_reason == "blocklist: followers"
    assert SocialComment.query.get(link.id).status == "removed"
    assert SocialComment.query.get(clean.id).status == "open"     # untouched


def test_automod_scan_respects_client_optout(session, app, make_target):
    client, target = _client_and_target(session, make_target, opted_in=False)
    _mk(session, target, "buy followers")
    _enable(app)
    assert _run_scan(app, client.id)["hidden"] == 0


def test_automod_scan_reverts_on_platform_failure(session, app, make_target):
    client, target = _client_and_target(session, make_target)
    # An external id carrying #simfail makes the sim provider's hide raise, so
    # the claim must be reverted and the comment left visible for a retry.
    c = _mk(session, target, "buy followers", ext="mc#simfail")
    _enable(app)
    assert _run_scan(app, client.id)["hidden"] == 0
    assert SocialComment.query.get(c.id).status == "open"


def test_automod_scan_inert_when_disabled(session, app, make_target):
    client, target = _client_and_target(session, make_target)
    _mk(session, target, "buy followers")
    with app.test_request_context():           # no AISettings row + env off
        out = engage.automod_scan(client_id=client.id)
    assert out.get("skipped") == "disabled"


# -- manual hide / delete / restore -----------------------------------------

def test_manual_hide_then_restore(session, make_target, make_user):
    # Manual actions don't need a client (they use the target's page token), so
    # skip the prefixed Client to avoid the make_user teardown FK ordering.
    _, _, target = make_target(platform="instagram")
    user = make_user("employee", permissions=["manage_social"])
    c = _mk(session, target, "whatever")

    assert engage.hide(c, actor_id=user.id) == (True, None)
    c = SocialComment.query.get(c.id)
    assert c.status == "removed" and c.removal_kind == "manual"
    assert c.removal_action == "hidden" and c.removed_by_id == user.id

    assert engage.restore(c, actor_id=user.id) == (True, None)
    c = SocialComment.query.get(c.id)
    assert c.status == "open" and c.removal_action is None and c.removed_by_id is None


def test_manual_delete_is_permanent(session, make_target, make_user):
    _, _, target = make_target(platform="instagram")
    user = make_user("employee", permissions=["manage_social"])
    c = _mk(session, target, "whatever")

    assert engage.delete(c, actor_id=user.id) == (True, None)
    c = SocialComment.query.get(c.id)
    assert c.status == "removed" and c.removal_action == "deleted"
    ok, reason = engage.restore(c, actor_id=user.id)          # can't restore a delete
    assert ok is False and "can't be restored" in reason


def test_delete_survives_a_failure_after_the_platform_call(session, make_target, make_user, monkeypatch):
    """A failure in the audit/commit step (after the platform delete) is caught
    and returned as (False, reason) — never a 500."""
    from app.social.services import audit as audit_mod
    _, _, target = make_target(platform="instagram")
    user = make_user("employee", permissions=["manage_social"])
    c = _mk(session, target, "whatever")

    def _boom(*a, **k):
        raise RuntimeError("audit boom")

    monkeypatch.setattr(audit_mod, "record", _boom)
    ok, reason = engage.delete(c, actor_id=user.id)
    assert ok is False and reason          # graceful, no exception propagated
