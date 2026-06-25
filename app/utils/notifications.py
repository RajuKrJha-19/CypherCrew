from app.extensions import db
from app.models import Notification


def create_notification(user_id, title, message=None, link=None, actor_id=None, task_id=None, commit=False):
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

    if commit:
        db.session.commit()

    return notification
