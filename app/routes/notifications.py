from datetime import timedelta

from flask import Blueprint, current_app, jsonify, request
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Notification


notifications_bp = Blueprint(
    "notifications",
    __name__,
    url_prefix="/notifications"
)

#: The only two categories a Notification row can carry - see
#: app.models.notification.Notification.category.
VALID_CATEGORIES = {"activity", "mention"}


def _category_filter():
    """?category=mention / ?category=activity, or no filter at all for
    anything else (including omitted) - an unrecognised value must
    never silently turn into "show nothing"."""
    category = request.args.get("category", "").strip()
    return category if category in VALID_CATEGORIES else None


def _teams_unread():
    """Number of Teams conversations with something new, or 0.

    Guarded twice over: returns 0 when the module is switched off (its
    tables may not even exist), and swallows any error rather than letting
    a Teams problem break the notification bell on every page in the app.
    """
    if not current_app.config.get("TEAMS_ENABLED"):
        return 0
    try:
        from app.teams.services.unread import total_unread
        return total_unread(current_user)
    except Exception:
        return 0


@notifications_bp.route("/api")
@login_required
def api_notifications():

    limit = request.args.get(
        "limit",
        10,
        type=int
    )

    if limit > 30:
        limit = 30

    category = _category_filter()

    query = Notification.query.filter_by(user_id=current_user.id)

    if category:
        query = query.filter_by(category=category)

    # The actor is eager-loaded: the popup shows who did the thing, and a
    # lazy load would be one extra query per row on a poll that runs every
    # five seconds for every signed-in user.
    notifications = query.options(
        joinedload(Notification.actor)
    ).order_by(
        Notification.id.desc()
    ).limit(
        limit
    ).all()

    unread_query = Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False
    )

    if category:
        unread_query = unread_query.filter_by(category=category)

    unread_count = unread_query.count()

    # Attendance idle-alert buzzer: a distinct sound the browser plays only
    # for idle-alert notifications. Fully guarded so the notification poll is
    # untouched when attendance is off or half-configured.
    idle_title = None
    buzzer = None
    if current_app.config.get("ATTENDANCE_ENABLED"):
        try:
            from app.services.idle_alerts import IDLE_ALERT_TITLE
            from app.attendance.service import get_settings
            settings = get_settings()
            idle_title = IDLE_ALERT_TITLE
            buzzer = {
                "enabled": bool(settings.buzzer_enabled
                                and settings.idle_alerts_enabled),
                "volume": int(settings.buzzer_volume),
            }
        except Exception:  # noqa: BLE001 - never let this break the poll
            idle_title = None
            buzzer = None

    return jsonify({
        "unread_count": unread_count,
        "attendance_buzzer": buzzer,
        # Cypher-Teams unread, piggybacked onto the poll the topbar already
        # makes on every page. Teams needs its badge to update outside the
        # module, and a second app-wide poller for one integer would double
        # the background request rate of the whole ERP. One extra indexed
        # count on a request that was happening anyway is the cheap version.
        "teams_unread": _teams_unread(),
        "notifications": [
            {
                "id": item.id,
                "title": item.title,
                "message": item.message,
                "link": item.link or "#",
                "is_read": item.is_read,
                "category": item.category,
                # Idle-task alert: the browser plays the distinct buzzer for
                # these instead of the normal chime.
                "is_idle_alert": bool(
                    idle_title and item.category == "activity"
                    and item.title == idle_title),
                # Who caused it. Initials rather than an avatar URL on
                # purpose: avatar_url() presigns against object storage,
                # which is far too expensive to do per row on a five-second
                # poll. A system-generated notification has no actor.
                "actor_name": item.actor.name if item.actor else None,
                "actor_initials": item.actor.initials if item.actor else None,
                # Kept for anything relying on the old absolute string;
                # the widget itself now renders a relative time client-
                # side from created_at_iso so "2h ago" stays accurate
                # without needing another server round-trip.
                "created_at": (
                    item.created_at + timedelta(hours=5, minutes=30)
                ).strftime("%d %b, %I:%M %p"),
                "created_at_iso": item.created_at.isoformat() + "Z"
            }
            for item in notifications
        ]
    })


@notifications_bp.route("/mark-read", methods=["POST"])
@login_required
def mark_read():

    category = _category_filter()

    query = Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False
    )

    if category:
        query = query.filter_by(category=category)

    query.update(
        {"is_read": True}
    )

    db.session.commit()

    return jsonify({
        "success": True
    })


@notifications_bp.route("/<int:notification_id>/mark-read", methods=["POST"])
@login_required
def mark_one_read(notification_id):

    notification = Notification.query.filter_by(
        id=notification_id,
        user_id=current_user.id
    ).first()

    if not notification:
        return jsonify(success=False), 404

    notification.is_read = True
    db.session.commit()

    return jsonify(success=True)
