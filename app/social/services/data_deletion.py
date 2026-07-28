"""Honouring a platform user-data deletion request.

Meta requires that when someone removes our app from their account, the
data we obtained through it goes too. "Disconnect" is not enough: that
only revokes the token and leaves the channel row, its comments and its
analytics behind.

What gets deleted for a given platform user: every channel they connected,
and everything derived from those channels - cached comments, analytics
snapshots and publish results. What is deliberately KEPT is CypherCrew's
own work records (tasks, posts as content) with the platform identifiers
detached, because those are the agency's records, not the platform's data.
That distinction is the whole design and is spelled out on the public
data-deletion page so nobody has to guess.
"""

from datetime import datetime

from app.extensions import db
from app.models import (
    DataDeletionRequest, PlatformRateBudget, PublishJob, PublishResult,
    SocialAccount, SocialAnalyticsSnapshot, SocialAuditLog, SocialComment,
    SocialPostTarget,
)


def accounts_for_platform_user(external_user_id, platform=None):
    """Channels connected by this platform user.

    Matched on the app-scoped id captured at consent time, which is the
    only identifier Meta's callback gives us.
    """
    if not external_user_id:
        return []

    query = SocialAccount.query.filter(
        SocialAccount.meta["connected_user_id"].astext == str(external_user_id)
    )
    if platform:
        query = query.filter(SocialAccount.platform == platform)
    return query.all()


def purge_accounts(accounts):
    """Delete the channels and everything derived from them.

    Returns counts, so the request record can say what was actually
    removed without holding any of the removed content.
    """
    counts = {"channels": 0, "comments": 0, "analytics": 0,
              "publish_results": 0, "targets_detached": 0}
    if not accounts:
        return counts

    account_ids = [a.id for a in accounts]
    targets = SocialPostTarget.query.filter(
        SocialPostTarget.social_account_id.in_(account_ids)
    ).all()
    target_ids = [t.id for t in targets]

    if target_ids:
        counts["comments"] = SocialComment.query.filter(
            SocialComment.target_id.in_(target_ids)
        ).delete(synchronize_session=False)

        counts["analytics"] = SocialAnalyticsSnapshot.query.filter(
            SocialAnalyticsSnapshot.target_id.in_(target_ids)
        ).delete(synchronize_session=False)

        counts["publish_results"] = PublishResult.query.filter(
            PublishResult.target_id.in_(target_ids)
        ).delete(synchronize_session=False)

        PublishJob.query.filter(
            PublishJob.target_id.in_(target_ids)
        ).delete(synchronize_session=False)

        # The task and its content stay - they are the agency's record of
        # work done. Only the platform's identifiers are cut loose, so
        # nothing here can still point at the person's account.
        for target in targets:
            target.social_account_id = None
            target.external_post_id = None
            target.permalink = None
            target.status = "removed" if target.status == "published" \
                else target.status
        counts["targets_detached"] = len(targets)

    PlatformRateBudget.query.filter(
        PlatformRateBudget.social_account_id.in_(account_ids)
    ).delete(synchronize_session=False)

    # The audit trail survives, with the account it named detached: it is
    # the evidence that the deletion happened, and it holds no content.
    SocialAuditLog.query.filter(
        SocialAuditLog.account_id.in_(account_ids)
    ).update({"account_id": None}, synchronize_session=False)

    for account in accounts:
        db.session.delete(account)
    counts["channels"] = len(accounts)

    return counts


def handle_platform_deletion(external_user_id, platform=None,
                             source=DataDeletionRequest.SOURCE_META_CALLBACK):
    """Full flow for one request: find, delete, record.

    Always produces a DataDeletionRequest - including when there was
    nothing to delete, because the person still needs a code to quote and
    a status page that tells them so.
    """
    accounts = accounts_for_platform_user(external_user_id, platform)
    counts = purge_accounts(accounts)

    record = DataDeletionRequest(
        confirmation_code=DataDeletionRequest.new_code(),
        source=source,
        platform=platform,
        external_user_id=str(external_user_id) if external_user_id else None,
        status=(DataDeletionRequest.STATUS_COMPLETED if accounts
                else DataDeletionRequest.STATUS_NOTHING_TO_DELETE),
        deleted=counts,
        completed_at=datetime.utcnow(),
    )
    db.session.add(record)
    db.session.commit()
    return record
