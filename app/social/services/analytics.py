"""AnalyticsSyncService - pulls insights for published targets into
append-only SocialAnalyticsSnapshot rows. Safe to run with no providers
loaded (it simply finds nothing to sync)."""

from flask import current_app

from app.extensions import db
from app.models import SocialPostTarget, SocialAnalyticsSnapshot
from app.social.registry import get_provider
from app.social.services.accounts import AccountManager


def sync_recent(limit=100):
    targets = (
        SocialPostTarget.query
        .filter(
            SocialPostTarget.status == "published",
            SocialPostTarget.external_post_id.isnot(None),
            SocialPostTarget.social_account_id.isnot(None),
        )
        .order_by(SocialPostTarget.updated_at.desc())
        .limit(limit)
        .all()
    )

    synced = failed = 0
    errors = []

    for target in targets:
        provider = get_provider(target.platform)
        if provider is None or target.account is None:
            continue
        try:
            token = AccountManager.access_token(target.account)
            metrics = provider.fetch_analytics(target, token)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            # Was a bare `continue`, so a run where every request was
            # refused looked identical to one with nothing to fetch - and
            # the Analytics screen just stayed empty forever with no clue.
            failed += 1
            reason = str(exc)[:200]
            if reason not in errors:
                errors.append(reason)
            current_app.logger.warning(
                "analytics sync failed target=%s platform=%s: %s",
                target.id, target.platform, reason)
            continue

        if metrics:
            db.session.add(SocialAnalyticsSnapshot(
                target_id=target.id,
                external_post_id=target.external_post_id,
                metrics=metrics,
            ))
            synced += 1

    db.session.commit()
    return {"checked": len(targets), "synced": synced,
            "failed": failed, "errors": errors}
