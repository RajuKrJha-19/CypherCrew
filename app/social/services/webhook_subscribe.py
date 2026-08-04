"""Subscribe connected Facebook Pages to the app's webhooks (subscribed_apps).

App-level field subscriptions (feed / comments) don't deliver a SPECIFIC page's
events until that page is individually subscribed to the app. This wires that
up automatically on channel connect, plus a backfill sweep for pages connected
before webhooks were switched on — so real-time Engage works without anyone
hand-running Graph calls. Dormant unless META_WEBHOOK_ENABLED.

Instagram comment webhooks ride the linked Facebook Page's subscription, so
subscribing the Page is all that's needed for both.
"""
from flask import current_app

from app.models import SocialAccount
from app.social.registry import get_provider
from app.social.services.accounts import AccountManager


def _enabled():
    return bool(current_app.config.get("META_WEBHOOK_ENABLED"))


def subscribe_account(account):
    """Subscribe one connected Facebook Page to the app's webhooks. A no-op for
    anything that isn't an active Facebook Page. Best-effort — a failure is
    logged, never raised (a page that won't subscribe must not break connect)."""
    if not _enabled():
        return False
    if (account is None or account.platform != "facebook"
            or account.account_type != "page" or account.status != "active"):
        return False
    provider = get_provider("facebook")
    if provider is None or not hasattr(provider, "subscribe_app_to_page"):
        return False
    try:
        token = AccountManager.access_token(account)
        return bool(provider.subscribe_app_to_page(account.external_id, token))
    except Exception:  # noqa: BLE001 - see docstring
        current_app.logger.exception(
            "[webhooks] page subscribe failed for %s", account.external_id)
        return False


def subscribe_all_pages():
    """Backfill / refresh: subscribe every active Facebook Page. Idempotent
    (Meta upserts each subscription), so it's safe to run on every connect and
    from a cron. Returns counts."""
    if not _enabled():
        return {"subscribed": 0, "skipped": "disabled"}
    subscribed = 0
    pages = SocialAccount.query.filter_by(
        platform="facebook", account_type="page", status="active").all()
    for account in pages:
        if subscribe_account(account):
            subscribed += 1
    return {"subscribed": subscribed, "pages": len(pages)}
