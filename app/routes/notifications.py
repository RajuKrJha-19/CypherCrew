from datetime import timedelta

from flask import Blueprint, jsonify, request
from flask_login import login_required, current_user

from app.extensions import db
from app.models import Notification


notifications_bp = Blueprint(
    "notifications",
    __name__,
    url_prefix="/notifications"
)


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

    notifications = Notification.query.filter_by(
        user_id=current_user.id
    ).order_by(
        Notification.id.desc()
    ).limit(
        limit
    ).all()

    unread_count = Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False
    ).count()

    return jsonify({
        "unread_count": unread_count,
        "notifications": [
            {
                "id": item.id,
                "title": item.title,
                "message": item.message,
                "link": item.link or "#",
                "is_read": item.is_read,
                "created_at": (
                    item.created_at + timedelta(hours=5, minutes=30)
                ).strftime("%d %b, %I:%M %p")
            }
            for item in notifications
        ]
    })


@notifications_bp.route("/mark-read", methods=["POST"])
@login_required
def mark_read():

    Notification.query.filter_by(
        user_id=current_user.id,
        is_read=False
    ).update(
        {"is_read": True}
    )

    db.session.commit()

    return jsonify({
        "success": True
    })