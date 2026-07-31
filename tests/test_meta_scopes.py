"""The three Meta scope lists must not drift apart.

There are three, and they are not the same thing:

    META_UNIFIED_SCOPES  what the user is ASKED for at consent time
    Provider.SCOPES      what the adapter needs to do all of its work
    _required_scopes()   the subset without which a connect is refused

A scope that is in SCOPES but not in META_UNIFIED_SCOPES is never granted,
no matter how carefully it is declared or how promptly Meta approves it -
the consent screen simply never mentions it. That is precisely how the
comment and insights permissions ended up approved-but-absent, showing up
as a first comment that silently never posted and an Analytics screen that
was always empty. These tests make that failure impossible to reintroduce
quietly.
"""

import pytest

from app.social.providers.meta_common import META_UNIFIED_SCOPES
from app.social.providers.meta_facebook import MetaFacebookProvider
from app.social.providers.meta_instagram import MetaInstagramProvider

PROVIDERS = [MetaFacebookProvider, MetaInstagramProvider]


@pytest.mark.parametrize("provider_class", PROVIDERS)
def test_every_scope_the_adapter_needs_is_actually_requested(provider_class):
    missing = set(provider_class.SCOPES) - set(META_UNIFIED_SCOPES)
    assert not missing, (
        f"{provider_class.__name__}.SCOPES contains {sorted(missing)}, which "
        "the consent screen never asks for - add them to META_UNIFIED_SCOPES "
        "or the feature that needs them will fail silently."
    )


@pytest.mark.parametrize("provider_class", PROVIDERS)
def test_required_scopes_are_a_subset_of_what_is_requested(provider_class):
    """Otherwise every connect fails: we would refuse the account for
    lacking a permission we never asked the user to grant."""
    required = set(provider_class()._required_scopes())
    missing = required - set(META_UNIFIED_SCOPES)
    assert not missing, (
        f"{provider_class.__name__} requires {sorted(missing)} but does not "
        "request it - connecting would always fail."
    )


@pytest.mark.parametrize("provider_class", PROVIDERS)
def test_required_scopes_are_only_the_publishing_critical_ones(provider_class):
    """Deliberately narrow: comment and insights scopes are requested but
    NOT required, so channels keep connecting while app review is still
    pending on them."""
    required = set(provider_class()._required_scopes())
    for optional in ("pages_manage_engagement", "read_insights",
                     "instagram_manage_comments", "instagram_manage_insights"):
        assert optional not in required, (
            f"{optional} must not be required - a pending review would then "
            "block every connect."
        )


def test_the_consent_screen_asks_for_the_comment_and_insights_scopes():
    """The four that were missing, named explicitly so a future edit that
    drops one fails here with an obvious message."""
    for scope in ("pages_manage_engagement", "read_insights",
                  "instagram_manage_comments", "instagram_manage_insights"):
        assert scope in META_UNIFIED_SCOPES, f"{scope} is not requested"


#: Exactly what was submitted to Meta App Review for the Cypher Crew app,
#: on 30 July 2026. `public_profile` is not here because Facebook Login
#: grants it by default and it never reaches a scope= parameter.
#:
#: Keep this in step with the App Review dashboard BY HAND - there is no
#: API telling us what was approved, which is the whole reason it drifts.
APPROVED_BY_APP_REVIEW = {
    "pages_show_list",
    "pages_read_engagement",
    "pages_read_user_content",
    "pages_manage_posts",
    "pages_manage_engagement",
    "read_insights",
    # Required to DISCOVER Business-Manager-owned Pages (agency-managed client
    # Pages) via /me/accounts - without it they never reach connect. Must be
    # part of the App Review submission for public use; keep this set in step
    # with the dashboard by hand.
    "business_management",
    "instagram_basic",
    "instagram_content_publish",
    "instagram_manage_comments",
    "instagram_manage_insights",
}


def test_we_ask_for_exactly_what_review_approved():
    """Both directions of drift are silent, which is why this is an
    equality check and not a subset one:

      * approved but not requested -> never granted, and the feature just
        does nothing. That has already happened here twice, to the comment
        and insights scopes.
      * requested but not approved -> also never granted, and on top of
        that it is the single most common App Review rejection: asking for
        a permission the app cannot demonstrate a use for.

    business_management is REQUIRED, contrary to an earlier belief that
    "nothing calls a Business Manager endpoint - not /me/accounts". That was
    wrong: /me/accounts only returns a Page owned inside a Business Manager
    (agency-managed client Pages, the norm here) when the token carries
    business_management. Dropping it silently hid those Pages from discovery.
    It must therefore be in the App Review submission too.
    """
    requested = set(META_UNIFIED_SCOPES)

    unapproved = requested - APPROVED_BY_APP_REVIEW
    assert not unapproved, (
        f"asking for {sorted(unapproved)}, which App Review did not "
        f"approve. Either get it approved or stop requesting it - Meta "
        f"rejects submissions for permissions with no demonstrable use."
    )

    unused = APPROVED_BY_APP_REVIEW - requested
    assert not unused, (
        f"{sorted(unused)} was approved but never reaches the consent "
        f"screen, so it will never be granted and whatever needs it will "
        f"silently do nothing."
    )


