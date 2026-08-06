"""Engage - the comments inbox.

Pulls comments from the platform for every published target into
`social_comments`, so the team can triage and reply from one place. Replies
are posted back to the platform and stored (is_ours=True) so a thread reads
end-to-end. Best-effort throughout: one unreachable post never aborts a sync.
"""

import random
import time
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError
from flask import current_app

from app.extensions import db
from app.models import SocialComment, SocialPost, SocialPostTarget
from app.social.registry import get_provider
from app.social.services import audit
from app.social.services.accounts import AccountManager


#: How stale `fetched_at` has to be before a re-sync bothers rewriting it.
#: The column records "when we last saw this comment on the platform" and no
#: per-row reader needs it fresher than this; the recency guard reads
#: created_at first and only falls back here. Refreshing it on every pass was
#: pure write amplification.
_FETCHED_AT_REFRESH = timedelta(hours=6)


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
              "errors": [], "by_reason": {}}
    now = datetime.utcnow()

    def fail(target, category, reason):
        # Name the channel and the post, not just the raw platform id. A
        # bare "Object with ID '1213..._1220...' does not exist" tells the
        # person reading it nothing they can act on; "Hope+ IVF — 'Diwali
        # offer'" tells them exactly which connection to look at. The full
        # per-post detail goes to `errors` + the log; the user-facing screen
        # gets the compact, grouped `by_reason` counts (see failure_summary).
        post = target.post
        where = target.account.display_name if target.account else target.platform
        what = (post.title if post and post.title else None) \
            or (target.caption or "")[:40] or f"post {target.id}"
        report["failed"] += 1
        report["errors"].append(f"{where} — “{what}”: {reason}")
        report["by_reason"][category] = report["by_reason"].get(category, 0) + 1
        current_app.logger.warning(
            "engage sync failed target=%s platform=%s account=%s reason=%s: %s",
            target.id, target.platform, target.social_account_id,
            category, reason)

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
            fail(target, "auth", "the channel is no longer connected")
            continue

        report["checked"] += 1
        try:
            token = AccountManager.access_token(target.account)
            comments = provider.list_comments(target.external_post_id, token)
        except Exception as exc:  # noqa: BLE001 - reported, not hidden
            category, reason = _classify(provider, exc)
            fail(target, category, reason)
            continue
        # Our own page/IG account's id. A comment authored by it is OURS - our
        # first comment, or a reply we typed natively on the platform - even
        # though we didn't create it through the app. Flagging it is_ours keeps
        # auto-reply and spam auto-mod from ever acting on our own words (e.g.
        # auto-hiding a first comment that carries a link).
        own_id = str(getattr(target.account, "external_id", "") or "")
        for c in comments:
            ext = c.get("external_id")
            if not ext:
                continue
            exists = SocialComment.query.filter_by(
                platform=target.platform, external_id=ext).first()
            if exists:
                # Only stamp it when it has actually gone stale. Rewriting
                # this on EVERY sync meant an UPDATE - and a row lock - for
                # every comment we already had: thousands per run, forever,
                # for a bookkeeping timestamp nothing reads per row. Those
                # locks are what a second, overlapping sync then queued
                # behind until statement_timeout cancelled it.
                if (exists.fetched_at is None
                        or (now - exists.fetched_at) > _FETCHED_AT_REFRESH):
                    exists.fetched_at = now
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
                        is_ours=bool(own_id and
                                     str(c.get("author_id") or "") == own_id),
                        fetched_at=datetime.utcnow()))
                    db.session.flush()
                report["new"] += 1
            except IntegrityError:
                continue

        # Commit THIS post's comments before fetching the next one. The loop
        # body makes a Graph call per post, and a single commit at the end
        # held every row lock it had taken across all of them - tens of
        # seconds on a busy account. A second sync (the cron, or a second
        # click of Fetch) then blocked on those locks until Postgres
        # cancelled its statement. Never hold a write transaction open across
        # network I/O; a per-post commit also means a failure halfway through
        # keeps the comments already read instead of discarding them.
        db.session.commit()

    return report


