"""Garbage collection for directly-uploaded social media.

`social.upload_media` streams a file to R2 under `social_uploads/<uuid>_<name>`
and hands the object key to the browser. The key only becomes a durable
`SocialMediaAsset` row if the user actually saves the post. Two things
therefore orphan an R2 object:

  * an upload the user never saved (composer abandoned, or the file removed
    before saving), and
  * a post/target deleted after saving (the SocialMediaAsset row goes, the
    object does not).

Rather than scatter R2 deletes across every mutation path - fragile, and
unsafe because `duplicate_post` reuses an object_key across posts, so one
post's delete must not remove an object another still points at - this is a
single reconciliation sweep: list the prefix, subtract everything any
SocialMediaAsset still references, and delete only what is left AND older
than a grace window (so an in-flight upload being composed is never touched).

Runs from the token-protected /internal/social/media-gc/run cron endpoint,
same shape as the scheduler/worker/analytics jobs.
"""

from datetime import datetime, timedelta, timezone

from flask import current_app

from app.extensions import db
from app.models import SocialMediaAsset
from app.storage.storage_service import StorageService

_PREFIX = "social_uploads/"


def _grace_hours():
    # A composer session never stays open this long, so an object older than
    # the window with no reference really is abandoned. Configurable for ops.
    try:
        return int(current_app.config.get("SOCIAL_UPLOAD_GC_HOURS", 24))
    except (TypeError, ValueError):
        return 24


def sweep(now=None, dry_run=False):
    """Delete orphaned `social_uploads/` objects. Returns a summary dict.

    An object is deleted only when BOTH hold:
      * no SocialMediaAsset (any post, any status) references its key, and
      * it is older than the grace window.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=_grace_hours())

    storage = StorageService()
    try:
        objects = storage.list_files(prefix=_PREFIX)
    except Exception:  # noqa: BLE001 - a listing failure must not crash cron
        current_app.logger.exception("[social-media-gc] list failed")
        return {"listed": 0, "referenced": 0, "orphaned": 0,
                "deleted": 0, "skipped_recent": 0, "failed": 0}

    # Every key still referenced by a row - regardless of post status, so a
    # live object (including one shared via duplicate_post) is never a target.
    referenced = {
        k for (k,) in db.session.query(SocialMediaAsset.object_key)
        .filter(SocialMediaAsset.object_key.like(_PREFIX + "%"))
        .distinct().all()
        if k
    }

    deleted = skipped_recent = failed = orphaned = 0
    for obj in objects:
        key = obj.get("object_key")
        if not key or key in referenced:
            continue
        orphaned += 1

        lm = obj.get("last_modified")
        if lm is not None:
            # boto3 hands back tz-aware UTC; normalise a naive value just in
            # case a provider returns one, so the comparison never throws.
            if lm.tzinfo is None:
                lm = lm.replace(tzinfo=timezone.utc)
            if lm > cutoff:
                skipped_recent += 1
                continue

        if dry_run:
            continue
        try:
            storage.delete(object_key=key)
            deleted += 1
        except Exception:  # noqa: BLE001 - one bad key never stops the sweep
            failed += 1
            current_app.logger.warning(
                "[social-media-gc] delete failed key=%s", key)

    summary = {
        "listed": len(objects),
        "referenced": len(referenced),
        "orphaned": orphaned,
        "deleted": deleted,
        "skipped_recent": skipped_recent,
        "failed": failed,
    }
    current_app.logger.info("[social-media-gc] %s", summary)
    return summary
