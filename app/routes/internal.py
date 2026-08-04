"""Internal, machine-triggered endpoints.

Authenticated by a shared secret token (not a user session), so system
cron can call them. Not @login_required; the token is the auth.
"""

import hmac
import os

from flask import Blueprint, abort, current_app, jsonify, request

from app.extensions import csrf
from app.services.reminders import send_deadline_reminders
from app.services.task_fallback import run_task_fallback_reassignment


internal_bp = Blueprint("internal", __name__, url_prefix="/internal")


def _secret_ok(expected, header):
    """True only when `expected` is configured AND the caller presents it in
    `header`. Fail-closed: an unset secret keeps the endpoint shut, so it can
    never run unauthenticated by default.

    **Header only.** These guards used to accept `?token=` / `?secret=` as an
    alternative, and a query string is not a private channel: it is written to
    nginx, Cloudflare and gunicorn access logs, and forwarded in Referer. One
    leaked log line handed over /internal/social/worker/run and
    /internal/social/tokens/refresh. Any cron job still passing the secret in
    the URL has to move it into the header.

    Compared with hmac.compare_digest rather than `==`, which returns as soon
    as two bytes differ and so leaks the secret's prefix to anyone who can
    time the response. Same pattern legal.py:178 already uses.
    """
    provided = request.headers.get(header)
    if not expected or not provided:
        return False
    return hmac.compare_digest(str(provided), str(expected))


def _authorised():
    return _secret_ok(os.getenv("REMINDER_TOKEN"), "X-Reminder-Token")


def _social_authorised():
    """Fail-closed guard for the Social Publishing Engine cron endpoints,
    gated by its own SOCIAL_WORKER_TOKEN."""
    return _secret_ok(
        current_app.config.get("SOCIAL_WORKER_TOKEN"), "X-Social-Token")


def _social_guard():
    """Abort unless the engine is enabled AND the caller is authorised."""
    if not current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        abort(503)
    if not _social_authorised():
        abort(403)


def _zoho_authorised():
    """Fail-closed token guard for the attendance cron endpoints, gated by
    its own ZOHO_SYNC_TOKEN."""
    return _secret_ok(
        current_app.config.get("ZOHO_SYNC_TOKEN"), "X-Zoho-Token")


def _attendance_guard():
    """Abort unless attendance is enabled AND the caller is authorised."""
    if not current_app.config.get("ATTENDANCE_ENABLED"):
        abort(503)
    if not _zoho_authorised():
        abort(403)


@internal_bp.route("/reminders/run", methods=["POST"])
@csrf.exempt
def run_reminders():
    """Trigger the daily deadline-reminder pass. Protected by REMINDER_TOKEN.

    Cron example (once a day):
        curl -fsS -X POST -H "X-Reminder-Token: $REMINDER_TOKEN" \\
             https://crew.cypherms.com/internal/reminders/run
    """
    if not _authorised():
        abort(403)

    result = send_deadline_reminders()
    return jsonify(success=True, **result)


@internal_bp.route("/task-fallback/run", methods=["POST"])
@csrf.exempt
def run_task_fallback():
    """Shift stalled tasks to their backup assignee. Protected by
    REMINDER_TOKEN. Run this far more often than the daily reminder -
    fallback windows are counted in hours, so an hourly cron entry (or
    every 15-30 min) is what actually makes the feature work on time.

    Cron example (every 15 minutes):
        curl -fsS -X POST -H "X-Reminder-Token: $REMINDER_TOKEN" \\
             https://crew.cypherms.com/internal/task-fallback/run
    """
    if not _authorised():
        abort(403)

    result = run_task_fallback_reassignment()
    return jsonify(success=True, **result)


# ----------------------------------------------------------------------
# Social Publishing Engine cron endpoints
# Gated by SOCIAL_ENGINE_ENABLED + SOCIAL_WORKER_TOKEN. Services are
# imported lazily so the engine stays fully dormant when the flag is off.
#
# Cron examples:
#   * * * * *      worker/run      (drain the publish queue)
#   * * * * *      scheduler/run   (enqueue due scheduled posts)
#   */30 * * * *   analytics/run   (refresh insights)
#   0 */6 * * *    tokens/refresh  (refresh expiring platform tokens)
#   0 2 * * *      media-gc/run    (delete orphaned direct-upload objects)
# ----------------------------------------------------------------------

@internal_bp.route("/social/worker/run", methods=["POST"])
@csrf.exempt
def run_social_worker():
    _social_guard()
    from app.social.queue import worker
    return jsonify(success=True, **worker.drain())


@internal_bp.route("/social/scheduler/run", methods=["POST"])
@csrf.exempt
def run_social_scheduler():
    _social_guard()
    from app.social.services import scheduling
    return jsonify(success=True, **scheduling.enqueue_due())


@internal_bp.route("/social/analytics/run", methods=["POST"])
@csrf.exempt
def run_social_analytics():
    _social_guard()
    from app.social.services import analytics
    return jsonify(success=True, **analytics.sync_recent())


