"""Ad/boosted-post comment ingestion: discovery materialises source="ad"
records (idempotent), the Engage inbox splits Post vs Ad comments, and ad posts
never leak into Studio's own lists/reports. Provider calls are monkeypatched;
nothing hits Meta.
"""
from app.models import (
    Client, SocialAccount, SocialAnalyticsSnapshot, SocialComment, SocialPost,
    SocialPostTarget,
)
from app.social.providers.simulation import SimulationProvider
from app.social.services import engage_ads
from app.social.tokens.vault import get_vault
from tests.conftest import PYTEST_EMAIL_PREFIX


def _client(session):
    c = Client(client_name=f"{PYTEST_EMAIL_PREFIX}ads", status="active")
    session.add(c)
    session.commit()
    return c


def _account(session, platform, ext, client_id, account_type="page"):
    a = SocialAccount(
        platform=platform, external_id=ext, display_name=ext,
        account_type=account_type, status="active", client_id=client_id,
        token_ciphertext=get_vault().encrypt("AT"), token_key_version=1)
    session.add(a)
    session.commit()
    return a


def _post_target(session, client_id, account, source, ext, platform="facebook"):
    post = SocialPost(title=source, status="published", source=source,
                      client_id=client_id, published_externally=(source == "ad"))
    session.add(post)
    session.flush()
    target = SocialPostTarget(
        social_post_id=post.id, social_account_id=account.id, platform=platform,
        post_type="image", external_post_id=ext, status="published")
    session.add(target)
    session.flush()
    return target


def _comment(session, target, msg):
    c = SocialComment(target_id=target.id, platform=target.platform,
                      external_id=f"c-{target.id}", message=msg,
                      is_ours=False, status="open")
    session.add(c)
    session.commit()
    return c


def test_target_live_url():
    """View-live link: stored permalink wins; a Facebook ad target builds one
    from its page_id_post_id; an Instagram ad media has none."""
    fb = SocialPostTarget(platform="facebook", external_post_id="PAGE1_99")
    assert fb.live_url == "https://www.facebook.com/PAGE1/posts/99"
    stored = SocialPostTarget(platform="facebook", external_post_id="X_1",
                              permalink="https://real/perma")
    assert stored.live_url == "https://real/perma"
    ig = SocialPostTarget(platform="instagram", external_post_id="MEDIA123")
    assert ig.live_url is None


# -- discovery / materialisation --------------------------------------------

def test_sync_ad_targets_disabled_by_default(app, session):
    with app.test_request_context():
        assert engage_ads.sync_ad_targets()["skipped"] == "disabled"


def test_sync_ad_targets_materialises_and_is_idempotent(app, session, monkeypatch):
    c = _client(session)
    page = _account(session, "facebook", "PAGE1", c.id)
    _account(session, "facebook", "act_1", c.id, account_type="ad_account")
    monkeypatch.setattr(
        SimulationProvider, "list_ad_posts",
        lambda self, aid, token, limit=100: [
            {"platform": "facebook", "external_post_id": "PAGE1_9"}],
        raising=False)

    with app.test_request_context():
        app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = True
        try:
            first = engage_ads.sync_ad_targets(c.id)
            again = engage_ads.sync_ad_targets(c.id)
        finally:
            app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = False

    assert first["discovered"] == 1
    assert again["discovered"] == 0                 # dedup by external_post_id
    t = SocialPostTarget.query.filter_by(external_post_id="PAGE1_9").first()
    assert t is not None and t.social_account_id == page.id  # resolved by prefix
    assert t.post.source == "ad" and t.post.client_id == c.id


def test_sync_ad_targets_skips_when_owner_page_missing(app, session, monkeypatch):
    c = _client(session)
    _account(session, "facebook", "act_1", c.id, account_type="ad_account")
    # No Page account with external_id "NOPAGE" -> can't resolve -> skipped.
    monkeypatch.setattr(
        SimulationProvider, "list_ad_posts",
        lambda self, aid, token, limit=100: [
            {"platform": "facebook", "external_post_id": "NOPAGE_1"}],
        raising=False)
    with app.test_request_context():
        app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = True
        try:
            assert engage_ads.sync_ad_targets(c.id)["discovered"] == 0
        finally:
            app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = False


# -- Engage tabs split the two lanes ----------------------------------------

def test_engage_tabs_split_post_and_ad(client, login, make_user, session):
    c = _client(session)
    page = _account(session, "facebook", "PAGE2", c.id)
    st = _post_target(session, c.id, page, "studio", "P_1")
    _comment(session, st, "ORGANICMARKER")
    at = _post_target(session, c.id, page, "ad", "P_2")
    _comment(session, at, "ADVERTMARKER")

    login(make_user("employee", permissions=["manage_social"]))

    r = client.get(f"/social/engage?client={c.id}")            # default = post
    assert b"ORGANICMARKER" in r.data and b"ADVERTMARKER" not in r.data

    r = client.get(f"/social/engage?client={c.id}&source=ad")   # ad lane
    assert b"ADVERTMARKER" in r.data and b"ORGANICMARKER" not in r.data


