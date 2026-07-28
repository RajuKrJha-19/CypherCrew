"""Engage - the comments inbox.

Pulls comments from the platform for every published target into
`social_comments`, so the team can triage and reply from one place. Replies
are posted back to the platform and stored (is_ours=True) so a thread reads
end-to-end. Best-effort throughout: one unreachable post never aborts a sync.
"""

from datetime import datetime

from flask import current_app

from app.extensions import db
from app.models import SocialComment, SocialPost, SocialPostTarget
from app.social.registry import get_provider
from app.social.services import audit
from app.social.services.accounts import AccountManager


def _published_targets(client_id=None):
    q = (SocialPostTarget.query
         .filter(SocialPostTarget.status.in_(["published", "removed"]),
                 SocialPostTarget.external_post_id.isnot(None)))
    if client_id:
        q = q.join(SocialPost,
                   SocialPost.id == SocialPostTarget.social_post_id) \
             .filter(SocialPost.client_id == client_id)
    return q.all()


def sync_comments(client_id=None):
    """Fetch comments for every published target into social_comments.

    Returns a report, not just a count. It used to return an int and
    swallow every failure with a bare `continue`, so a sync where every
    single call was refused looked exactly like a sync where there was
    genuinely nothing new - the screen said "you're all caught up" while
    the real answer was "we were not allowed to look". The caller needs to
    be able to tell those apart.

    Still best-effort: one unreachable post never aborts the rest.
    Idempotent - the unique (platform, external_id) constraint means a
    re-sync never duplicates.
    """
    report = {"checked": 0, "new": 0, "skipped": 0, "failed": 0,
              "errors": []}

    def fail(target, reason):
        report["failed"] += 1
        if reason not in report["errors"]:
            report["errors"].append(reason)
        current_app.logger.warning(
            "engage sync failed target=%s platform=%s: %s",
            target.id, target.platform, reason)

    for target in _published_targets(client_id):
        provider = get_provider(target.platform)
        caps = provider.capabilities if provider else None

        if provider is None:
            report["skipped"] += 1
            continue
        if not (caps and getattr(caps, "supports_comments", False)):
            report["skipped"] += 1
            continue
        if target.account is None:
            fail(target, "the channel is no longer connected")
            continue

        report["checked"] += 1
        try:
            token = AccountManager.access_token(target.account)
            comments = provider.list_comments(target.external_post_id, token)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            fail(target, _reason(provider, exc))
            continue
        for c in comments:
            ext = c.get("external_id")
            if not ext:
                continue
            exists = SocialComment.query.filter_by(
                platform=target.platform, external_id=ext).first()
            if exists:
                exists.fetched_at = datetime.utcnow()
                continue
            db.session.add(SocialComment(
                target_id=target.id, platform=target.platform,
                external_id=ext, parent_external_id=c.get("parent_external_id"),
                author_name=c.get("author_name"), author_id=c.get("author_id"),
                message=c.get("message"), created_time=c.get("created_time"),
                is_ours=False, fetched_at=datetime.utcnow()))
            report["new"] += 1

    db.session.commit()
    return report


def _reason(provider, exc):
    """A short, human reason a post could not be read.

    Mapped through the provider so a permissions problem reads as one
    rather than as a raw Graph error code - "reconnect the channel" is
    something a person can act on.
    """
    from app.social.errors import AuthError, RateLimitError

    try:
        mapped = provider.map_error(exc)
    except Exception:  # noqa: BLE001
        mapped = exc

    if isinstance(mapped, AuthError):
        return ("the channel needs reconnecting - it is missing permission "
                "to read comments")
    if isinstance(mapped, RateLimitError):
        return "the platform is rate-limiting us; try again shortly"
    return str(mapped)[:200] or "the platform refused the request"


def reply(comment, text, actor_id=None):
    """Post a reply to `comment` on the platform and record it. Returns the new
    comment's external id (or None if the platform call produced nothing)."""
    text = (text or "").strip()
    if not text:
        return None
    provider = get_provider(comment.platform)
    if not (provider and comment.target and comment.target.account is not None):
        return None
    token = AccountManager.access_token(comment.target.account)
    ext = provider.reply_to_comment(comment.external_id, text, token)

    comment.replied = True
    comment.status = "done"
    if ext:
        db.session.add(SocialComment(
            target_id=comment.target_id, platform=comment.platform,
            external_id=ext, parent_external_id=comment.external_id,
            author_name="You", is_ours=True, message=text, replied=True,
            status="done", fetched_at=datetime.utcnow()))
    audit.record("comment_replied", target_id=comment.target_id,
                 post_id=(comment.target.social_post_id
                          if comment.target else None),
                 actor_id=actor_id, detail={"comment_id": comment.external_id})
    db.session.commit()
    return ext


def mark_done(comment, done=True):
    comment.status = "done" if done else "open"
    db.session.commit()