@internal_bp.route("/reviews/auto-reply/run", methods=["POST"])
@csrf.exempt
def run_reviews_auto_reply():
    """Cron entry point: sync each GBP location's reviews and auto-reply the
    safe ones (guarded). No-op unless GBP_REVIEWS_ENABLED + GBP_AUTOREPLY_ENABLED
    are both on. Reuses the social worker token, so _social_guard() also
    requires SOCIAL_ENGINE_ENABLED (GBP accounts connect through that engine)."""
    _social_guard()
    if not current_app.config.get("GBP_REVIEWS_ENABLED"):
        return jsonify(success=True, skipped="reviews disabled")
    from app.ai import settings as ai_settings
    if not ai_settings.autoreply_config()["enabled"]:
        return jsonify(success=True, skipped="auto-reply disabled")
    from app.models import SocialAccount
    from app.social.reviews import service as reviews_service
    total = 0
    for account in SocialAccount.query.filter_by(platform="google_business").all():
        try:
            total += reviews_service.auto_reply_run(account).get("auto_replied", 0)
        except Exception:  # noqa: BLE001
            current_app.logger.exception(
                "[reviews] auto-reply failed for account %s", account.id)
    return jsonify(success=True, auto_replied=total)


@internal_bp.route("/engage/auto-reply/run", methods=["POST"])
@csrf.exempt
def run_engage_auto_reply():
    """Cron entry point: sync social comments and auto-reply the safe ones
    (guarded). No-op unless ENGAGE_AUTOREPLY_ENABLED is on; the deeper feature/
    admin/per-client gates are re-checked inside the run. Reuses the social
    worker token + engine gate."""
    _social_guard()
    if not current_app.config.get("ENGAGE_AUTOREPLY_ENABLED"):
        return jsonify(success=True, skipped="engage auto-reply disabled")
    from app.social.services import engage as engage_svc
    try:
        out = engage_svc.auto_reply_comments_run()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("[engage] auto-reply run failed")
        return jsonify(success=False, auto_replied=0)
    return jsonify(success=True, **out)


@internal_bp.route("/engage/automod/run", methods=["POST"])
@csrf.exempt
def run_engage_automod():
    """Cron entry point: sync social comments and auto-HIDE the spam ones
    (guarded, reversible). No-op unless ENGAGE_AUTOMOD_ENABLED is on; the admin/
    per-client/blocklist gates are re-checked inside the run. Reuses the social
    worker token + engine gate."""
    _social_guard()
    if not current_app.config.get("ENGAGE_AUTOMOD_ENABLED"):
        return jsonify(success=True, skipped="engage auto-mod disabled")
    from app.social.services import engage as engage_svc
    try:
        out = engage_svc.automod_run()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("[engage] auto-mod run failed")
        return jsonify(success=False, hidden=0)
    return jsonify(success=True, **out)


@internal_bp.route("/engage/ads-sync/run", methods=["POST"])
@csrf.exempt
def run_engage_ads_sync():
    """Cron entry point: discover ad/boosted posts and pull their comments into
    Engage. No-op unless SOCIAL_ADS_COMMENTS_ENABLED is on. Reuses the social
    worker token + engine gate."""
    _social_guard()
    if not current_app.config.get("SOCIAL_ADS_COMMENTS_ENABLED"):
        return jsonify(success=True, skipped="ad comments disabled")
    from app.social.services import engage_ads
    try:
        out = engage_ads.sync_ad_comments()
    except Exception:  # noqa: BLE001
        current_app.logger.exception("[engage-ads] sync failed")
        return jsonify(success=False, discovered=0)
    return jsonify(success=True, **out)


@internal_bp.route("/social/tokens/refresh", methods=["POST"])
@csrf.exempt
def run_social_token_refresh():
    _social_guard()
    from app.social.tokens import refresh
    return jsonify(success=True, **refresh.refresh_expiring())


@internal_bp.route("/social/media-gc/run", methods=["POST"])
@csrf.exempt
def run_social_media_gc():
    """Delete orphaned direct-upload objects from R2 (uploads never saved, or
    left behind by a deleted post). Safe to run daily: 0 2 * * *."""
    _social_guard()
    from app.social.media import gc
    return jsonify(success=True, **gc.sweep())


@internal_bp.route("/social/status", methods=["GET"])
@csrf.exempt
def social_status():
    """Queue depth / dead-letter / account-health snapshot for monitoring."""
    _social_guard()
    from app.social.status import engine_status
    return jsonify(success=True, **engine_status())


# ----------------------------------------------------------------------
# Attendance (Zoho People bridge + idle-task alerts) cron endpoints
# Gated by ATTENDANCE_ENABLED + ZOHO_SYNC_TOKEN. Services imported lazily
# so the module stays dormant when the flag is off.
#
# Cron examples:
#   */2 * * * *    attendance/sync         (pull Zoho attendance)
#   */10 * * * *   attendance/idle-alerts  (nudge idle checked-in users)
# ----------------------------------------------------------------------

@internal_bp.route("/attendance/sync", methods=["POST"])
@csrf.exempt
def run_attendance_sync():
    _attendance_guard()
    from app.attendance import service
    return jsonify(success=True, **service.sync_attendance())


@internal_bp.route("/attendance/idle-alerts", methods=["POST"])
@csrf.exempt
def run_attendance_idle_alerts():
    _attendance_guard()
    from app.services.idle_alerts import run_idle_task_alerts
    return jsonify(success=True, **run_idle_task_alerts())


@internal_bp.route("/attendance/webhook", methods=["POST"])
@csrf.exempt
def attendance_webhook():
    """Inbound Zoho People automation webhook. Verifies its own secret and
    triggers an immediate sync so a check-in shows up without waiting for the
    poll. Polling remains the guaranteed fallback if this is not configured.
    """
    if not current_app.config.get("ATTENDANCE_ENABLED"):
        abort(503)
    if not _secret_ok(current_app.config.get("ZOHO_WEBHOOK_SECRET"),
                      "X-Zoho-Webhook-Secret"):
        abort(403)
    from app.attendance import service
    return jsonify(success=True, **service.sync_attendance())
