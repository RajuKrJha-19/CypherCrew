"""Engage - the comments inbox.

Pulls comments from the platform for every published target into
`social_comments`, so the team can triage and reply from one place. Replies
are posted back to the platform and stored (is_ours=True) so a thread reads
end-to-end. Best-effort throughout: one unreachable post never aborts a sync.
"""

from datetime import datetime

from sqlalchemy.exc import IntegrityError
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
        # Name the channel and the post, not just the raw platform id. A
        # bare "Object with ID '1213..._1220...' does not exist" tells the
        # person reading it nothing they can act on; "Hope+ IVF — 'Diwali
        # offer'" tells them exactly which connection to look at.
        post = target.post
        where = target.account.display_name if target.account else target.platform
        what = (post.title if post and post.title else None) \
            or (target.caption or "")[:40] or f"post {target.id}"
        report["failed"] += 1
        report["errors"].append(f"{where} — “{what}”: {reason}")
        current_app.logger.warning(
            "engage sync failed target=%s platform=%s account=%s: %s",
            target.id, target.platform, target.social_account_id, reason)

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
            # Two overlapping syncs (cron overrun, or cron + a manual trigger)
            # can both miss the SELECT above and both insert the same
            # (platform, external_id), whose unique constraint would then abort
            # the WHOLE batch commit. Insert inside a SAVEPOINT and treat the
            # collision as "already fetched" instead of failing everything.
            try:
                with db.session.begin_nested():
                    db.session.add(SocialComment(
                        target_id=target.id, platform=target.platform,
                        external_id=ext,
                        parent_external_id=c.get("parent_external_id"),
                        author_name=c.get("author_name"),
                        author_id=c.get("author_id"),
                        author_pic=c.get("author_pic"),
                        message=c.get("message"),
                        created_time=c.get("created_time"),
                        is_ours=False, fetched_at=datetime.utcnow()))
                    db.session.flush()
                report["new"] += 1
            except IntegrityError:
                continue

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


# -- guarded comment auto-reply ---------------------------------------------

import re

#: A generated auto-reply longer than this is treated as suspect (steered by
#: the comment, or a runaway) and dropped to the human queue.
_AUTO_COMMENT_MAX_CHARS = 300

#: Any URL/bare-domain in the comment OR the generated reply -> human. Kills the
#: "make the bot post a link" prompt-injection / phishing vector on the
#: unattended public path.
_LINK_RE = re.compile(
    r"https?://|www\.|\b[a-z0-9-]+\.(?:com|net|org|in|io|co|ly|me|link|xyz|"
    r"shop|store|info|biz|app|site|online)\b", re.I)

#: Question intent - ASCII + Unicode question marks, plus common English AND
#: Hinglish/Hindi question tokens (our audience often omits the "?"). A hit
#: routes the comment to a human rather than auto-answering it wrong.
_Q_MARKS = ("?", "？", "؟")
_Q_TOKENS = {
    "how", "what", "when", "where", "why", "which", "whom", "much", "cost",
    "price", "rate", "available", "stock", "delivery", "deliver", "ship",
    "shipping", "dm", "kaise", "kaisa", "kaha", "kahan", "kitna", "kitne",
    "kitni", "kab", "kaun", "kaunsa", "konsa", "kidhar", "kimat", "keemat",
}


def _has_link(text):
    return bool(_LINK_RE.search(text or ""))


def _looks_like_question(text):
    t = text or ""
    if any(m in t for m in _Q_MARKS):
        return True
    return bool(set(re.findall(r"[a-z]+", t.lower())) & _Q_TOKENS)


def _comment_client(comment):
    target = comment.target
    post = target.post if target else None
    cid = getattr(post, "client_id", None)
    if not cid:
        return None
    from app.models import Client
    return Client.query.get(cid)


def comment_is_auto_safe(comment, cfg):
    """May this comment be auto-replied without a human? Conservative by design:
    global+feature+admin switch on (in `cfg`), client opted in, the comment is
    not ours and not already handled, it is short, it is NOT a question, and it
    hits no blocklisted word. Anything failing -> the human queue."""
    if not cfg["enabled"]:
        return False
    if comment.is_ours or comment.replied or comment.status != "open":
        return False
    text = (comment.message or "").strip()
    if not text or len(text) > cfg["max_len"]:
        return False
    if _looks_like_question(text):       # a question needs a real answer -> human
        return False
    if _has_link(text):                  # link/spam comment -> human
        return False
    low = text.lower()
    if any(w in low for w in cfg["blocklist"]):
        return False
    client = _comment_client(comment)
    if client is None or not getattr(client, "comment_autoreply", False):
        return False
    return True


