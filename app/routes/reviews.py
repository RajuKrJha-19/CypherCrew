"""Google review reply inbox: synced reviews with AI-drafted, human-approved
replies (and, opt-in, guarded auto-reply). Gated by can_use_social; the AI
draft additionally needs AI to be on + within budget.
"""
from flask import (
    Blueprint, abort, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.models import GoogleReview, SocialAccount
from app.social.reviews import service as reviews_service
from app.utils.permissions import can_use_social

reviews_bp = Blueprint("reviews", __name__, url_prefix="/reviews")

_STATUS_ORDER = {"pending": 0, "drafted": 1, "posted": 2, "skipped": 3}


def _guard():
    if not can_use_social(current_user):
        abort(403)


def _gbp_accounts():
    return (SocialAccount.query
            .filter_by(platform="google_business")
            .order_by(SocialAccount.display_name.asc()).all())


def _review_or_404(review_id):
    review = GoogleReview.query.get_or_404(review_id)
    # The review's account must be a GBP account the social team manages;
    # can_use_social already gates the surface, so no per-account owner check
    # beyond that (all connected accounts are agency-managed).
    return review


def _ai_ready():
    from app.ai import settings as ai_settings, usage as ai_usage
    return ai_settings.is_enabled() and ai_usage.within_budget()


@reviews_bp.route("/")
@login_required
def inbox():
    _guard()
    accounts = _gbp_accounts()
    account_ids = [a.id for a in accounts]
    reviews = []
    if account_ids:
        reviews = GoogleReview.query.filter(
            GoogleReview.account_id.in_(account_ids)).all()
        reviews.sort(key=lambda r: (
            _STATUS_ORDER.get(r.reply_status, 9),
            -(r.review_created_at.timestamp() if r.review_created_at else 0)))
    return render_template(
        "reviews/inbox.html",
        accounts=accounts,
        reviews=reviews,
        ai_ready=_ai_ready(),
    )


@reviews_bp.route("/sync", methods=["POST"])
@login_required
def sync():
    _guard()
    total_new = 0
    for account in _gbp_accounts():
        try:
            total_new += reviews_service.sync_reviews(account).get("new", 0)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "[reviews] sync failed for account %s", account.id)
    flash(f"Synced reviews — {total_new} new.", "success")
    return redirect(url_for("reviews.inbox"))


@reviews_bp.route("/<int:review_id>/draft", methods=["POST"])
@login_required
def draft(review_id):
    _guard()
    if not _ai_ready():
        return jsonify(error="AI assist is not available."), 503
    review = _review_or_404(review_id)
    from app.ai.errors import AIError
    try:
        text = reviews_service.draft_reply(review, actor_id=current_user.id)
    except AIError:
        return jsonify(error="Couldn't draft a reply — please try again."), 502
    except Exception:  # noqa: BLE001
        current_app.logger.exception("[reviews] draft failed")
        return jsonify(error="Something went wrong drafting the reply."), 500
    return jsonify(reply=text)


@reviews_bp.route("/<int:review_id>/reply", methods=["POST"])
@login_required
def reply(review_id):
    _guard()
    review = _review_or_404(review_id)
    text = (request.form.get("reply_text") or "").strip()
    if not text:
        flash("Write a reply before posting.", "error")
        return redirect(url_for("reviews.inbox"))
    try:
        reviews_service.post_reply(review, text, current_user)
        flash("Reply posted.", "success")
    except Exception:  # noqa: BLE001
        current_app.logger.exception("[reviews] post reply failed")
        flash("Couldn't post the reply — please try again.", "error")
    return redirect(url_for("reviews.inbox"))


@reviews_bp.route("/<int:review_id>/skip", methods=["POST"])
@login_required
def skip(review_id):
    _guard()
    review = _review_or_404(review_id)
    reviews_service.skip(review, current_user)
    flash("Review marked as skipped.", "success")
    return redirect(url_for("reviews.inbox"))