def test_the_engage_inbox_can_read_other_peoples_comments():
    """pages_read_engagement covers what the PAGE posted; the comments
    Engage exists to show are written by other people, and that is a
    different permission. It is also a declared dependency of
    instagram_basic, so both halves of the connect need it."""
    assert "pages_read_user_content" in META_UNIFIED_SCOPES, (
        "without pages_read_user_content, GET /{post-id}/comments is "
        "refused and the Facebook side of Engage is locked out"
    )


def test_no_messaging_scope_is_requested():
    """There is no messaging code in this app, and asking for a permission
    that cannot be demonstrated is a review rejection."""
    for scope in META_UNIFIED_SCOPES:
        assert "manage_messages" not in scope, f"unexpected: {scope}"
        assert "messaging" not in scope, f"unexpected: {scope}"


@pytest.mark.parametrize("provider_class", PROVIDERS)
def test_connect_scopes_is_what_the_oauth_manager_will_use(provider_class):
    """OAuthManager.start() calls connect_scopes() when a provider has it -
    pin that it returns the unified list rather than the adapter's own."""
    assert provider_class().connect_scopes() == META_UNIFIED_SCOPES


# --- The grant itself, not just the request -----------------------------------
#
# Asking for the right scopes is only half of it. Meta can hand back a token
# carrying FEWER permissions than the dialog displayed, and says nothing about
# the difference. Two things made that invisible: the dialog never re-asked for
# a permission an earlier authorization had not granted, and the account row
# recorded the adapter's declared SCOPES rather than Meta's answer. The connect
# then flashed plain success while the new permissions were simply absent.


def test_consent_url_rerequests_permissions_meta_would_otherwise_skip(app):
    """Without auth_type=rerequest, Meta silently omits any permission the
    user has already declined - including every scope added to
    META_UNIFIED_SCOPES after they first connected. The dialog appears, Save
    works, and the token comes back without them."""
    from app.social.providers.meta_common import build_login_url

    with app.app_context():
        url = build_login_url(META_UNIFIED_SCOPES, "state123",
                              "https://example.test/oauth/facebook/callback")

    assert "auth_type=rerequest" in url, (
        "the consent URL must re-request declined permissions, or newly "
        "added scopes can never be granted to an existing user"
    )


@pytest.mark.parametrize("provider_class", PROVIDERS)
def test_exchange_records_what_meta_granted_not_what_we_declared(
        app, monkeypatch, provider_class):
    """The stored scopes must be Meta's answer. Recording SCOPES meant the
    row claimed permissions the user never gave, so a dropped scope could
    not be diagnosed after the fact - it looked complete."""
    from app.social.providers import meta_common

    # Meta grants the publishing-critical scopes but withholds the two that
    # are requested-but-not-required (a pending review looks exactly like
    # this), plus public_profile which we never put in a scope= parameter.
    withheld = {"read_insights", "instagram_manage_insights"}
    granted = (set(META_UNIFIED_SCOPES) - withheld) | {"public_profile"}

    monkeypatch.setattr(meta_common, "exchange_code_for_long_lived_token",
                        lambda code, redirect_uri: ("tok", None))
    monkeypatch.setattr(meta_common, "granted_permissions",
                        lambda token: granted)
    monkeypatch.setattr(meta_common, "_me_user_id", lambda token: "fbuser1")

    with app.app_context():
        bundle = provider_class().exchange_code("code", None, "https://x.test/cb")

    assert set(bundle.scopes.split(",")) == granted, (
        "bundle.scopes must be the granted set, not the declared SCOPES"
    )
    assert set(bundle.meta["ungranted_scopes"]) == withheld, (
        "a requested-but-withheld permission must be reported so the "
        "connect cannot flash unqualified success"
    )


@pytest.mark.parametrize("provider_class", PROVIDERS)
def test_full_grant_reports_nothing_ungranted(app, monkeypatch, provider_class):
    """The happy path stays quiet - no spurious warning on a clean connect."""
    from app.social.providers import meta_common

    monkeypatch.setattr(meta_common, "exchange_code_for_long_lived_token",
                        lambda code, redirect_uri: ("tok", None))
    monkeypatch.setattr(meta_common, "granted_permissions",
                        lambda token: set(META_UNIFIED_SCOPES))
    monkeypatch.setattr(meta_common, "_me_user_id", lambda token: "fbuser1")

    with app.app_context():
        bundle = provider_class().exchange_code("code", None, "https://x.test/cb")

    assert bundle.meta["ungranted_scopes"] == []
