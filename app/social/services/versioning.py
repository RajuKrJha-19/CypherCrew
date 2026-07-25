"""VersioningService - JSON snapshots of a post's content for reversible,
auditable edit history (Version History)."""

from app.extensions import db
from app.models import ContentVersion


def _target_snapshot(t):
    return {
        "id": t.id,
        "platform": t.platform,
        "post_type": t.post_type,
        "caption": t.caption,
        "hashtags": t.hashtags,
        "account_id": t.social_account_id,
        "scheduled_for": t.scheduled_for.isoformat() if t.scheduled_for else None,
        "status": t.status,
    }


def snapshot_post(post, edited_by_id=None, commit=False):
    snap = {
        "title": post.title,
        "base_caption": post.base_caption,
        "status": post.status,
        "targets": [_target_snapshot(t) for t in post.targets],
    }
    row = ContentVersion(
        social_post_id=post.id,
        snapshot=snap,
        edited_by_id=edited_by_id,
    )
    db.session.add(row)
    if commit:
        db.session.commit()
    return row


def history(post_id):
    return (
        ContentVersion.query
        .filter_by(social_post_id=post_id)
        .order_by(ContentVersion.created_at.desc())
        .all()
    )
