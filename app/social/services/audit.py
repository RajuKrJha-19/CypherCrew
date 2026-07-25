"""Audit + notification helpers.

`record()` writes an engine-level SocialAuditLog row and, when the action
is tied to a Task, ALSO mirrors it onto the task timeline via TaskActivity
(actor_id may be None for system actions - the existing convention).
`notify()` is a thin pass-through to the app's create_notification.
"""

from app.extensions import db
from app.models import SocialAuditLog, TaskActivity
from app.utils.notifications import create_notification


def record(
    action,
    *,
    actor_id=None,
    account_id=None,
    post_id=None,
    target_id=None,
    detail=None,
    task_id=None,
    message=None,
    commit=False,
):
    log = SocialAuditLog(
        actor_id=actor_id,
        account_id=account_id,
        post_id=post_id,
        target_id=target_id,
        action=action,
        detail=detail,
    )
    db.session.add(log)

    # Mirror to the task timeline so social actions show up where the rest
    # of a task's history lives.
    if task_id:
        db.session.add(
            TaskActivity(
                task_id=task_id,
                actor_id=actor_id,
                action=f"social_{action}"[:100],
                message=message,
            )
        )

    if commit:
        db.session.commit()
    return log


def notify(user_id, title, message, *, link=None, actor_id=None,
           task_id=None, email=False, commit=False):
    return create_notification(
        user_id=user_id,
        title=title,
        message=message,
        link=link,
        actor_id=actor_id,
        task_id=task_id,
        email=email,
        commit=commit,
    )
