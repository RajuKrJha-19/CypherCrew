from app.extensions import db
from app.models import Notification


def create_notification(user_id, title, message=None, link=None, actor_id=None, task_id=None, commit=False, email=False):
    if not user_id:
        return None

    notification = Notification(
        user_id=user_id,
        title=title,
        message=message,
        link=link,
        actor_id=actor_id,
        task_id=task_id
    )
    db.session.add(notification)

    # Optional email copy. Best-effort and never allowed to break the
    # notification write: the in-app notification is the source of truth,
    # email is a bonus. Links are absolutised so they work outside the app.
    if email:
        try:
            from flask import request
            from app.models import User
            from app.utils.email import send_notification_email

            recipient = User.query.get(user_id)
            if recipient:
                absolute_link = link
                if link and not link.startswith("http"):
                    absolute_link = request.host_url.rstrip("/") + link
                send_notification_email(
                    recipient, title, message or title, absolute_link
                )
        except Exception:
            pass

    if commit:
        db.session.commit()

    return notification
