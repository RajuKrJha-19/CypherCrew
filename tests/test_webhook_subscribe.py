"""Auto-subscribing connected Facebook Pages to the app's webhooks
(subscribed_apps): gated on META_WEBHOOK_ENABLED, only touches active FB Pages,
idempotent backfill, and a provider failure is swallowed (never breaks connect).
Simulation provider — nothing hits Meta.
"""
from app.models import SocialAccount
from app.social.services import webhook_subscribe
from app.social.tokens.vault import get_vault
from tests.conftest import PYTEST_EMAIL_PREFIX


def _account(session, platform, ext, account_type="page"):
    a = SocialAccount(
        platform=platform, external_id=ext, display_name=ext,
        account_type=account_type, status="active",
        token_ciphertext=get_vault().encrypt("AT"), token_key_version=1)
    session.add(a)
    session.commit()
    return a


def _on(app):
    app.config["META_WEBHOOK_ENABLED"] = True


def _off(app):
    app.config["META_WEBHOOK_ENABLED"] = False


def test_subscribe_account_subscribes_active_fb_page(app, session):
    page = _account(session, "facebook", "PAGE1")
    with app.test_request_context():
        _on(app)
        try:
            assert webhook_subscribe.subscribe_account(page) is True
        finally:
            _off(app)


def test_subscribe_account_noop_when_disabled(app, session):
    page = _account(session, "facebook", "PAGE2")
    with app.test_request_context():           # flag off by default
        assert webhook_subscribe.subscribe_account(page) is False


def test_subscribe_account_skips_non_fb_and_non_page(app, session):
    ig = _account(session, "instagram", "IG1", account_type="ig_business")
    ad = _account(session, "facebook", "act_1", account_type="ad_account")
    with app.test_request_context():
        _on(app)
        try:
            assert webhook_subscribe.subscribe_account(ig) is False
            assert webhook_subscribe.subscribe_account(ad) is False
        finally:
            _off(app)


def test_subscribe_account_swallows_provider_failure(app, session):
    # The simulation provider raises on a page id carrying #simfail.
    page = _account(session, "facebook", "PAGE#simfail")
    with app.test_request_context():
        _on(app)
        try:
            assert webhook_subscribe.subscribe_account(page) is False   # no raise
        finally:
            _off(app)


def test_subscribe_all_pages_counts_only_active_fb_pages(app, session):
    _account(session, "facebook", "PAGEA")
    _account(session, "facebook", "PAGEB")
    _account(session, "instagram", "IGX", account_type="ig_business")
    _account(session, "facebook", "act_9", account_type="ad_account")
    with app.test_request_context():
        _on(app)
        try:
            out = webhook_subscribe.subscribe_all_pages()
        finally:
            _off(app)
    assert out["subscribed"] == 2 and out["pages"] == 2


def test_subscribe_all_pages_disabled(app, session):
    _account(session, "facebook", "PAGEC")
    with app.test_request_context():
        assert webhook_subscribe.subscribe_all_pages()["skipped"] == "disabled"
