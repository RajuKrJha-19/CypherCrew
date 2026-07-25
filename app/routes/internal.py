"""Internal, machine-triggered endpoints.

Authenticated by a shared secret token (not a user session), so system
cron can call them. Not @login_required; the token is the auth.
"""

import os

from flask import Blueprint, abort, jsonify, request

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