def purge_expired_comment_pii():
    """Anonymise commenter PII on third-party comments past the retention window.

    Data-retention hygiene (Meta Platform Terms: don't keep platform data longer
    than needed). Past ENGAGE_COMMENT_RETENTION_DAYS we NULL a commenter's
    name / id / picture and the comment body IN PLACE, keeping the row (id,
    timestamps, status, replied/removal bookkeeping) so counts, audit trails and
    thread continuity are unaffected - only the personal data is dropped.

    Scoped to is_ours=False: our own posted replies are our business records, not
    third-party PII, and keeping them preserves the reply audit. Idempotent - a
    row already purged (author_id IS NULL) is skipped. 0 days disables it.
    """
    days = int(current_app.config.get("ENGAGE_COMMENT_RETENTION_DAYS", 0) or 0)
    if days <= 0:
        return {"purged": 0, "skipped": "disabled"}
    cutoff = datetime.utcnow() - timedelta(days=days)
    purged = (SocialComment.query
              .filter(SocialComment.is_ours.is_(False),
                      SocialComment.created_at < cutoff,
                      SocialComment.author_id.isnot(None))
              .update({"author_name": None, "author_id": None,
                       "author_pic": None, "message": None},
                      synchronize_session=False))
    db.session.commit()
    if purged:
        current_app.logger.info(
            "[engage] retention: anonymised %s comment(s) older than %s days",
            purged, days)
    return {"purged": purged}


def ingest_comment_event(platform, external_post_id, *, external_id,
                         author_id=None, author_name=None, message=None,
                         parent_external_id=None, created_time=None):
    """Materialise ONE comment pushed by a Meta webhook (no polling).

    Finds the tracked target by external_post_id and returns None if we don't
    track that post (a comment on something not published/discovered here).
    Dedupes by (platform, external_id) - a re-delivered webhook is a no-op.
    is_ours is set the same way as the bulk sync (author == our page/IG id), so
    a reply we typed natively never gets auto-answered. Returns (comment,
    created) - created=False for a duplicate."""
    target = (SocialPostTarget.query
              .filter_by(platform=platform, external_post_id=external_post_id)
              .first())
    if target is None:
        return None, False
    existing = SocialComment.query.filter_by(
        platform=platform, external_id=external_id).first()
    if existing is not None:
        existing.fetched_at = datetime.utcnow()
        db.session.commit()
        return existing, False
    own_id = str(getattr(target.account, "external_id", "") or "")
    comment = SocialComment(
        target_id=target.id, platform=platform, external_id=external_id,
        parent_external_id=parent_external_id, author_name=author_name,
        author_id=author_id, message=message, created_time=created_time,
        is_ours=bool(own_id and str(author_id or "") == own_id),
        status="open", fetched_at=datetime.utcnow())
    try:
        with db.session.begin_nested():          # same anti-collision guard as sync
            db.session.add(comment)
            db.session.flush()
    except IntegrityError:
        return (SocialComment.query.filter_by(
            platform=platform, external_id=external_id).first()), False
    db.session.commit()
    return comment, True


# Graph "object does not exist / unsupported get request" (code 100, often
# subcode 33; 803 = deleted). The post is gone, too old, or its type simply
# doesn't expose a comments edge - expected on any account with history, and
# nothing the user can DO about it. So it must never reach the screen as a raw
# Graph dump with the object id; it's counted as "no longer available".
_GONE_CODES = {100, 803}
_GONE_HINTS = ("does not exist", "unsupported get request",
               "does not support this operation", "cannot be loaded")

#: The failure buckets sync produces, in the order the summary lists them
#: (most-actionable first), each mapped to plain, non-technical language.
#: Phrased without a verb so "<n> <phrase>" reads correctly for 1 or many.
_FAILURE_PHRASES = {
    "auth": "on a channel that needs reconnecting",
    "rate_limit": "rate-limited (try again shortly)",
    "unavailable": "no longer available on the platform",
    "refused": "refused by the platform",
}


