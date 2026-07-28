"""Public legal + compliance pages.

Everything here is deliberately reachable WITHOUT logging in: Meta's app
review fetches these URLs anonymously, and a person who has already
removed our app from their Facebook account has no way to log in to ask
for their data to be deleted.

Routes:
    /legal/privacy                     Privacy Policy
    /legal/terms                       Terms of Service
    /legal/data-deletion               Data Deletion Instructions (+ form)
    /legal/data-deletion/callback      Meta's signed deletion callback
    /legal/data-deletion/status/<code> Status of one request
"""

import base64
import hashlib
import hmac
import json
import logging

from flask import (
    Blueprint, abort, current_app, flash, redirect, render_template, request,
    url_for,
)

from app.extensions import csrf, db, limiter
from app.models import DataDeletionRequest

legal_bp = Blueprint("legal", __name__, url_prefix="/legal")

log = logging.getLogger(__name__)


# --------------------------------------------------------------------------
# Static documents
# --------------------------------------------------------------------------

@legal_bp.route("/privacy")
def privacy():
    return render_template("legal/privacy.html", **_doc_context())


@legal_bp.route("/terms")
def terms():
    return render_template("legal/terms.html", **_doc_context())


def _doc_context():
    """Shared facts, from config rather than hard-coded into the prose, so
    the operator's real contact details appear on a deployed instance."""
    cfg = current_app.config
    return {
        "company": cfg.get("LEGAL_COMPANY_NAME", "CypherCrew"),
        "contact_email": cfg.get("LEGAL_CONTACT_EMAIL",
                                 "dev.cypherms@gmail.com"),
        "last_updated": cfg.get("LEGAL_LAST_UPDATED", "28 July 2026"),
    }


# --------------------------------------------------------------------------
# Data deletion
# --------------------------------------------------------------------------

@legal_bp.route("/data-deletion", methods=["GET", "POST"])
@limiter.limit("10 per hour", methods=["POST"])
def data_deletion():
    """Human-facing instructions, plus a form for people who cannot use
    the automated route (Meta only calls the callback when the app is
    removed from within Facebook)."""
    if request.method == "POST":
        identifier = (request.form.get("identifier") or "").strip()

        if not identifier:
            flash("Enter the Page, Instagram account or email to identify "
                  "the data you want removed.", "error")
            return redirect(url_for("legal.data_deletion"))

        # Not deleted on the spot: a free-text identifier from an
        # unauthenticated form is not proof of ownership, and acting on it
        # blindly would itself be a data-protection failure. It is logged
        # for a human to verify and action - which is what the page says.
        record = DataDeletionRequest(
            confirmation_code=DataDeletionRequest.new_code(),
            source=DataDeletionRequest.SOURCE_WEB_FORM,
            external_user_id=identifier[:255],
            status=DataDeletionRequest.STATUS_MANUAL_REVIEW,
            note="Submitted via the public form; identity to be verified "
                 "before deletion.",
        )
        db.session.add(record)
        db.session.commit()
        log.info("data deletion requested via form: %s",
                 record.confirmation_code)
        return redirect(url_for("legal.deletion_status",
                                code=record.confirmation_code))

    return render_template("legal/data_deletion.html", **_doc_context())


@legal_bp.route("/data-deletion/callback", methods=["POST"])
@csrf.exempt
@limiter.limit("60 per hour")
def deletion_callback():
    """Meta's data deletion callback.

    Facebook POSTs a signed_request when someone removes the app. The
    signature MUST be verified - this endpoint deletes data, and without
    the check anyone who knew the URL could post an arbitrary user id and
    have us wipe that person's channels.

    Responds with the {url, confirmation_code} pair Meta expects.
    """
    from app.social.services import data_deletion as service

    signed_request = request.form.get("signed_request")
    if not signed_request:
        abort(400, "signed_request is required")

    payload = _parse_signed_request(signed_request)
    if payload is None:
        log.warning("data deletion callback: bad signature")
        abort(400, "invalid signed_request")

    user_id = payload.get("user_id")
    if not user_id:
        abort(400, "signed_request carried no user_id")

    record = service.handle_platform_deletion(user_id, platform=None)
    log.info("data deletion callback honoured: code=%s deleted=%s",
             record.confirmation_code, record.deleted)

    return {
        "url": url_for("legal.deletion_status",
                       code=record.confirmation_code, _external=True),
        "confirmation_code": record.confirmation_code,
    }


@legal_bp.route("/data-deletion/status/<code>")
def deletion_status(code):
    record = DataDeletionRequest.query.filter_by(
        confirmation_code=code
    ).first_or_404()
    return render_template("legal/deletion_status.html", record=record,
                           **_doc_context())


def _parse_signed_request(signed_request):
    """Verify and decode Meta's signed_request. None if it isn't genuine.

    Format is `<base64url signature>.<base64url json payload>`, signed
    HMAC-SHA256 with the app secret.
    """
    secret = current_app.config.get("META_APP_SECRET")
    if not secret:
        log.error("data deletion callback: META_APP_SECRET is not configured")
        return None

    try:
        encoded_sig, encoded_payload = signed_request.split(".", 1)
        signature = base64.urlsafe_b64decode(_pad(encoded_sig))
        payload = json.loads(base64.urlsafe_b64decode(_pad(encoded_payload)))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None

    if str(payload.get("algorithm", "")).upper() != "HMAC-SHA256":
        return None

    expected = hmac.new(
        secret.encode("utf-8"),
        encoded_payload.encode("utf-8"),
        hashlib.sha256,
    ).digest()

    # compare_digest, not ==: a plain comparison leaks how much of the
    # signature was right through its timing.
    if not hmac.compare_digest(expected, signature):
        return None

    return payload


def _pad(value):
    """base64url without padding is legal; Python's decoder needs it."""
    return value + "=" * (-len(value) % 4)
