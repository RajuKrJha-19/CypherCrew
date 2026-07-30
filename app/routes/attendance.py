"""Attendance UI + JSON API.

The top-bar widget polls /attendance/status and posts to checkin/checkout/
snooze. Admins connect Zoho and view the team's attendance under /admin.
Registered only when ATTENDANCE_ENABLED (see app/__init__.py).
"""

from flask import (
    Blueprint, current_app, flash, jsonify, redirect, render_template,
    request, url_for,
)
from flask_login import current_user, login_required

from app.attendance import service
from app.attendance.service import AttendanceError
from app.extensions import db
from app.models import User, ZohoConnection
from app.utils.permissions import can_manage_attendance

attendance_bp = Blueprint("attendance", __name__, url_prefix="/attendance")


# ---------------------------------------------------------------------------
# Top-bar widget API
# ---------------------------------------------------------------------------

@attendance_bp.route("/status", methods=["GET"])
@login_required
def status():
    return jsonify(service.status_for(current_user))


@attendance_bp.route("/checkin", methods=["POST"])
@login_required
def checkin():
    try:
        service.checkin_user(current_user)
    except AttendanceError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **service.status_for(current_user))


@attendance_bp.route("/checkout", methods=["POST"])
@login_required
def checkout():
    try:
        service.checkout_user(current_user)
    except AttendanceError as exc:
        return jsonify(ok=False, error=str(exc)), 400
    return jsonify(ok=True, **service.status_for(current_user))


@attendance_bp.route("/snooze", methods=["POST"])
@login_required
def snooze():
    service.snooze(current_user)  # duration comes from admin settings
    return jsonify(ok=True, **service.status_for(current_user))


# ---------------------------------------------------------------------------
# Admin: connection + team attendance
# ---------------------------------------------------------------------------

def _admin_guard():
    if not can_manage_attendance(current_user):
        flash("You do not have access to attendance settings.", "error")
        return False
    return True


@attendance_bp.route("/admin", methods=["GET"])
@login_required
def admin():
    if not _admin_guard():
        return redirect(url_for("dashboard.index"))

    connection = ZohoConnection.query.filter_by(status="active").first()
    users = User.query.filter(User.status == "active").order_by(
        User.name).all()
    rows = []
    for user in users:
        session = service.current_open_session(user.id)
        rows.append({
            "user": user,
            "source": service.source_of(user),
            "checked_in": session is not None,
            "since_label": (
                service._ist_label(session.check_in_at) if session else None),
        })

    return render_template(
        "attendance/admin.html",
        connection=connection,
        simulation=bool(current_app.config.get("ZOHO_SIMULATION_MODE")),
        rows=rows,
        settings=service.get_settings(),
    )


@attendance_bp.route("/settings", methods=["POST"])
@login_required
def save_settings():
    """Update the idle-alert ("buzzer") behaviour from the admin panel."""
    if not _admin_guard():
        return redirect(url_for("dashboard.index"))

    settings = service.get_settings()
    settings.idle_alerts_enabled = bool(request.form.get("idle_alerts_enabled"))
    settings.escalate_enabled = bool(request.form.get("escalate_enabled"))
    settings.buzzer_enabled = bool(request.form.get("buzzer_enabled"))

    def _clamp(name, current, lo, hi):
        try:
            return max(lo, min(hi, int(request.form.get(name, current))))
        except (TypeError, ValueError):
            return current

    settings.grace_min = _clamp("grace_min", settings.grace_min, 0, 240)
    settings.repeat_min = _clamp("repeat_min", settings.repeat_min, 1, 240)
    settings.escalate_after = _clamp(
        "escalate_after", settings.escalate_after, 1, 50)
    settings.snooze_min = _clamp("snooze_min", settings.snooze_min, 1, 480)
    settings.buzzer_volume = _clamp(
        "buzzer_volume", settings.buzzer_volume, 0, 100)
    settings.updated_by_id = current_user.id
    db.session.commit()
    flash("Attendance alert settings saved.", "success")
    return redirect(url_for("attendance.admin"))


def _redirect_uri():
    base = current_app.config.get("SOCIAL_PUBLIC_BASE_URL") \
        or request.host_url.rstrip("/")
    return f"{base.rstrip('/')}/attendance/callback"


@attendance_bp.route("/connect", methods=["POST"])
@login_required
def connect():
    if not _admin_guard():
        return redirect(url_for("dashboard.index"))
    if current_app.config.get("ZOHO_SIMULATION_MODE"):
        flash("Simulation mode is on - no real Zoho connection is needed.",
              "info")
        return redirect(url_for("attendance.admin"))
    from app.attendance import oauth
    try:
        url = oauth.start_connect(_redirect_uri(), current_user.id)
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not start the Zoho connection: {exc}", "error")
        return redirect(url_for("attendance.admin"))
    return redirect(url)


@attendance_bp.route("/callback", methods=["GET"])
@login_required
def callback():
    if not _admin_guard():
        return redirect(url_for("dashboard.index"))
    code = request.args.get("code")
    state = request.args.get("state")
    if not code or not state:
        flash("Zoho did not return an authorisation code.", "error")
        return redirect(url_for("attendance.admin"))
    from app.attendance import oauth
    try:
        oauth.finish_connect(code, state, current_user.id)
        flash("Zoho People connected.", "success")
    except Exception as exc:  # noqa: BLE001
        flash(f"Could not connect Zoho: {exc}", "error")
    return redirect(url_for("attendance.admin"))


@attendance_bp.route("/disconnect", methods=["POST"])
@login_required
def disconnect():
    if not _admin_guard():
        return redirect(url_for("dashboard.index"))
    from app.attendance import oauth
    oauth.disconnect()
    flash("Zoho People disconnected.", "success")
    return redirect(url_for("attendance.admin"))
