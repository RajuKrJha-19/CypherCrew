"""Review sync, AI-drafted replies, and the guarded auto-reply run.

Human-in-the-loop by default: a synced review sits at reply_status="pending"
until someone approves a reply. Auto-reply only ever fires for reviews that
pass every guardrail AND whose client opted in AND while the global switch is
on - everything else stays in the human queue.
"""
from datetime import datetime

from flask import current_app

from app.ai import service as ai_service
from app.extensions import db
from app.models import Client, GoogleReview
from app.social.reviews.source import get_source


# -- sync -------------------------------------------------------------------

def sync_reviews(account):
    """Upsert the account's reviews. Idempotent on (account, external_id):
    updates rating/comment on an existing row but never touches its reply
    state or original timestamp."""
    source = get_source()
    fetched = source.list_reviews(account)
    new = 0
    for r in fetched:
        ext = r.get("external_id")
        if not ext:
            continue
        row = GoogleReview.query.filter_by(
            account_id=account.id, external_id=ext).first()
        if row is None:
            db.session.add(GoogleReview(
                account_id=account.id, external_id=ext,
                reviewer_name=r.get("reviewer_name"),
                rating=r.get("rating"), comment=r.get("comment") or "",
                review_created_at=r.get("created_at"),
                fetched_at=datetime.utcnow(), reply_status="pending"))
            new += 1
        else:
            row.rating = r.get("rating")
            row.comment = r.get("comment") or ""
            row.fetched_at = datetime.utcnow()
    db.session.commit()
    return {"fetched": len(fetched), "new": new}


# -- helpers ----------------------------------------------------------------

def _client_of(review):
    account = review.account
    cid = getattr(account, "client_id", None)
    return Client.query.get(cid) if cid else None


def _draft_text(review, actor_id=None):
    from app.ai import client_brain
    client = _client_of(review)
    facts = client_brain.facts_text(client) if client else ""
    return ai_service.generate_reply(
        review_text=review.comment or "",
        rating=review.rating,
        reviewer=review.reviewer_name,
        business_name=getattr(client, "client_name", None),
        brand_voice=getattr(client, "brand_voice", None),
        brand_notes=getattr(client, "brand_guidelines_notes", None),
        facts=(facts or None),
        actor_id=actor_id,
        client_id=getattr(client, "id", None),
    )


# -- human actions ----------------------------------------------------------

def draft_reply(review, actor_id=None):
    """Generate + store an AI draft, leaving it for a human to approve."""
    text = _draft_text(review, actor_id=actor_id)
    review.reply_text = text
    review.reply_ai_generated = True
    review.reply_status = "drafted"
    db.session.commit()
    return text


def post_reply(review, text, user):
    """Post a (human-approved) reply to Google and mark it live."""
    get_source().post_reply(review.account, review.external_id, text)
    review.reply_text = text
    review.reply_status = "posted"
    review.replied_at = datetime.utcnow()
    review.replied_by_id = getattr(user, "id", None)
    db.session.commit()


def skip(review, user):
    review.reply_status = "skipped"
    review.replied_by_id = getattr(user, "id", None)
    db.session.commit()


# -- guarded auto-reply -----------------------------------------------------

def _autoreply_cfg():
    """Effective guardrails - admin-editable (AISettings) with env fallback."""
    from app.ai import settings as ai_settings
    return ai_settings.autoreply_config()


def is_auto_safe(review, cfg=None):
    """May this review be auto-replied without a human? Conservative by design.
    The AI review-reply feature on, global switch on, client opted in, high
    rating, short/no text, and no blocklisted word. Anything failing -> the
    human queue. `cfg` is the resolved guardrail dict (autoreply_config); passed
    in by the run loop so it isn't re-read per review."""
    from app.ai import settings as ai_settings
    if cfg is None:
        cfg = _autoreply_cfg()
    # If an admin has turned the review-reply feature off, nothing auto-posts.
    if not ai_settings.feature_enabled("reply"):
        return False
    if not cfg["enabled"]:
        return False
    client = _client_of(review)
    if client is None or not getattr(client, "gmb_autoreply", False):
        return False
    if (review.rating or 0) < cfg["min_rating"]:
        return False
    text = review.comment or ""
    if len(text) > cfg["max_len"]:
        return False
    low = text.lower()
    if any(word in low for word in cfg["blocklist"]):
        return False
    return True


# A drafted auto-reply longer than this is treated as suspect (a model that
# ran long, or was steered by review text) and is NOT auto-posted - it drops to
# the human queue instead. The prompt asks for 1-3 sentences.
_AUTO_REPLY_MAX_CHARS = 600


def auto_reply_run(account):
    """Sync, then auto-reply the safe pending reviews (rate-limited per run).
    Returns counts. Everything unsafe - and anything the draft itself makes
    look risky - stays pending for a human."""
    from app.ai import usage as ai_usage

    sync_reviews(account)
    # Auto-reply is unattended and costs money; respect the AI budget cap.
    if not ai_usage.within_budget():
        return {"auto_replied": 0, "skipped": "over budget"}

    guards = _autoreply_cfg()                       # resolved once per run
    cap = guards["max_per_run"]
    pending = (GoogleReview.query
               .filter_by(account_id=account.id, reply_status="pending")
               .order_by(GoogleReview.id.asc()).all())
    sent = 0
    for review in pending:
        if sent >= cap:
            break
        if not is_auto_safe(review, cfg=guards):
            continue
        text = (_draft_text(review) or "").strip()
        # Guards on the GENERATED reply (it posts publicly, unattended): never
        # auto-post an empty or over-long draft, and never one that itself
        # contains a blocklisted word - a crafted short review could otherwise
        # steer the model into a harmful reply. Any hit -> the human queue.
        if not text or len(text) > _AUTO_REPLY_MAX_CHARS:
            continue
        low_reply = text.lower()
        if any(word in low_reply for word in guards["blocklist"]):
            continue
        get_source().post_reply(review.account, review.external_id, text)
        review.reply_text = text
        review.reply_ai_generated = True
        review.auto_sent = True
        review.reply_status = "posted"
        review.replied_at = datetime.utcnow()
        review.replied_by_id = None            # None = auto-sent
        db.session.commit()
        sent += 1
    return {"auto_replied": sent}
