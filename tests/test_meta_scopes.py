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

    business_management is deliberately absent. Nothing in this codebase
    calls a Business Manager endpoint - not /me/accounts, not publishing,
    not insights - so there was no feature to demonstrate for it, and it
    was dropped from the submission rather than defended.
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
