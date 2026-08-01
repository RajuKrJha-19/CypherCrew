"""Post lifecycle after publishing: remove a live post from the platform, and
detect posts that were deleted directly on the platform.

Design notes for our use case:
- Facebook Page posts can be deleted via the Graph API. Instagram media
  CANNOT (API limitation) - there we mark it removed in the Studio and tell the
  user to delete it on Instagram.
- 'removed' is a target/post status (a plain string; no migration) meaning the
  post is no longer live. The Studio KEEPS the record for history rather than
  hard-deleting it.
"""

from datetime import datetime

from app.extensions import db
from app.models import SocialPost, SocialPostTarget
from app.social.services import audit
from app.social.services.accounts import AccountManager
from app.social.errors import SocialError


#: Target statuses that will not change again on their own. Anything else
#: means the queue is still working on it.
TERMINAL_TARGET_STATUSES = ("published", "removed", "failed", "blocked")

#: Terminal statuses meaning the target never went live. "blocked" belongs
#: here with "failed": a target that can never publish settles the post just
#: like a failed one, otherwise the rollup waits forever on something that is
#: never going to move.
STUCK_TARGET_STATUSES = ("failed", "blocked")


def post_status_from(statuses):
    """The post status implied by its targets', or None while any is in flight.

    One function because there were two rollups - this module's, reached by
    removing a target, and the worker's, reached by publishing one - and each
    enumerated the combinations it happened to think of. Between them they
    missed the same one:

        ["removed", "failed"]

    is not all-published, not all-removed, has nothing published, and is not
    all-stuck because "removed" was left out of that set. It matched no branch
    in either function, so the post kept whatever status it had - typically
    `partially_published` - forever. And `delete_post` refuses exactly those
    statuses, so the post could not be settled OR deleted, and it pinned its
    task in the Published column with a retry badge for a target that had
    already been taken down. Remove the live half of a partially-published
    post and that is what you got.

    Written as "what is true of these statuses" rather than a branch per
    combination, so a status added later cannot silently fall through.
    """
    if not statuses or not all(s in TERMINAL_TARGET_STATUSES
                               for s in statuses):
        return None

    if all(s == "published" for s in statuses):
        return "published"

    if all(s == "removed" for s in statuses):
        return "removed"

    if any(s == "published" for s in statuses):
        # Something is live. Say "partially" only when something else never
        # made it - a target that published and was later taken down is not a
        # partial publish, it is a publish plus a removal.
        return ("partially_published"
                if any(s in STUCK_TARGET_STATUSES for s in statuses)
                else "published")

    # Nothing is live, and it is not the clean all-removed case: some target
    # never published at all.
    return "failed"


def _rollup_removed(post):
    """Reflect target removals on the parent post's status."""
    if post is None:
        return
    settled = post_status_from([t.status for t in post.targets])
    if settled is not None:
        post.status = settled


def remove_target(target, actor_id=None):
    """Delete one published target on the platform (where supported) and mark
    it removed. Returns a human note when the platform can't delete via API."""
    from app.social.registry import get_provider
    provider = get_provider(target.platform)
    caps = provider.capabilities if provider else None
    can_api_delete = bool(caps and getattr(caps, "supports_delete", False)) \
        and target.external_post_id and target.account is not None

    note = None
    if can_api_delete:
        try:
            token = AccountManager.access_token(target.account)
            provider.delete_post(target.external_post_id, token)
        except Exception as exc:  # noqa: BLE001
            # Providers raise raw transport errors (MetaGraphError /
            # GoogleHTTPError) that don't subclass SocialError, so catching only
            # SocialError let a token-expired / already-gone / 5xx delete 500 the
            # route and leave the target stuck "published". Surface it as a note.
            note = f"Platform couldn't delete it: {exc}"
    else:
        note = (f"{target.platform.capitalize()} can't delete posts via the "
                "API - remove it directly on the platform. Marked as removed "
                "here.")

    target.status = "removed"
    target.updated_at = datetime.utcnow()
    audit.record("post_removed", target_id=target.id,
                 post_id=target.social_post_id, actor_id=actor_id,
                 task_id=(target.post.task_id if target.post else None),
                 detail={"note": note})
    return note


def sync_published(client_id=None, limit=200):
    """Detect targets whose post was deleted directly on the platform and mark
    them removed, so the Studio's Published list reflects reality."""
    from app.social.registry import get_provider
    q = SocialPostTarget.query.filter(
        SocialPostTarget.status == "published",
        SocialPostTarget.external_post_id.isnot(None))
    if client_id:
        q = (q.join(SocialPost,
                    SocialPostTarget.social_post_id == SocialPost.id)
             .filter(SocialPost.client_id == client_id))
    targets = q.order_by(SocialPostTarget.updated_at.desc()).limit(limit).all()

    removed = 0
    for t in targets:
        provider = get_provider(t.platform)
        if provider is None or not hasattr(provider, "post_exists"):
            continue
        if t.account is None or t.account.status != "active":
            continue
        try:
            token = AccountManager.access_token(t.account)
            exists = provider.post_exists(t.external_post_id, token)
        except Exception:  # noqa: BLE001 - never let one bad target abort sync
            continue
        if not exists:
            t.status = "removed"
            t.updated_at = datetime.utcnow()
            audit.record("post_removed_external", target_id=t.id,
                         post_id=t.social_post_id,
                         task_id=(t.post.task_id if t.post else None))
            removed += 1
    db.session.commit()
    return removed
