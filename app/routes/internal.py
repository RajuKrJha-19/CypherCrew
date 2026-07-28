"""Internal, machine-triggered endpoints.

Authenticated by a shared secret token (not a user session), so system
cron can call them. Not @login_required; the token is the auth.
"""

import os

from flask import Blueprint, abort, current_app, jsonify, request

from app.extensions import csrf
from app.services.reminders import send_deadline_reminders
from app.services.task_fallback import run_task_fallback_reassignment


internal_bp = Blueprint("internal", __name__, url_prefix="/internal")


def _authorised():
    """True only when REMINDER_TOKEN is configured AND the caller presents
    it (header or ?token=). If the token isn't set, the endpoint stays
    closed - so it can never run unauthenticated by default."""
    expected = os.getenv("REMINDER_TOKEN")
    provided = (
        request.headers.get("X-Reminder-Token")
        or request.args.get("token")
    )
    return bool(expected) and provided == expected


def _social_authorised():
    """Same fail-closed pattern for the Social Publishing Engine cron
    endpoints, gated by its own SOCIAL_WORKER_TOKEN."""
    expected = current_app.config.get("SOCIAL_WORKER_TOKEN")
    provided = (
        request.headers.get("X-Social-Token")
        or request.args.get("token")
    )
    return bool(expected) and provided == expected


def _social_guard():
    """Abort unless the engine is enabled AND the caller is authorised."""
    if not current_app.config.get("SOCIAL_ENGINE_ENABLED"):
        abort(503)
    if not _social_authorised():
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