# -- ad posts never leak into Studio's own screens --------------------------

def test_ad_post_excluded_from_analytics(session):
    from app.social.services import analytics_report
    from app.utils import periods

    c = _client(session)
    page = _account(session, "facebook", "PAGE3", c.id)
    at = _post_target(session, c.id, page, "ad", "P_3")
    session.add(SocialAnalyticsSnapshot(
        target_id=at.id, external_post_id="P_3", metrics={"reach": 100}))
    session.commit()

    period = periods.resolve_period({"period": "all"}, allow_all=True,
                                    default="all")
    report = analytics_report.build_report(period, client_id=c.id)
    assert report["post_count"] == 0        # the ad target is not Studio content


def test_ad_post_excluded_from_history(session):
    from app.routes.social import _published_post_rows

    c = _client(session)
    page = _account(session, "facebook", "PAGE4", c.id)
    _post_target(session, c.id, page, "ad", "P_4")
    _post_target(session, c.id, page, "studio", "P_5")

    rows = _published_post_rows(c.id)
    sources = {row["post"].source for row in rows}
    assert "ad" not in sources and "studio" in sources


# -- ad accounts are not publishable channels (composer safety) -------------

def test_ad_accounts_hidden_from_publishable_list(session):
    from app.social.services.accounts import AccountManager

    c = _client(session)
    _account(session, "facebook", "PAGEZ", c.id)
    _account(session, "facebook", "act_Z", c.id, account_type="ad_account")

    publishable = AccountManager.list_accounts()
    assert not any(a.account_type == "ad_account" for a in publishable)
    everything = AccountManager.list_accounts(include_ad_accounts=True)
    assert any(a.account_type == "ad_account" for a in everything)


# -- OAuth discovery of ad accounts (gated + best-effort) -------------------

class _DiscoveryGraph:
    def get(self, path, token=None, params=None):
        if path == "me/accounts":
            return {"data": [{"id": "P1", "name": "Page",
                              "access_token": "pt", "tasks": []}], "paging": {}}
        if path == "me/adaccounts":
            return {"data": [{"id": "act_9", "name": "Ads"}]}
        return {"data": []}


def _fb_provider(monkeypatch):
    from app.social.providers.meta_facebook import MetaFacebookProvider
    p = MetaFacebookProvider()
    monkeypatch.setattr(p, "graph", lambda: _DiscoveryGraph())
    return p


def test_discovery_finds_ad_accounts_when_enabled(app, monkeypatch):
    p = _fb_provider(monkeypatch)
    with app.app_context():
        app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = True
        try:
            accts = p.list_publishable_accounts("usertoken")
        finally:
            app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = False
    types = {a.account_type for a in accts}
    assert "page" in types and "ad_account" in types
    ad = next(a for a in accts if a.account_type == "ad_account")
    # Stored with the USER token (carries ads_read), keyed by act_<id>.
    assert ad.external_id == "act_9" and ad.access_token == "usertoken"


def test_discovery_skips_ad_accounts_when_disabled(app, monkeypatch):
    p = _fb_provider(monkeypatch)
    with app.app_context():                    # flag off by default
        accts = p.list_publishable_accounts("usertoken")
    assert not any(a.account_type == "ad_account" for a in accts)


# -- ad token expiry surfaces a reconnect prompt ----------------------------

def test_ad_token_expiry_flags_the_account_needs_reauth(app, session, monkeypatch):
    """The ad account carries the ~60-day user token; when it expires,
    list_ad_posts fails with an auth error and the account is flagged
    needs_reauth so Channels shows a reconnect prompt (not a silent stall)."""
    from app.social.errors import AuthError

    c = _client(session)
    ad = _account(session, "facebook", "act_5", c.id, account_type="ad_account")

    def _boom(self, aid, token, limit=100):
        raise RuntimeError("token expired")

    monkeypatch.setattr(SimulationProvider, "list_ad_posts", _boom, raising=False)
    monkeypatch.setattr(SimulationProvider, "map_error",
                        lambda self, exc: AuthError("expired"), raising=False)

    with app.test_request_context():
        app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = True
        try:
            engage_ads.sync_ad_targets(c.id)
        finally:
            app.config["SOCIAL_ADS_COMMENTS_ENABLED"] = False

    assert SocialAccount.query.get(ad.id).status == "needs_reauth"