def _classify(provider, exc):
    """(category, human_reason) for a post we couldn't read.

    The category groups the failure so the screen can show a compact count
    instead of a wall of per-post errors; the reason is plain, actionable
    language with NO raw Graph text and NO object ids. Mapped through the
    provider so a permissions problem reads as "reconnect the channel" - which
    a person can act on - rather than a bare error code.
    """
    from app.social.errors import AuthError, RateLimitError

    try:
        mapped = provider.map_error(exc)
    except Exception:  # noqa: BLE001
        mapped = exc

    if isinstance(mapped, AuthError):
        return ("auth", "the channel needs reconnecting — it is missing "
                        "permission to read comments")
    if isinstance(mapped, RateLimitError):
        return ("rate_limit", "the platform is rate-limiting us; try again "
                              "shortly")
    code = getattr(mapped, "code", None)
    text = str(getattr(mapped, "message", "") or mapped).lower()
    if code in _GONE_CODES or any(h in text for h in _GONE_HINTS):
        return ("unavailable", "the post is no longer available on the "
                              "platform (deleted, too old, or it does not "
                              "support reading comments)")
    return ("refused", "the platform refused the request")


def failure_summary(report):
    """A compact, grouped one-liner for the sync report's failures, e.g.
    "5 are no longer available on the platform, 1 need the channel reconnected".
    The per-post detail stays in report["errors"] and the log; the screen shows
    this. Empty string when nothing failed."""
    by_reason = report.get("by_reason") or {}
    bits = []
    for cat, phrase in _FAILURE_PHRASES.items():        # known, ordered
        n = by_reason.get(cat)
        if n:
            bits.append(f"{n} {phrase}")
    for cat, n in by_reason.items():                    # any future category
        if cat not in _FAILURE_PHRASES and n:
            bits.append(f"{n} could not be read ({cat})")
    return ", ".join(bits)


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