def auto_reply_comments_run(client_id=None):
    """Sync comments, then auto-reply the safe ones (guarded). Per-post cap is a
    TOTAL across runs (a viral thread can't be flooded), budget-capped, and the
    GENERATED reply is output-scanned. Returns counts; everything else stays in
    the human queue."""
    from app.ai import (client_brain, service as ai_service,
                        settings as ai_settings, usage as ai_usage)

    sync_comments(client_id)
    cfg = ai_settings.comment_config()
    if not cfg["enabled"]:
        return {"auto_replied": 0, "skipped": "disabled"}
    if not ai_usage.within_budget():
        return {"auto_replied": 0, "skipped": "over budget"}

    q = SocialComment.query.filter(SocialComment.is_ours.is_(False),
                                   SocialComment.replied.is_(False),
                                   SocialComment.status == "open")
    if client_id:
        q = (q.join(SocialPostTarget,
                    SocialComment.target_id == SocialPostTarget.id)
              .join(SocialPost,
                    SocialPost.id == SocialPostTarget.social_post_id)
              .filter(SocialPost.client_id == client_id))
    pending = q.order_by(SocialComment.id.asc()).all()
    if not pending:
        return {"auto_replied": 0}

    # Per-post cap counts replies ALREADY auto-sent on each post, so the cap is
    # a lifetime total per post rather than per run.
    tids = list({c.target_id for c in pending})
    per_post = dict(
        db.session.query(SocialComment.target_id, db.func.count())
        .filter(SocialComment.auto_sent.is_(True),
                SocialComment.target_id.in_(tids))
        .group_by(SocialComment.target_id).all())

    sent = 0
    for c in pending:
        if not comment_is_auto_safe(c, cfg):
            continue
        if per_post.get(c.target_id, 0) >= cfg["max_per_post"]:
            continue
        client = _comment_client(c)
        facts = client_brain.facts_text(client) if client else ""
        post = c.target.post if c.target else None
        post_context = "\n".join(p for p in (
            (post.title if post else None),
            (c.target.caption if c.target else None)) if p) or None
        try:
            text = (ai_service.generate_comment_reply(
                comment_text=c.message, author=c.author_name,
                business_name=getattr(client, "client_name", None),
                brand_voice=getattr(client, "brand_voice", None),
                brand_notes=getattr(client, "brand_guidelines_notes", None),
                facts=(facts or None), post_context=post_context,
                actor_id=None, client_id=getattr(client, "id", None)) or "").strip()
        except Exception:  # noqa: BLE001 - one bad draft never aborts the run
            current_app.logger.exception("[engage] auto-reply draft failed")
            continue
        # Output guards on the GENERATED public reply: empty, over-long,
        # blocklisted, or containing a link (injection/phishing) -> human.
        if not text or len(text) > _AUTO_COMMENT_MAX_CHARS:
            continue
        low_reply = text.lower()
        if any(w in low_reply for w in cfg["blocklist"]) or _has_link(text):
            continue

        # Atomically CLAIM the comment before posting, so two overlapping cron
        # runs can't both post to it (the loser's UPDATE matches 0 rows).
        claimed = (SocialComment.query
                   .filter_by(id=c.id, replied=False, status="open")
                   .update({"replied": True, "status": "done"},
                           synchronize_session=False))
        db.session.commit()
        if not claimed:
            continue                            # another run/human took it

        try:
            ext = reply(c, text, actor_id=None)  # posts + records via the poster
        except Exception:  # noqa: BLE001 - one bad post never aborts the run
            current_app.logger.exception("[engage] auto-reply post failed")
            ext = None
        if ext is None:
            # Nothing confirmed posted -> release the claim back to the queue.
            c.replied = False
            c.status = "open"
            db.session.commit()
            continue
        c.auto_sent = True
        db.session.commit()
        per_post[c.target_id] = per_post.get(c.target_id, 0) + 1
        sent += 1
    return {"auto_replied": sent}
