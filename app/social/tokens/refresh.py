"""Scheduled token refresh.

Refreshes access tokens that expire soon for platforms that support it
(LinkedIn 60-day, Google 1-hour-with-refresh). Accounts whose tokens don't
expire (Meta System-User) return None from refresh_token() and are skipped.
A failed refresh flips the account to needs_reauth so a human is prompted.
"""

from datetime import datetime, timedelta

from app.extensions import db
from app.models import SocialAccount
from app.social.registry import get_provider
from app.social.services.accounts import AccountManager


def refresh_expiring(within_hours=48, limit=200):
    cutoff = datetime.utcnow() + timedelta(hours=within_hours)
    accounts = (
        SocialAccount.query
        .filter(
            SocialAccount.status == "active",
            SocialAccount.token_expires_at.isnot(None),
            SocialAccount.token_expires_at <= cutoff,
        )
        .limit(limit)
        .all()
    )

    refreshed = failed = skipped = 0
    for account in accounts:
        provider = get_provider(account.platform)
        if provider is None:
            continue
        try:
            bundle = provider.refresh_token(account)
        except Exception:
            AccountManager.mark_needs_reauth(account)
            failed += 1
            continue
        if bundle is None:
            skipped += 1
            continue
        AccountManager.store_refreshed(account, bundle)
        refreshed += 1

    db.session.commit()
    return {
        "checked": len(accounts),
        "refreshed": refreshed,
        "failed": failed,
        "skipped": skipped,
    }