def _recent_enough(comment):
    """Only comments from the last ENGAGE_AUTO_MAX_AGE_DAYS days may be acted on
    automatically, so switching auto-reply/auto-mod on never machine-guns a
    backlog of months-old comments — it starts from recent ones and then keeps
    up with new ones (which arrive fresh via the webhook). 0 = no age limit.

    Judged by the platform's own timestamp (the comment's real age); when that's
    absent or unparseable, fall back to when we first saw it."""
    days = current_app.config.get("ENGAGE_AUTO_MAX_AGE_DAYS", 3)
    try:
        days = int(days)
    except (TypeError, ValueError):
        days = 3
    if days <= 0:
        return True
    cutoff = datetime.utcnow() - timedelta(days=days)
    raw = (comment.created_time or "").strip()
    if raw:
        try:
            # Facebook FEED webhooks deliver created_time as a Unix epoch int;
            # the Graph API (and IG) return ISO 8601 ("2026-08-05T12:00:00+0000").
            when = (datetime.utcfromtimestamp(int(raw)) if raw.isdigit()
                    else datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S"))
            return when >= cutoff
        except (ValueError, OverflowError, OSError):
            pass
    # Fallback when the platform time is missing/unparseable: the row's own
    # insert time (created_at is stable; fetched_at is refreshed on every
    # re-sync, which would keep resetting a comment's apparent age).
    stamp = getattr(comment, "created_at", None) or comment.fetched_at
    return (stamp or datetime.utcnow()) >= cutoff


#: Titles that describe nothing. An ad post is materialised with the literal
#: placeholder "Ad post" (engage_ads.sync_ad_targets), so feeding the title to
#: the model told it only that an ad existed - which is how ad comments got
#: generic replies while Studio posts got specific ones.
_EMPTY_TITLES = {"ad post", "untitled post", "untitled"}


def post_context_for(comment):
    """Public: what the post is about, for the AI drafting a reply to
    `comment`. Shared by the auto-reply path and the manual "AI draft" button
    so the two are never given different context.

    The post's own CAPTION is the real context - it is the text the commenter
    was reacting to. The title is only worth sending when it says something a
    placeholder does not.
    """
    target = comment.target
    if target is None:
        return None
    post = target.post
    title = (getattr(post, "title", None) or "").strip()
    if title.lower() in _EMPTY_TITLES:
        title = ""
    caption = (target.caption or "").strip()
    return "\n".join(p for p in (title, caption) if p) or None


def _is_reply_to_a_comment(comment):
    """True when this is a reply on a comment thread rather than a comment on
    the post itself.

    Deliberately not a bare `parent_external_id is not None`: Facebook omits
    `parent` on a top-level comment today, but if it ever answered with the
    POST id there instead, a bare check would silently stop every auto-reply.
    Comparing against the post's own id makes that failure impossible - the
    worst case becomes "we auto-reply as before", not "auto-reply dies".
    """
    parent = comment.parent_external_id
    if not parent:
        return False
    target = comment.target
    post_id = getattr(target, "external_post_id", None)
    return parent != post_id


def comment_is_auto_safe(comment, cfg):
    """May this comment be auto-replied without a human? Conservative by design:
    global+feature+admin switch on (in `cfg`), client opted in, the comment is
    not ours and not already handled, it is recent, it is short, it is NOT a
    question, and it hits no blocklisted word. Anything failing -> human queue."""
    if not cfg["enabled"]:
        return False
    if comment.is_ours or comment.replied or comment.status != "open":
        return False
    # Only comments ON the post, never replies within a comment thread. A
    # thread is a conversation someone is already having - often with a reply
    # WE posted - and a bot answering into it reads as talking to itself.
    # Replies stay in the inbox for a human; they are only exempt from the
    # unattended path.
    if _is_reply_to_a_comment(comment):
        return False
    if not _recent_enough(comment):      # never auto-reply an old backlog comment
        return False
    text = (comment.message or "").strip()
    if not text or len(text) > cfg["max_len"]:
        return False
    is_question = _looks_like_question(text)
    if is_question and not cfg.get("answer_questions"):
        return False                     # answering questions is off -> human
    if _has_link(text):                  # link/spam comment -> human
        return False
    low = text.lower()
    if any(w in low for w in cfg["blocklist"]):
        return False
    client = _comment_client(comment)
    if client is None or not getattr(client, "comment_autoreply", False):
        return False
    # A question may only be auto-answered when there are facts to ground it in
    # (the client's Client Brain). No facts -> we'd be guessing -> human.
    if is_question and not getattr(client, "brand_brain", None):
        return False
    return True


def auto_reply_comments_run(client_id=None):
    """Sync comments, then auto-reply the safe ones (guarded). For the cron/
    manual path."""
    sync_comments(client_id)
    return auto_reply_scan(client_id)


def auto_reply_scan(client_id=None):
    """Auto-reply the safe ALREADY-fetched comments (guarded), WITHOUT a sync.
    The webhook path calls this straight after ingesting a single comment, so a
    real-time reply costs no extra polling. Acknowledgments are per-post
    flood-capped; QUESTIONS (prospects/leads) are exempt from that cap and
    bounded only by their own ceiling (0 = unlimited), so a real question is
    never dropped. Budget-capped throughout, and the GENERATED reply is
    output-scanned. Returns counts; everything else stays in the human queue."""
    from app.ai import (client_brain, service as ai_service,
                        settings as ai_settings, usage as ai_usage)

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
    q_per_post = {}                       # question answers sent THIS run, per post
    qmax = cfg["question_max_per_post"]   # 0 = no limit; AD posts are exempt below

    # Anti-burst ceiling: the most auto-replies ONE sweep may send across all
    # posts. A comment surge (a viral post, or a running ad drawing many
    # questions) can't machine-gun a Page with hundreds of near-instant public
    # replies - which reads to Meta as bulk/automated spam. The remainder is
    # answered on the NEXT sweep, so nothing is dropped; only the burst flattens.
    run_max = int(current_app.config.get("ENGAGE_AUTO_MAX_PER_RUN", 25) or 0)
    interval_ms = int(
        current_app.config.get("ENGAGE_AUTO_REPLY_MIN_INTERVAL_MS", 0) or 0)
    testing = bool(current_app.config.get("TESTING"))

    sent = 0
    for c in pending:
        if run_max and sent >= run_max:
            break                            # per-run anti-burst ceiling reached
        if not comment_is_auto_safe(c, cfg):
            continue
        is_question = _looks_like_question((c.message or "").strip())
        if is_question:
            # Questions (prospects/leads) are exempt from the acknowledgment
            # flood cap. On an AD post they are also exempt from the per-post
            # question cap: an ad runs for months and every new question on it
            # is a fresh lead, so we never stop answering them - the per-run
            # ceiling + monthly budget still bound the pace. On an ORGANIC post
            # the per-post question cap still applies.
            if (qmax and not _is_ad_comment(c)
                    and q_per_post.get(c.target_id, 0) >= qmax):
                continue
            if not ai_usage.within_budget():
                break
        elif per_post.get(c.target_id, 0) >= cfg["max_per_post"]:
            continue
        client = _comment_client(c)
        facts = client_brain.facts_text(client) if client else ""
        post_context = post_context_for(c)
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

        # Nothing to post WITH (channel disconnected, platform unsupported) ->
        # leave it in the human queue. Checked BEFORE the claim so a comment
        # nobody can answer is never silently consumed by one.
        if not (get_provider(c.platform) and c.target
                and c.target.account is not None):
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
            reply(c, text, actor_id=None)       # posts + records via the poster
        except Exception:  # noqa: BLE001 - one bad post never aborts the run
            # reply() commits internally; if that commit (or its audit write)
            # failed mid-flush the session is left in a rolled-back-pending
            # state, and the NEXT iteration's very first query would raise
            # PendingRollbackError and abort the whole sweep. Clear it here.
            # Safe: the claim was committed in its own prior transaction
            # (above), so this rollback cannot un-claim or re-open the comment -
            # it only discards the poisoned pending state so the loop continues.
            db.session.rollback()
            # The claim is deliberately NOT released. The platform may already
            # have taken the reply - the failure can just as easily come from
            # reading the response or writing our own record after the POST -
            # and re-posting it on every later run is an unbounded public
            # duplicate. One auto-reply that never goes out is the cheaper
            # failure, so this fails closed and stays loud in the log.
            current_app.logger.exception("[engage] auto-reply post failed")
            continue

        # The platform accepted it. `reply()` returns the new comment's id, but
        # a success whose body carries no id is still a reply that is now live:
        # treating that as "not posted" is what used to release the claim and
        # post it again on the next run, past the per-post cap.
        c.auto_sent = True
        db.session.commit()
        if is_question:
            q_per_post[c.target_id] = q_per_post.get(c.target_id, 0) + 1
        else:
            per_post[c.target_id] = per_post.get(c.target_id, 0) + 1
        sent += 1
        # Pace the sweep so replies don't post machine-gun fast even within the
        # ceiling. AFTER the commit above, so no DB transaction is ever held
        # open across the pause. Jittered (0.5x-1.5x) so the cadence isn't a
        # tell-tale fixed interval. Skipped under TESTING.
        if interval_ms and not testing:
            time.sleep((interval_ms / 1000.0) * (0.5 + random.random()))
    return {"auto_replied": sent}


# -- spam moderation (hide / delete / restore) ------------------------------

def _is_ad_comment(comment):
    """True for a comment on an ad/boosted post (SocialPost.source == 'ad')."""
    post = comment.target.post if comment.target else None
    return bool(post and getattr(post, "source", None) == "ad")


def is_spam(comment, cfg):
    """A short, plain reason this comment is spam, or None. Conservative:
    matches the admin spam blocklist, or (if enabled) a link/bare-domain from a
    non-page commenter. Only NEW, not-ours, open comments are candidates."""
    if comment.is_ours or comment.replied or comment.status != "open":
        return None
    if not _recent_enough(comment):      # don't bulk-hide an old backlog either
        return None
    text = comment.message or ""
    low = text.lower()
    for word in cfg["blocklist"]:
        if word in low:
            return f"blocklist: {word}"
    # The link heuristic is SKIPPED for ad/boosted-post comments: ads attract
    # link comments from real prospects (sharing a number/profile/WhatsApp) as
    # well as bots, so auto-hiding every link would bury leads in Removed. The
    # explicit blocklist above still applies to the ad lane.
    if cfg["hide_links"] and _has_link(text) and not _is_ad_comment(comment):
        return "link spam"
    return None


def _provider_token(comment):
    """(provider, page_token) to act on a comment, or (None, None) if we can't
    (channel disconnected / unsupported) - same guard as reply().

    access_token can RAISE for a revoked/expired channel (its stored secret was
    wiped, so decryption fails). That used to bubble out of hide/delete as an
    uncaught 500; catch it here so the action fails gracefully instead."""
    provider = get_provider(comment.platform)
    if not (provider and comment.target and comment.target.account is not None):
        return None, None
    try:
        return provider, AccountManager.access_token(comment.target.account)
    except Exception:  # noqa: BLE001 - disconnected/expired channel -> can't act
        current_app.logger.exception(
            "[engage] token unavailable for comment %s (channel disconnected?)",
            comment.id)
        return None, None


def _moderation_reason(provider, exc):
    """A plain, actionable reason an Engage action (reply / hide / delete /
    restore) was refused — reusing the sync classifier so the message says what
    to DO, not a raw Graph error."""
    category, _ = _classify(provider, exc)
    return {
        "auth": "the channel is missing the permission needed — reconnect it in "
                "Channels to refresh its permissions",
        "rate_limit": "the platform is rate-limiting us — try again shortly",
        "unavailable": "that comment or post is no longer on the platform",
    }.get(category,
          "the platform refused it — the channel may need reconnecting, or this "
          "comment is on an ad/Instagram post that can't be actioned this way")


def classify_failure(comment, exc):
    """Public: the plain, actionable reason an Engage action on `comment` failed
    (used by the manual reply route, which catches so a bad reply never 500s)."""
    return _moderation_reason(get_provider(comment.platform), exc)


_NEEDS_RECONNECT = "the channel needs reconnecting — reconnect it in Channels"


def automod_run(client_id=None):
    """Sync comments, then auto-HIDE the spam ones. For the cron path."""
    sync_comments(client_id)
    return automod_scan(client_id)


def automod_scan(client_id=None):
    """Auto-HIDE spam among the ALREADY-fetched open comments (guarded,
    reversible). Hide (not delete) so a false positive is recoverable from the
    Removed tab. Self-gated: returns early unless auto-mod is fully enabled, so
    the Fetch button can call it after its own sync without a second sync."""
    from app.ai import settings as ai_settings

    cfg = ai_settings.automod_config()
    if not cfg["enabled"]:
        return {"hidden": 0, "skipped": "disabled"}

    q = SocialComment.query.filter(SocialComment.is_ours.is_(False),
                                   SocialComment.status == "open")
    if client_id:
        q = (q.join(SocialPostTarget,
                    SocialComment.target_id == SocialPostTarget.id)
              .join(SocialPost,
                    SocialPost.id == SocialPostTarget.social_post_id)
              .filter(SocialPost.client_id == client_id))
    pending = q.order_by(SocialComment.id.asc()).all()

    hidden = 0
    for c in pending:
        if hidden >= cfg["max_per_run"]:
            break
        client = _comment_client(c)                 # per-client opt-in
        if client is None or not getattr(client, "comment_automod", False):
            continue
        reason = is_spam(c, cfg)
        if not reason:
            continue
        provider, token = _provider_token(c)
        if not provider:
            continue

        # Atomically CLAIM (mark removed) BEFORE the platform call, so two
        # overlapping runs can't both hide it. Revert if the hide fails, so a
        # transient error leaves it visible for a retry rather than stuck as a
        # "removed" comment that was never actually hidden.
        claimed = (SocialComment.query
                   .filter_by(id=c.id, status="open")
                   .update({"status": "removed", "removal_kind": "auto",
                            "removal_action": "hidden", "removal_reason": reason,
                            "removed_by_id": None,
                            "removed_at": datetime.utcnow()},
                           synchronize_session=False))
        db.session.commit()
        if not claimed:
            continue

        try:
            provider.set_comment_hidden(c.external_id, token, hidden=True)
        except Exception:  # noqa: BLE001 - one failure never aborts the run
            SocialComment.query.filter_by(id=c.id).update(
                {"status": "open", "removal_kind": None, "removal_action": None,
                 "removal_reason": None, "removed_at": None},
                synchronize_session=False)
            db.session.commit()
            current_app.logger.exception(
                "[engage] auto-hide failed for comment %s", c.id)
            continue

        audit.record("comment_hidden", target_id=c.target_id,
                     post_id=(c.target.social_post_id if c.target else None),
                     actor_id=None,
                     detail={"comment_id": c.external_id, "reason": reason,
                             "auto": True})
        hidden += 1
    return {"hidden": hidden}


def _record_removal(comment, actor_id, action, reason):
    comment.status = "removed"
    comment.removal_kind = "manual"
    comment.removal_action = action                 # hidden | deleted
    comment.removal_reason = reason
    comment.removed_by_id = actor_id
    comment.removed_at = datetime.utcnow()


def hide(comment, actor_id=None):
    """Manually hide a comment (reversible). Returns (ok, reason) - reason is a
    plain, actionable message when it couldn't be hidden, else None."""
    provider, token = _provider_token(comment)
    if not provider:
        return False, _NEEDS_RECONNECT
    try:
        provider.set_comment_hidden(comment.external_id, token, hidden=True)
        _record_removal(comment, actor_id, "hidden", "manual")
        audit.record("comment_hidden", target_id=comment.target_id,
                     post_id=(comment.target.social_post_id if comment.target else None),
                     actor_id=actor_id,
                     detail={"comment_id": comment.external_id, "auto": False})
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - a manual hide must never 500
        db.session.rollback()
        current_app.logger.exception("[engage] hide failed for %s", comment.id)
        return False, _moderation_reason(provider, exc)
    return True, None


def delete(comment, actor_id=None):
    """Permanently delete a comment on the platform. NOT reversible; we keep the
    local record (in the Removed tab) for the audit trail. Returns (ok, reason)."""
    provider, token = _provider_token(comment)
    if not provider:
        return False, _NEEDS_RECONNECT
    try:
        provider.delete_comment(comment.external_id, token)
        _record_removal(comment, actor_id, "deleted", "manual")
        audit.record("comment_deleted", target_id=comment.target_id,
                     post_id=(comment.target.social_post_id if comment.target else None),
                     actor_id=actor_id, detail={"comment_id": comment.external_id})
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - a manual delete must never 500
        db.session.rollback()
        current_app.logger.exception("[engage] delete failed for %s", comment.id)
        return False, _moderation_reason(provider, exc)
    return True, None


def restore(comment, actor_id=None):
    """Unhide a previously HIDDEN comment back into the inbox. A deleted comment
    can't be restored (it's gone on the platform). Returns (ok, reason)."""
    if comment.removal_action != "hidden":
        return False, "a deleted comment can't be restored (it's gone on the platform)"
    provider, token = _provider_token(comment)
    if not provider:
        return False, _NEEDS_RECONNECT
    try:
        provider.set_comment_hidden(comment.external_id, token, hidden=False)
        comment.status = "open"
        comment.removal_kind = None
        comment.removal_action = None
        comment.removal_reason = None
        comment.removed_by_id = None
        comment.removed_at = None
        audit.record("comment_restored", target_id=comment.target_id,
                     post_id=(comment.target.social_post_id if comment.target else None),
                     actor_id=actor_id, detail={"comment_id": comment.external_id})
        db.session.commit()
    except Exception as exc:  # noqa: BLE001 - a manual restore must never 500
        db.session.rollback()
        current_app.logger.exception("[engage] restore failed for %s", comment.id)
        return False, _moderation_reason(provider, exc)
    return True, None
